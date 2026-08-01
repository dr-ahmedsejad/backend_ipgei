"""
Test de l'endpoint de contrôle d'accès /media/ (Nginx auth_request).

Un anonyme est refusé (401/403) ; un utilisateur authentifié obtient 204.
C'est ce que Nginx interroge avant de servir un fichier /media/ → empêche
le téléchargement non authentifié des justificatifs / diplômes / PII.

Base SQLite en mémoire (siga.settings.test).
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

User = get_user_model()

URL = '/internal/media-auth/'


class MediaAuthTest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username='media_user', email='media@test.local', password='x', role='etudiant',
        )

    def test_anonyme_refuse(self):
        resp = self.client.get(URL)
        self.assertIn(resp.status_code, (401, 403))

    def test_authentifie_autorise(self):
        self.client.force_authenticate(self.user)
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 204)
