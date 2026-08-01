"""
Non-régression du diagnostic READ-ONLY des sessions d'un PV annuel.

Garde-fou avant/après extraction de PVDeliberationViewSet.diagnostic_sessions
vers services/pv_diagnostic.build_pv_diagnostic. Endpoint JSON pur (aucune
écriture en base) → pas de mock nécessaire.

Base SQLite en mémoire (siga.settings.test).
"""
from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework.test import APITestCase

from apps.prof.models import ProfTypeHistory
from tests.factories.deliberation import PVDeliberationAnnuelFactory

User = get_user_model()


class PVDiagnosticTest(APITestCase):
    @classmethod
    def setUpClass(cls):
        # create_model plante la transaction PG si la table existe deja (creee par
        # la fixture session de tests/conftest.py) : verifier l'existence AVANT
        # d'entrer dans le schema_editor est vendor-neutre (sqlite ET postgresql).
        if ProfTypeHistory._meta.db_table not in connection.introspection.table_names():
            with connection.schema_editor() as se:
                se.create_model(ProfTypeHistory)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username='admin_diag', email='admin_diag@test.local',
            password='x', role='admin',
        )
        cls.pv = PVDeliberationAnnuelFactory()

    def test_diagnostic_sessions_renvoie_json(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f'/api/v1/evaluations/pvs/{self.pv.id}/diagnostic-sessions/')

        self.assertEqual(resp.status_code, 200, getattr(resp, 'content', b'')[:400])
        data = resp.json()
        self.assertEqual(data['pv_id'], self.pv.id)
        self.assertEqual(data['type_pv'], 'annuel')
        self.assertIn('etudiants', data)
        self.assertIn('warnings', data)
