"""
Non-régression de l'export PDF du PV de délibération.

Garde-fou avant/après l'extraction de PVDeliberationViewSet.pdf vers
services/pv_export.build_pv_pdf. wkhtmltopdf (binaire) n'étant pas requis en
test, on mocke pdfkit.configuration / from_string : tout le reste (rendu du
template HTML, contexte, jury, réponse) est exercé réellement.

Base SQLite en mémoire (siga.settings.test).
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework.test import APITestCase

from apps.prof.models import ProfTypeHistory
from tests.factories.deliberation import PVDeliberationSemestrielFactory

User = get_user_model()


class PVPdfExportTest(APITestCase):
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
            username='admin_pdf', email='admin_pdf@test.local',
            password='x', role='admin',
        )
        cls.pv = PVDeliberationSemestrielFactory()

    @patch('pdfkit.from_string', return_value=b'%PDF-1.4 fake-pv')
    @patch('pdfkit.configuration', return_value=object())
    def test_pdf_export_renvoie_pdf(self, mock_conf, mock_from_string):
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f'/api/v1/evaluations/pvs/{self.pv.id}/pdf/')

        self.assertEqual(resp.status_code, 200, getattr(resp, 'content', b'')[:400])
        self.assertEqual(resp['Content-Type'], 'application/pdf')
        self.assertTrue(resp.content.startswith(b'%PDF'))
        mock_from_string.assert_called_once()
