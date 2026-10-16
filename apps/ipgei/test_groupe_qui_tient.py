"""
Test — dire QUEL groupe tient déjà un enseignant ou une salle.

La grille « Gérer les emplois » grise un enseignant pris ailleurs sur le même
créneau, avec le groupe qui le tient : « pris (MPSI B · B1) ». Le refus du
serveur, lui, disait « MPSI B B1 », et l'occupation « MPSI B (B1) » : trois
écritures d'une même chose. « B1 » seul ne suffit pas — il existe en MPSI B
comme en MP B la même année (relevé de la base `IPGEI` du 16/10/2026).

Ce que le test fige :
  1. le nom complet : la classe seule, ou « classe · sous-groupe » ;
  2. le refus d'une séance réelle nomme le sous-groupe ainsi ;
  3. le refus d'une case de patron aussi ;
  4. l'occupation lue par la grille (semaine et patron) écrit la même chose ;
  5. les règles de conflit elles-mêmes ne bougent pas : une séance annulée
     libère toujours son créneau, la même classe n'est pas un conflit.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework.test import APITestCase

from apps.ipgei.models import (
    Classe, GrilleType, Matiere, NiveauIPGEI, SeanceReelle, SeanceType,
    SemaineIPGEI, SemestreIPGEI, SousGroupeTP,
)
from apps.ipgei.serializers import (
    SeanceReelleSerializer, SeanceTypeSerializer, nom_complet_groupe,
)
from apps.parametres.models import Creneau, Institution, Jour, Niveau, Seance
from apps.prof.models import Prof, ProfTypeHistory
from apps.salle.models import Salle

User = get_user_model()
ANNEE = '2026-2027'
URL_OCCUPATION = '/api/v1/ipgei/seances/occupation/'


# ── Le nom complet ───────────────────────────────────────────────────────────
class _Nomme:
    def __init__(self, nom='', groupe=''):
        self.nom, self.groupe = nom, groupe


def test_la_classe_entiere_garde_son_nom():
    assert nom_complet_groupe(_Nomme(nom='MPSI B')) == 'MPSI B'


def test_le_sous_groupe_est_rattache_a_sa_classe():
    assert nom_complet_groupe(_Nomme(nom='MPSI B'), _Nomme(groupe='B1')) == 'MPSI B · B1'
    assert nom_complet_groupe(_Nomme(nom='MP B'), _Nomme(groupe='B1')) == 'MP B · B1'


def test_un_nom_manquant_ne_laisse_pas_de_separateur_orphelin():
    assert nom_complet_groupe(_Nomme(nom=''), _Nomme(groupe='B1')) == 'B1'
    assert nom_complet_groupe(_Nomme(nom='MPSI B'), _Nomme(groupe='')) == 'MPSI B'
    assert nom_complet_groupe(None) == ''


# ── Le refus du serveur et l'occupation ─────────────────────────────────────
class GroupeQuiTientTest(APITestCase):
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
        socle = Niveau.objects.create(niveau='MPSI')
        ref = NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI',
                                         libelle='MPSI', rang=1)
        s1 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S1', annee_universitaire=ANNEE,
            date_debut=date(2026, 9, 14))
        cls.semaine = SemaineIPGEI.objects.create(
            semestre=s1, numero=1, date_debut=date(2026, 9, 14),
            date_fin=date(2026, 9, 19), type_semaine=SemaineIPGEI.TYPE_COURS)
        cls.lundi = Jour.objects.create(jour='Lundi')
        cls.c1 = Creneau.objects.create(creneau='08:00-10:00', ordre=1)
        cls.tp = Seance.objects.create(type_seance='TP')
        cls.prof = Prof.objects.create(nom='SEJAD', NNI=1234567890, type='permanent')
        cls.salle = Salle.objects.create(nom='Labo 1')
        cls.matiere = Matiere.objects.create(
            institution=cls.inst, code='INFO', intitule='Informatique',
            code_semestre='S1', niveau_ref=ref, coefficient=Decimal('2'))

        cls.mpsi_a = Classe.objects.create(institution=cls.inst, niveau=socle,
                                           annee_universitaire=ANNEE, groupe='A')
        cls.mpsi_b = Classe.objects.create(institution=cls.inst, niveau=socle,
                                           annee_universitaire=ANNEE, groupe='B')
        cls.b1 = SousGroupeTP.objects.create(classe=cls.mpsi_b, groupe='B1')
        cls.admin = User.objects.create_user(username='adm_occ', email='occ@t.l',
                                             password='Xk93!plqz72', role='admin')

    def _tp_de_b1(self, **extra):
        """Le TP de MPSI B · B1, lundi premier créneau, avec SEJAD en Labo 1."""
        return SeanceReelle.objects.create(
            classe=self.mpsi_b, semaine=self.semaine, sous_groupe=self.b1,
            jour=self.lundi, creneau=self.c1, matiere=self.matiere,
            prof=self.prof, salle=self.salle, type_seance=self.tp,
            date=date(2026, 9, 14), origine=SeanceReelle.ORIGINE_MANUELLE, **extra)

    def _seance_de_a(self, **champs):
        donnees = {
            'classe': self.mpsi_a.pk, 'semaine': self.semaine.pk,
            'jour': self.lundi.pk, 'creneau': self.c1.pk,
            'matiere': self.matiere.pk, 'type_seance': self.tp.pk,
            'date': '2026-09-14',
        }
        donnees.update(champs)
        return SeanceReelleSerializer(data=donnees)

    # ── Le refus ────────────────────────────────────────────────────────────
    def test_le_refus_d_un_enseignant_nomme_classe_et_sous_groupe(self):
        self._tp_de_b1()
        s = self._seance_de_a(prof=self.prof.pk)
        self.assertFalse(s.is_valid())
        self.assertIn('(MPSI B · B1)', str(s.errors['prof']))

    def test_le_refus_d_une_salle_nomme_classe_et_sous_groupe(self):
        self._tp_de_b1()
        s = self._seance_de_a(salle=self.salle.pk)
        self.assertFalse(s.is_valid())
        self.assertIn('(MPSI B · B1)', str(s.errors['salle']))

    def test_le_refus_sur_le_patron_parle_de_meme(self):
        grille_b = GrilleType.objects.create(classe=self.mpsi_b, type_semestre='I')
        SeanceType.objects.create(
            grille=grille_b, sous_groupe=self.b1, jour=self.lundi, creneau=self.c1,
            matiere=self.matiere, prof=self.prof, type_seance=self.tp)
        grille_a = GrilleType.objects.create(classe=self.mpsi_a, type_semestre='I')
        s = SeanceTypeSerializer(data={
            'grille': grille_a.pk, 'jour': self.lundi.pk, 'creneau': self.c1.pk,
            'matiere': self.matiere.pk, 'prof': self.prof.pk, 'type_seance': self.tp.pk,
        })
        self.assertFalse(s.is_valid())
        self.assertIn('(MPSI B · B1)', str(s.errors['prof']))

    # ── Les règles ne bougent pas ───────────────────────────────────────────
    def test_une_seance_annulee_libere_toujours_le_creneau(self):
        self._tp_de_b1(annulee=True)
        s = self._seance_de_a(prof=self.prof.pk, salle=self.salle.pk)
        self.assertTrue(s.is_valid(), s.errors)

    # ── L'occupation lue par la grille ──────────────────────────────────────
    def _occupation(self, **params):
        self.client.force_authenticate(user=self.admin)
        reponse = self.client.get(URL_OCCUPATION, {'classe': self.mpsi_a.pk, **params})
        self.assertEqual(reponse.status_code, 200, getattr(reponse, 'data', None))
        return reponse.data

    def test_l_occupation_de_la_semaine_ecrit_comme_le_refus(self):
        self._tp_de_b1()
        SeanceReelle.objects.create(
            classe=self.mpsi_b, semaine=self.semaine, jour=self.lundi,
            creneau=Creneau.objects.create(creneau='10:00-12:00', ordre=2),
            matiere=self.matiere, prof=self.prof, type_seance=self.tp,
            date=date(2026, 9, 14), origine=SeanceReelle.ORIGINE_MANUELLE)
        noms = sorted(o['classe'] for o in self._occupation(semaine=self.semaine.pk))
        self.assertEqual(noms, ['MPSI B', 'MPSI B · B1'])

    def test_l_occupation_du_patron_ecrit_comme_le_refus(self):
        grille_b = GrilleType.objects.create(classe=self.mpsi_b, type_semestre='I')
        SeanceType.objects.create(
            grille=grille_b, sous_groupe=self.b1, jour=self.lundi, creneau=self.c1,
            matiere=self.matiere, salle=self.salle, type_seance=self.tp)
        occupation = self._occupation(type_semestre='I')
        self.assertEqual([o['classe'] for o in occupation], ['MPSI B · B1'])
        self.assertEqual(occupation[0]['salle'], self.salle.pk)

    def test_la_classe_affichee_n_est_pas_dans_sa_propre_occupation(self):
        self._tp_de_b1()
        self.client.force_authenticate(user=self.admin)
        reponse = self.client.get(URL_OCCUPATION, {'classe': self.mpsi_b.pk,
                                                   'semaine': self.semaine.pk})
        self.assertEqual(reponse.data, [])
