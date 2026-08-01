"""
Test — audit des clôtures/réouvertures de SESSION (vrai trou : SessionEvaluation
n'est pas dans TRACKED_MODELS de core/signals.py).

NB : les PV/Note/Rachat/LigneDeliberation sont déjà audités automatiquement par
les signaux (core/signals.py) → pas besoin d'audit explicite pour eux.

write_audit via transaction.on_commit → captureOnCommitCallbacks(execute=True).
Base SQLite en mémoire (siga.settings.test) — aucun contact gesafped26.
"""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.parametres.models import Year, Institution
from apps.evaluations.models import SessionEvaluation
from core.models import AuditLog

User = get_user_model()


class AuditClotureSessionTest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test', est_principale=True)
        cls.year = Year.objects.create(annee='2024-2025')
        cls.admin = User.objects.create_user(
            username='admin_audit', email='admin_audit@test.local',
            password='Xk93!plqz72', role='admin',
        )

    def setUp(self):
        self.client.force_authenticate(user=self.admin)

    def _session(self, **kw):
        return SessionEvaluation.objects.create(
            institution=self.inst, annee_univ=self.year,
            type_session='normale', type_semestre='Impairs', **kw,
        )

    def test_cloture_session_audit_et_effet(self):
        s = self._session(est_close=False, est_ouverte=True)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(f'/api/v1/evaluations/sessions/{s.pk}/cloturer/')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)

        s.refresh_from_db()
        self.assertTrue(s.est_close, "Effet métier cassé : la session n'est pas clôturée")

        log = AuditLog.objects.filter(
            model_name='SessionEvaluation', object_id=str(s.pk), action='UPDATE',
        ).first()
        self.assertIsNotNone(log, "Aucune ligne d'audit pour la clôture de session")
        self.assertEqual(log.changes.get('est_close'), {'old': False, 'new': True})

    def test_reouverture_session_audit_et_effet(self):
        s = self._session(est_close=True, est_ouverte=False)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(f'/api/v1/evaluations/sessions/{s.pk}/rouvrir/')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)

        s.refresh_from_db()
        self.assertFalse(s.est_close, "Effet métier cassé : la session n'est pas réouverte")
        self.assertTrue(
            AuditLog.objects.filter(model_name='SessionEvaluation', object_id=str(s.pk)).exists(),
            "Aucune ligne d'audit pour la réouverture de session",
        )
