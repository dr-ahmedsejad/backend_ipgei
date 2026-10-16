"""
Test — un enseignant, une salle : un seul endroit à la même HEURE, quel que
soit le calendrier qui décrit la semaine.

Chaque niveau a son calendrier : la semaine du MPSI (S1) et celle du MP (S3)
sont deux lignes `SemaineIPGEI` distinctes, aux mêmes dates. Les conflits se
cherchaient sur la LIGNE de semaine : un enseignant pouvait être placé en
MPSI A et en MP B le même lundi à 8 h sans un mot — ni au serveur, ni dans la
grille, ni à la duplication (relevé de la base `IPGEI` du 16/10/2026 : trente
dates de début partagées entre S1 et S3).

Ce que le test fige :
  1. le refus d'une séance réelle voit la classe de l'autre niveau ;
  2. l'occupation lue par la grille aussi ;
  3. la duplication l'écarte et le dit ;
  4. une autre semaine civile, ou une séance annulée, ne bloque rien.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework.test import APITestCase

from apps.ipgei.models import (
    Classe, Matiere, NiveauIPGEI, SeanceReelle, SemaineIPGEI, SemestreIPGEI,
)
from apps.ipgei.serializers import SeanceReelleSerializer
from apps.ipgei.services import _occupation_ailleurs
from apps.parametres.models import Creneau, Institution, Jour, Niveau, Seance
from apps.prof.models import Prof, ProfTypeHistory
from apps.salle.models import Salle

User = get_user_model()
ANNEE = '2026-2027'
URL_OCCUPATION = '/api/v1/ipgei/seances/occupation/'
LUNDI = date(2026, 9, 14)


class SemaineCivileTest(APITestCase):
    @classmethod
    def setUpClass(cls):
        # `prof_type_history` naît d'une migration SQL brute, absente sous
        # `--no-migrations` — même parade que `test_duplication_conflits`.
        if ProfTypeHistory._meta.db_table not in connection.introspection.table_names():
            with connection.schema_editor() as se:
                se.create_model(ProfTypeHistory)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        ref_mpsi = NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI',
                                              libelle='MPSI', rang=1)
        ref_mp = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                            libelle='MP', rang=2)
        s1 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S1', annee_universitaire=ANNEE, date_debut=LUNDI)
        s3 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S3', annee_universitaire=ANNEE, date_debut=LUNDI)

        def semaine(semestre, numero, debut):
            return SemaineIPGEI.objects.create(
                semestre=semestre, numero=numero, date_debut=debut,
                date_fin=date.fromordinal(debut.toordinal() + 5),
                type_semaine=SemaineIPGEI.TYPE_COURS)

        # Deux calendriers, la même semaine civile.
        cls.sem_mpsi = semaine(s1, 1, LUNDI)
        cls.sem_mp = semaine(s3, 1, LUNDI)
        # Et la semaine civile suivante, côté MP.
        cls.sem_mp_suivante = semaine(s3, 2, date(2026, 9, 21))

        cls.lundi = Jour.objects.create(jour='Lundi')
        cls.c1 = Creneau.objects.create(creneau='08:00-10:00', ordre=1)
        cls.cm = Seance.objects.create(type_seance='CM')
        cls.prof = Prof.objects.create(nom='SEJAD', NNI=1234567890, type='permanent')
        cls.salle = Salle.objects.create(nom='Amphi 1')
        cls.math_mpsi = Matiere.objects.create(
            institution=cls.inst, code='MATH1', intitule='Analyse',
            code_semestre='S1', niveau_ref=ref_mpsi, coefficient=Decimal('2'))
        cls.math_mp = Matiere.objects.create(
            institution=cls.inst, code='MATH3', intitule='Algèbre',
            code_semestre='S3', niveau_ref=ref_mp, coefficient=Decimal('2'))

        cls.mpsi_a = Classe.objects.create(
            institution=cls.inst, niveau=Niveau.objects.get_or_create(niveau='MPSI')[0],
            annee_universitaire=ANNEE, groupe='A')
        cls.mp_b = Classe.objects.create(
            institution=cls.inst, niveau=Niveau.objects.get_or_create(niveau='MP')[0],
            annee_universitaire=ANNEE, groupe='B')
        cls.admin = User.objects.create_user(username='adm_civ', email='civ@t.l',
                                             password='Xk93!plqz72', role='admin')

    def _cours_de_mp(self, semaine=None, **extra):
        """Le cours de MP B, lundi 8 h, avec SEJAD en Amphi 1."""
        semaine = semaine or self.sem_mp
        return SeanceReelle.objects.create(
            classe=self.mp_b, semaine=semaine, jour=self.lundi, creneau=self.c1,
            matiere=self.math_mp, prof=self.prof, salle=self.salle,
            type_seance=self.cm, date=semaine.date_debut,
            origine=SeanceReelle.ORIGINE_MANUELLE, **extra)

    def _seance_de_mpsi(self, **champs):
        donnees = {
            'classe': self.mpsi_a.pk, 'semaine': self.sem_mpsi.pk,
            'jour': self.lundi.pk, 'creneau': self.c1.pk,
            'matiere': self.math_mpsi.pk, 'type_seance': self.cm.pk,
            'date': LUNDI.isoformat(),
        }
        donnees.update(champs)
        return SeanceReelleSerializer(data=donnees)

    # ── Le refus du serveur ─────────────────────────────────────────────────
    def test_un_enseignant_du_mp_est_pris_pour_le_mpsi_la_meme_semaine(self):
        self._cours_de_mp()
        s = self._seance_de_mpsi(prof=self.prof.pk)
        self.assertFalse(s.is_valid())
        self.assertIn('(MP B)', str(s.errors['prof']))

    def test_une_salle_du_mp_est_prise_pour_le_mpsi_la_meme_semaine(self):
        self._cours_de_mp()
        s = self._seance_de_mpsi(salle=self.salle.pk)
        self.assertFalse(s.is_valid())
        self.assertIn('(MP B)', str(s.errors['salle']))

    def test_une_autre_semaine_civile_ne_bloque_rien(self):
        self._cours_de_mp(semaine=self.sem_mp_suivante)
        s = self._seance_de_mpsi(prof=self.prof.pk, salle=self.salle.pk)
        self.assertTrue(s.is_valid(), s.errors)

    def test_une_seance_annulee_libere_toujours_le_creneau(self):
        self._cours_de_mp(annulee=True)
        s = self._seance_de_mpsi(prof=self.prof.pk, salle=self.salle.pk)
        self.assertTrue(s.is_valid(), s.errors)

    # ── L'occupation lue par la grille ──────────────────────────────────────
    def test_l_occupation_de_la_semaine_voit_l_autre_niveau(self):
        self._cours_de_mp()
        self._cours_de_mp(semaine=self.sem_mp_suivante)
        self.client.force_authenticate(user=self.admin)
        reponse = self.client.get(URL_OCCUPATION, {'classe': self.mpsi_a.pk,
                                                   'semaine': self.sem_mpsi.pk})
        self.assertEqual(reponse.status_code, 200, getattr(reponse, 'data', None))
        self.assertEqual([(o['classe'], o['prof'], o['salle']) for o in reponse.data],
                         [('MP B', self.prof.pk, self.salle.pk)])

    def test_une_semaine_inconnue_est_refusee(self):
        self.client.force_authenticate(user=self.admin)
        reponse = self.client.get(URL_OCCUPATION, {'classe': self.mpsi_a.pk,
                                                   'semaine': 999999})
        self.assertEqual(reponse.status_code, 400)

    # ── La duplication ──────────────────────────────────────────────────────
    def test_la_duplication_voit_l_autre_niveau_rangee_par_semaine_demandee(self):
        self._cours_de_mp()
        index = _occupation_ailleurs(self.mpsi_a, [self.sem_mpsi])
        case = index[(self.sem_mpsi.pk, self.lundi.pk, self.c1.pk)]
        self.assertEqual(case['profs'], {self.prof.pk: 'MP B'})
        self.assertEqual(case['salles'], {self.salle.pk: 'MP B'})

    def test_la_duplication_ignore_une_autre_semaine_civile(self):
        self._cours_de_mp(semaine=self.sem_mp_suivante)
        self.assertEqual(_occupation_ailleurs(self.mpsi_a, [self.sem_mpsi]), {})
