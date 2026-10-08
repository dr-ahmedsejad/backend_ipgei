"""
Test — l'emploi du temps publié d'un enseignant réunit les deux semestres.

Une semaine de calendrier existe une fois par semestre de la période : S1 pour
les MPSI, S3 pour les MP. Un enseignant intervient dans les deux. La liste lui
montrait donc chaque semaine EN DOUBLE, aux mêmes dates, sans rien pour les
distinguer — et chaque entrée ne portait que la moitié de son service.

Ce que le test fige :
  1. `?semaine=12,19` rend les séances des deux semestres ;
  2. un seul identifiant continue de marcher — c'est le cas d'une classe ;
  3. un identifiant qui n'est pas un nombre est refusé, et le dit ;
  4. sans paramètre, refus ;
  5. l'en-tête du document nomme UN semestre, ou les DEUX ;
  6. la dernière version de chaque classe reste la règle, semaine par semaine.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.ipgei.models import (
    Classe, EmploiArchive, NiveauIPGEI, SemaineIPGEI, SemestreIPGEI,
)
from apps.ipgei.views import _libelle_semestres
from apps.parametres.models import Institution, Niveau

User = get_user_model()
ANNEE = '2026-2027'
GRILLE = '/api/v1/ipgei/archives-edt/grille/'
PROF = 7


class EdtDeuxSemestresTest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        mpsi = Niveau.objects.create(niveau='MPSI')
        mp   = Niveau.objects.create(niveau='MP')
        NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI', libelle='MPSI', rang=1)
        NiveauIPGEI.objects.create(institution=cls.inst, code='MP', libelle='MP', rang=2)

        debut = date.today()
        fin   = debut + timedelta(days=5)

        def semestre(code):
            return SemestreIPGEI.objects.create(
                institution=cls.inst, code=code, annee_universitaire=ANNEE,
                date_debut=debut)

        # Mêmes dates, même numéro : c'est toute la difficulté.
        cls.s1, cls.s3 = semestre('S1'), semestre('S3')
        cls.semaine_mpsi = SemaineIPGEI.objects.create(
            semestre=cls.s1, numero=4, date_debut=debut, date_fin=fin, type_semaine='cours')
        cls.semaine_mp = SemaineIPGEI.objects.create(
            semestre=cls.s3, numero=4, date_debut=debut, date_fin=fin, type_semaine='cours')

        cls.mpsi_a = Classe.objects.create(institution=cls.inst, niveau=mpsi,
                                           annee_universitaire=ANNEE, groupe='A')
        cls.mp_a = Classe.objects.create(institution=cls.inst, niveau=mp,
                                         annee_universitaire=ANNEE, groupe='A')
        cls.admin = User.objects.create_user(username='adm_edt', email='a@t.l',
                                             password='Xk93!plqz72', role='admin')

    def _archive(self, semaine, classe, **extra):
        donnees = dict(
            semaine=semaine, classe=classe, version=1, genere_le=date.today(),
            classe_nom=classe.nom, prof_ref=PROF, prof_nom='Cheikh Nagi JMAILLI',
            matiere_code='MATH', jour_libelle='Lundi', creneau_libelle='08h00-09h30',
        )
        donnees.update(extra)
        return EmploiArchive.objects.create(**donnees)

    def setUp(self):
        self.client.force_authenticate(user=self.admin)
        self._archive(self.semaine_mpsi, self.mpsi_a, matiere_code='MATH-S1')
        self._archive(self.semaine_mp, self.mp_a, matiere_code='MATH-S3')

    def _classes(self, semaine_param):
        reponse = self.client.get(GRILLE, {'semaine': semaine_param, 'prof': PROF})
        self.assertEqual(reponse.status_code, 200, reponse.data)
        return sorted(l['classe_nom'] for l in reponse.data)

    # ── La réunion ──────────────────────────────────────────────────────────
    def test_deux_semaines_rendent_les_deux_semestres(self):
        self.assertEqual(
            self._classes(f'{self.semaine_mpsi.pk},{self.semaine_mp.pk}'),
            ['MP A', 'MPSI A'])

    def test_une_seule_semaine_reste_valable(self):
        self.assertEqual(self._classes(str(self.semaine_mp.pk)), ['MP A'])
        self.assertEqual(self._classes(str(self.semaine_mpsi.pk)), ['MPSI A'])

    def test_les_espaces_et_virgules_en_trop_sont_tolerés(self):
        self.assertEqual(
            self._classes(f' {self.semaine_mpsi.pk} , {self.semaine_mp.pk} ,'),
            ['MP A', 'MPSI A'])

    # ── Les refus ───────────────────────────────────────────────────────────
    def test_un_identifiant_qui_n_est_pas_un_nombre_est_refuse(self):
        reponse = self.client.get(GRILLE, {'semaine': '12,abc', 'prof': PROF})
        self.assertEqual(reponse.status_code, 400, reponse.data)
        self.assertIn('abc', str(reponse.data))

    def test_sans_semaine_refus(self):
        self.assertEqual(self.client.get(GRILLE, {'prof': PROF}).status_code, 400)

    # ── L'en-tête du document ───────────────────────────────────────────────
    def test_l_en_tete_nomme_un_ou_deux_semestres(self):
        self.assertEqual(_libelle_semestres([]), '')
        self.assertEqual(_libelle_semestres([self.semaine_mp]), 'Semestre S3')
        self.assertEqual(_libelle_semestres([self.semaine_mpsi, self.semaine_mp]),
                         'Semestres S1 · S3')

    # ── La règle des versions ───────────────────────────────────────────────
    def test_la_derniere_version_de_chaque_classe_est_retenue(self):
        self._archive(self.semaine_mp, self.mp_a, version=2, matiere_code='MATH-S3-v2')
        reponse = self.client.get(
            GRILLE, {'semaine': f'{self.semaine_mpsi.pk},{self.semaine_mp.pk}', 'prof': PROF})
        self.assertEqual(reponse.status_code, 200, reponse.data)
        codes = sorted(l['matiere_code'] for l in reponse.data)
        # La version 1 de MP A disparaît au profit de la 2 ; MPSI A, restée en
        # version 1, ne doit pas disparaître avec elle.
        self.assertEqual(codes, ['MATH-S1', 'MATH-S3-v2'])
