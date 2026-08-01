"""
Non-régression du rapport PDF de progression d'un PV annuel.

Garde-fou avant/après extraction de PVDeliberationViewSet.rapport_progression
vers services/pv_export.build_pv_rapport_progression. pdfkit est mocké (le
binaire wkhtmltopdf n'est pas requis) : tout le reste est exercé réellement.

Base SQLite en mémoire (siga.settings.test).
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework.test import APITestCase

from apps.prof.models import ProfTypeHistory
from tests.factories.deliberation import PVDeliberationAnnuelFactory

User = get_user_model()


class PVRapportProgressionTest(APITestCase):
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
            username='admin_rap', email='admin_rap@test.local',
            password='x', role='admin',
        )
        # PV ANNUEL : le rapport de progression n'existe que pour ce type.
        cls.pv = PVDeliberationAnnuelFactory()

    @patch('pdfkit.from_string', return_value=b'%PDF-1.4 fake-rap')
    @patch('pdfkit.configuration', return_value=object())
    def test_rapport_progression_renvoie_pdf(self, mock_conf, mock_from_string):
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f'/api/v1/evaluations/pvs/{self.pv.id}/rapport-progression/')

        self.assertEqual(resp.status_code, 200, getattr(resp, 'content', b'')[:400])
        self.assertEqual(resp['Content-Type'], 'application/pdf')
        self.assertTrue(resp.content.startswith(b'%PDF'))
        mock_from_string.assert_called_once()
