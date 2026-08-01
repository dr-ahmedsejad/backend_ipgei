"""
Tests P1 (fin) — audit des dérogations et de la pondération.

  - POST dérogation → ligne d'audit CREATE.
  - PATCH pondération → coeffs changés (effet) + ligne d'audit old/new.

write_audit via transaction.on_commit → captureOnCommitCallbacks(execute=True).
Base SQLite en mémoire (siga.settings.test) — aucun contact gesafped26.
"""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.parametres.models import Year, Institution
from apps.departement.models import Departement
from apps.absence.models import Etudiant
from apps.scolarite.models import ParametresPonderation
from core.models import AuditLog

User = get_user_model()


class AuditDerogationPonderationTest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test', est_principale=True)
        cls.year = Year.objects.create(annee='2024-2025')
        cls.dept = Departement.objects.create(nom='Dept Test', institution=cls.inst)
        cls.etu = Etudiant.objects.create(
            matricule='ETU-D', nom='Etudiant Derog', departement=cls.dept, genre='M',
        )
        cls.admin = User.objects.create_user(
            username='admin_p1f', email='p1f@test.local',
            password='Xk93!plqz72', role='admin',
        )

    def setUp(self):
        self.client.force_authenticate(user=self.admin)

    def test_derogation_create_audit(self):
        with self.captureOnCommitCallbacks(execute=True):
            # 'derogation_inscription' n'exige pas de justificatif (≠ année blanche).
            r = self.client.post('/api/v1/inscriptions/derogations/', {
                'etudiant': self.etu.pk, 'annee_univ': self.year.pk, 'institution': self.inst.pk,
                'type_derogation': 'derogation_inscription', 'motif': 'Dérogation (test)',
                'date_decision': '2025-01-15',
            }, format='json')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)

        log = AuditLog.objects.filter(model_name='Derogation', action='CREATE').first()
        self.assertIsNotNone(log, "Aucune ligne d'audit pour la dérogation")
        self.assertEqual(log.changes.get('type_derogation'), 'derogation_inscription')

    def test_ponderation_update_audit_et_effet(self):
        p = ParametresPonderation.get()  # singleton, défaut coeff_cc=2
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.patch(
                f'/api/v1/scolarite/parametres-ponderation/{p.pk}/',
                {'coeff_cc': 3}, format='json',
            )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)

        p.refresh_from_db()
        self.assertEqual(p.coeff_cc, 3, "Effet métier cassé : le coefficient n'a pas changé")

        log = AuditLog.objects.filter(model_name='ParametresPonderation', action='UPDATE').first()
        self.assertIsNotNone(log, "Aucune ligne d'audit pour la pondération")
        self.assertEqual(log.changes.get('coeff_cc'), {'old': 2, 'new': 3})
