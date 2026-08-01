"""
Tests de non-régression — correctif A (élévation de privilège self-service profil).

Vérifie :
  1. Un utilisateur non-admin ne peut PAS changer son rôle via PATCH /auth/profil/.
  2. Un admin PEUT toujours changer le rôle d'un utilisateur via /auth/users/<id>/
     (on ne casse pas la gestion admin des rôles).

Exécution :  python manage.py test apps.authentication.tests_security_fixes
Base de test jetable — aucune donnée de production touchée.
"""
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

User = get_user_model()

_PWD = 'Xk93!plqz72'  # mot de passe bidon, jamais persisté hors base de test


class ProfilPrivilegeEscalationTest(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='etudiant_test', email='etu@test.local',
            password=_PWD, role='etudiant',
        )

    def test_self_service_profil_ne_peut_pas_changer_role(self):
        """A : PATCH /auth/profil/ {role:admin} ne doit PAS promouvoir l'utilisateur."""
        self.client.force_authenticate(user=self.user)
        resp = self.client.patch(
            reverse('profil'),
            {'role': 'admin', 'name': 'Hacker'},
            format='json',
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(
            self.user.role, 'etudiant',
            "FAILLE A NON CORRIGÉE : le rôle a été élevé à admin via le self-service profil.",
        )
        # Le champ légitime (name) reste bien modifiable.
        self.assertEqual(self.user.name, 'Hacker')

    def test_admin_peut_toujours_changer_role(self):
        """Non-régression : l'admin garde la main sur les rôles via UserViewSet."""
        admin = User.objects.create_user(
            username='admin_test', email='admin@test.local',
            password=_PWD, role='admin',
        )
        cible = User.objects.create_user(
            username='cible_test', email='cible@test.local',
            password=_PWD, role='etudiant',
        )
        self.client.force_authenticate(user=admin)
        resp = self.client.patch(
            reverse('users-detail', args=[cible.pk]),
            {'role': 'DE'},
            format='json',
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        cible.refresh_from_db()
        self.assertEqual(
            cible.role, 'DE',
            "RÉGRESSION : l'admin ne peut plus changer les rôles via UserViewSet.",
        )
