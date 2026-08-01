"""
Test P2 — audit des toggles RBAC (octroi/retrait de droits par rôle).

UserPermission/RoleDefault ne sont pas dans TRACKED_MODELS → audit explicite
dans les vues de toggle.

write_audit via transaction.on_commit → captureOnCommitCallbacks(execute=True).
Base SQLite en mémoire (siga.settings.test) — aucun contact gesafped26.
"""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.authentication.models import Module, Action, ModuleAction
from core.models import AuditLog

User = get_user_model()


class AuditRbacToggleTest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username='admin_rbac', email='rbac@test.local',
            password='Xk93!plqz72', role='admin',
        )
        m = Module.objects.create(code='doc_attestation', nom='Attestations')
        a = Action.objects.create(code='voir', nom='Voir')
        cls.ma = ModuleAction.objects.create(module=m, action=a)

    def setUp(self):
        self.client.force_authenticate(user=self.admin)

    def test_role_toggle_cree_audit(self):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post('/api/v1/auth/rbac/role-toggle/',
                                  {'role': 'DE', 'ma_id': self.ma.pk}, format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)

        log = AuditLog.objects.filter(model_name='RoleDefault', action='UPDATE').first()
        self.assertIsNotNone(log, "Aucune ligne d'audit pour le toggle de rôle RBAC")
        self.assertEqual(log.changes.get('role'), 'DE')

    def test_user_toggle_cree_audit(self):
        cible = User.objects.create_user(
            username='cible_rbac', email='cible_rbac@test.local',
            password='Xk93!plqz72', role='etudiant',
        )
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post('/api/v1/auth/rbac/user-toggle/',
                                  {'user_id': cible.pk, 'ma_id': self.ma.pk, 'state': 'on'},
                                  format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)

        log = AuditLog.objects.filter(model_name='UserPermission', object_id=str(cible.pk)).first()
        self.assertIsNotNone(log, "Aucune ligne d'audit pour le toggle utilisateur RBAC")
