"""
Test de non-régression — correctif E.3 :
resume_vacataire (GET /api/v1/vacations/resume-vacataire/) doit calculer les
montants sur le taux_paiement FIGE de chaque ligne Vacation, et NON sur le
tarif courant get_taux_at() (qui faussait le résumé dès qu'un tarif changeait).

Reproduit le Cas 1 du diagnostic : 6 h de CM saisies à 1000 MRU/h figé, alors
que le tarif courant est passé à 1200. Le résumé doit afficher 6000 (taux figé),
pas 7200 (tarif du jour).

Exécution :  pytest apps/vacation/test_resume_vacataire.py
Base SQLite en mémoire (siga.settings.test) — aucun contact avec gesafped26.
"""
from datetime import date

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from django.db import connection

from apps.parametres.models import Institution, Seance, Paiement
from apps.prof.models import Prof, ProfTypeHistory
from apps.vacation.models import Vacation

User = get_user_model()

URL = '/api/v1/vacations/resume-vacataire/'
ANNEE = '2024-2025'


def _cm_row(data):
    return next((r for r in data['summary'] if r['type'] == 'CM'), None)


class ResumeVacataireTauxFigeTest(APITestCase):
    @classmethod
    def setUpClass(cls):
        # ProfTypeHistory est managed=False (table créée hors-Django) → run-syncdb
        # ne la crée pas. Le signal post_save de Prof y écrit pourtant à la création.
        # On la crée dans la base de test avant tout chargement de données.
        # create_model plante la transaction PG si la table existe deja (creee par
        # la fixture session de tests/conftest.py) : verifier l'existence AVANT
        # d'entrer dans le schema_editor est vendor-neutre (sqlite ET postgresql).
        if ProfTypeHistory._meta.db_table not in connection.introspection.table_names():
            with connection.schema_editor() as se:
                se.create_model(ProfTypeHistory)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(
            acronyme='TST', nom='Institut Test', est_principale=True,
        )
        cls.cm = Seance.objects.create(type_seance='CM')
        # Tarif COURANT (le plus récent) = 1200. get_taux_at('CM') le renverrait.
        Paiement.objects.create(type='CM', taux=1200.0, date_debut=date(2020, 1, 1))

        cls.prof = Prof.objects.create(NNI=1234567, nom='Vacataire Test', type='vacataire')
        cls.admin = User.objects.create_user(
            username='admin_resume', email='admin_resume@test.local',
            password='Xk93!plqz72', role='admin',
        )

    def setUp(self):
        self.client.force_authenticate(user=self.admin)

    def _add_vacation(self, duree, taux_fige):
        return Vacation.objects.create(
            prof=self.prof, type=self.cm, duree=duree, date=date(2025, 3, 3),
            annee_univ=ANNEE, taux_paiement=taux_fige, institution=self.inst,
        )

    def test_cas1_taux_uniforme_fige(self):
        """6 h CM figées à 1000 → 6000 MRU (pas 7200 au tarif courant 1200)."""
        for _ in range(3):
            self._add_vacation(duree=2.0, taux_fige=1000.0)

        resp = self.client.get(URL, {'prof': self.prof.pk, 'annee_univ': ANNEE})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        cm = _cm_row(resp.data)
        self.assertIsNotNone(cm, 'Ligne CM absente du résumé.')
        self.assertEqual(cm['heures'], 6.0)
        self.assertEqual(cm['montant'], 6000.0)   # taux figé 1000, pas 1200
        self.assertEqual(cm['taux'], 1000.0)
        self.assertEqual(resp.data['total_montant'], 6000.0)

        # Garde-fou : le tarif courant est bien 1200 → l'ancien code aurait
        # donné 7200. La divergence est donc bien éliminée par le correctif.
        self.assertEqual(Paiement.get_taux_at('CM'), 1200.0)

    def test_cas2_taux_mixtes_somme_par_ligne(self):
        """4 h à 1000 + 2 h à 1200 → 6400 (somme par ligne), pas 6h×tarif unique."""
        self._add_vacation(duree=2.0, taux_fige=1000.0)
        self._add_vacation(duree=2.0, taux_fige=1000.0)
        self._add_vacation(duree=2.0, taux_fige=1200.0)

        resp = self.client.get(URL, {'prof': self.prof.pk, 'annee_univ': ANNEE})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

        cm = _cm_row(resp.data)
        self.assertEqual(cm['heures'], 6.0)
        self.assertEqual(cm['montant'], 6400.0)            # 4*1000 + 2*1200
        self.assertEqual(cm['taux'], round(6400.0 / 6.0, 2))  # taux effectif pondéré
