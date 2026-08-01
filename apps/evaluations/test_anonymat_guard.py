"""
Test — garde-fou régénération d'anonymat.

Régénérer les anonymats après le début de la saisie réassigne l'association
numéro↔étudiant et provoque des notes mal attribuées. Le service BLOQUE la
régénération dès qu'une note existe pour la session (sauf override `force`).

Base SQLite en mémoire (siga.settings.test).
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APITestCase

from apps.parametres.models import Institution, Year, Niveau, Semestre
from apps.scolarite.models import Filiere
from apps.departement.models import Departement
from apps.absence.models import Etudiant
from apps.em.models import EM
from apps.inscriptions.models import (
    InscriptionAdministrative, InscriptionPedagogique, InscriptionElement,
)
from apps.evaluations.models import SessionEvaluation, Note
from apps.evaluations.services.anonymat import AnonymatService


class AnonymatGuardTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Test', est_principale=True)
        cls.year = Year.objects.create(annee='2025-2026', est_active=True)
        cls.niveau = Niveau.objects.create(niveau='L1')
        cls.fil = Filiere.objects.create(code='LP', intitule_fr='LP', institution=cls.inst)
        cls.dept = Departement.objects.create(nom='G1', institution=cls.inst, filiere=cls.fil, niveau=cls.niveau)
        cls.sem = Semestre.objects.create(code_semestre='S1', semestre='S1', niveau_semestre=cls.niveau, type_semestre='I')
        cls.em = EM.objects.create(code_em='X1', intitule='X', departement=cls.dept, semestre=cls.sem, institution=cls.inst)

        cls.session = SessionEvaluation.objects.create(
            annee_univ=cls.year, institution=cls.inst,
            type_session='normale', type_semestre='Impairs', est_ouverte=True)

        etu = Etudiant.objects.create(matricule='E1', nom='Etu', departement=cls.dept, genre='M')
        adm = InscriptionAdministrative.objects.create(
            etudiant=etu, annee_univ=cls.year, filiere=cls.fil, institution=cls.inst,
            niveau=1, numero_inscription='INS-1')
        ped = InscriptionPedagogique.objects.create(inscription_admin=adm, semestre=cls.sem)
        cls.ie = InscriptionElement.objects.create(inscription_ped=ped, em=cls.em)

    def test_regeneration_bloquee_si_notes_existent(self):
        # 1) Génère les anonymats (1 étudiant couvert)
        n = AnonymatService.generer(self.session, regenerer=False)
        self.assertEqual(n, 1)

        # 2) Une note est saisie pour la session
        Note.objects.create(inscription_element=self.ie, session=self.session,
                            type_note='EXAM', valeur=Decimal('12'))

        # 3) Régénérer → BLOQUÉ (ValueError)
        with self.assertRaises(ValueError):
            AnonymatService.generer(self.session, regenerer=True)

        # 4) force=True (admin) → autorisé
        n2 = AnonymatService.generer(self.session, regenerer=True, force=True)
        self.assertEqual(n2, 1)

    def test_regeneration_ok_sans_notes(self):
        AnonymatService.generer(self.session, regenerer=False)
        # Aucune note → régénération autorisée
        n = AnonymatService.generer(self.session, regenerer=True)
        self.assertEqual(n, 1)

    def test_session_cloturee_bloque(self):
        self.session.est_close = True
        self.session.save(update_fields=['est_close'])
        with self.assertRaises(ValueError):
            AnonymatService.generer(self.session, regenerer=False)


class AnonymatAuditTest(APITestCase):
    """L'endpoint de (ré)génération écrit un AuditLog (qui/quand/session)."""

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Test', est_principale=True)
        cls.year = Year.objects.create(annee='2025-2026', est_active=True)
        cls.session = SessionEvaluation.objects.create(
            annee_univ=cls.year, institution=cls.inst,
            type_session='normale', type_semestre='Impairs', est_ouverte=True)
        cls.admin = get_user_model().objects.create_user(
            username='admin_anon', email='a@test.local', password='Xk93!plqz72', role='admin')

    def test_generation_ecrit_audit(self):
        from core.models import AuditLog
        self.client.force_authenticate(user=self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(f'/api/v1/evaluations/anonymats/generer/?session={self.session.pk}')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(
            AuditLog.objects.filter(model_name='AnonymatSession', object_id=str(self.session.pk)).exists(),
            "La (ré)génération d'anonymat doit être tracée dans l'audit.",
        )
