"""
Tests — audit des 4 éléments ajoutés à la demande :
  - Paiement (taux)           → signal (TRACKED_MODELS)
  - DerogationMedicale (stage)→ signal (TRACKED_MODELS)
  - PeriodeReclamation        → signal (TRACKED_MODELS)
  - SessionEvaluation create/delete → write_audit explicite (viewset)

write_audit/_safe via transaction.on_commit → captureOnCommitCallbacks(execute=True).
Base SQLite en mémoire (siga.settings.test) — aucun contact gesafped26.
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.parametres.models import Year, Institution, Paiement
from apps.departement.models import Departement
from apps.absence.models import Etudiant
from apps.stages.models import DerogationMedicale
from apps.reclamations.models import PeriodeReclamation
from apps.evaluations.models import SessionEvaluation
from core.models import AuditLog

User = get_user_model()


class AuditGapsBatchTest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test', est_principale=True)
        cls.year = Year.objects.create(annee='2024-2025')
        cls.dept = Departement.objects.create(nom='Dept Test', institution=cls.inst)
        cls.etu = Etudiant.objects.create(
            matricule='ETU-G', nom='Etudiant Gap', departement=cls.dept, genre='M',
        )
        cls.admin = User.objects.create_user(
            username='admin_gaps', email='gaps@test.local',
            password='Xk93!plqz72', role='admin',
        )

    def setUp(self):
        self.client.force_authenticate(user=self.admin)

    # ── Signaux (TRACKED_MODELS) ───────────────────────────────────────────────

    def test_paiement_signal_audit(self):
        with self.captureOnCommitCallbacks(execute=True):
            p = Paiement.objects.create(type='CM', taux=1000.0, date_debut=date(2025, 1, 1))
        self.assertTrue(
            AuditLog.objects.filter(model_name='Paiement', object_id=str(p.pk), action='CREATE').exists(),
            "Pas d'audit pour la création d'un taux de paiement",
        )

    def test_derogation_medicale_signal_audit(self):
        with self.captureOnCommitCallbacks(execute=True):
            d = DerogationMedicale.objects.create(
                etudiant=self.etu, motif='Test', date_debut=date(2025, 1, 1), date_fin=date(2025, 1, 15),
            )
        self.assertTrue(
            AuditLog.objects.filter(model_name='DerogationMedicale', object_id=str(d.pk), action='CREATE').exists(),
            "Pas d'audit pour la création d'une dérogation médicale",
        )

    def test_periode_reclamation_signal_audit(self):
        now = timezone.now()
        with self.captureOnCommitCallbacks(execute=True):
            pr = PeriodeReclamation.objects.create(
                annee_univ=self.year, type_semestre='I', institution=self.inst,
                date_ouverture=now, date_fermeture=now,
            )
        self.assertTrue(
            AuditLog.objects.filter(model_name='PeriodeReclamation', object_id=str(pr.pk), action='CREATE').exists(),
            "Pas d'audit pour la création d'une période de réclamation",
        )

    # ── SessionEvaluation : création + suppression (write_audit explicite) ──────

    def test_session_create_then_delete_audit(self):
        # CREATE
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post('/api/v1/evaluations/sessions/', {
                'institution': self.inst.pk, 'annee_univ': self.year.pk,
                'type_session': 'normale', 'type_semestre': 'Impairs',
            }, format='json')
        self.assertIn(r.status_code, (status.HTTP_201_CREATED, status.HTTP_200_OK), r.data)
        sid = r.data['id']
        self.assertTrue(
            AuditLog.objects.filter(model_name='SessionEvaluation', object_id=str(sid), action='CREATE').exists(),
            "Pas d'audit pour la création de session",
        )

        # DELETE
        with self.captureOnCommitCallbacks(execute=True):
            rd = self.client.delete(f'/api/v1/evaluations/sessions/{sid}/')
        self.assertEqual(rd.status_code, status.HTTP_204_NO_CONTENT, getattr(rd, 'data', None))
        self.assertTrue(
            AuditLog.objects.filter(model_name='SessionEvaluation', object_id=str(sid), action='DELETE').exists(),
            "Pas d'audit pour la suppression de session",
        )
