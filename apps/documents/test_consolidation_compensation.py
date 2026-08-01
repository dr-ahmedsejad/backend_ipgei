"""
Test — calculer_resultat_semestre_consolide (source unique relevé + consultation).

Vérifie que la compensation (Art. 13/14/15) est bien appliquée : un EM dont la
moyenne est < 10 mais validé par COMPENSATION (module/semestre validé → VCI/VCS)
compte quand même ses crédits, et sa décision est 'Validé'. C'est ce que la
consultation écran (notes_etudiant) ne faisait PAS avant le branchement sur ce
helper (bug 22640 : crédits compensés non comptés).

Runner = pytest :
  .venv/Scripts/python.exe -m pytest apps/documents/test_consolidation_compensation.py
"""
from decimal import Decimal

from django.test import TestCase

from apps.parametres.models import Institution, Year, Niveau, Semestre
from apps.scolarite.models import Filiere
from apps.departement.models import Departement
from apps.absence.models import Etudiant
from apps.em.models import EM
from apps.modules.models import Module
from apps.inscriptions.models import (
    InscriptionAdministrative, InscriptionPedagogique, InscriptionElement,
)
from apps.evaluations.models import SessionEvaluation, Note
from apps.documents.services import calculer_resultat_semestre_consolide


class CompensationConsolideTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst   = Institution.objects.create(acronyme='TST', nom='T', est_principale=True)
        cls.year   = Year.objects.create(annee='2023-2024')
        cls.niveau = Niveau.objects.create(niveau='1')
        cls.sem    = Semestre.objects.create(code_semestre='S1', semestre='Semestre 1',
                                             niveau_semestre=cls.niveau, type_semestre='I', credits=30)
        cls.fil    = Filiere.objects.create(code='LP', intitule_fr='LP', institution=cls.inst)
        cls.dept   = Departement.objects.create(nom='G', institution=cls.inst)
        cls.mod    = Module.objects.create(code='M01', intitule_fr='Mod', semestre=cls.sem,
                                           filiere=cls.fil, institution=cls.inst, credits=20, coefficient=2)
        # 2 EM, 10 crédits chacun, sans TP → ME = (CC×2 + EXAM×3) / 5
        cls.em1 = EM.objects.create(code_em='E1', intitule='E1', departement=cls.dept, semestre=cls.sem,
                                    institution=cls.inst, module_lmd=cls.mod, credits=10, coefficient=1)
        cls.em2 = EM.objects.create(code_em='E2', intitule='E2', departement=cls.dept, semestre=cls.sem,
                                    institution=cls.inst, module_lmd=cls.mod, credits=10, coefficient=1)
        cls.etu = Etudiant.objects.create(matricule='C1', nom='C', departement=cls.dept, genre='M')
        adm = InscriptionAdministrative.objects.create(etudiant=cls.etu, annee_univ=cls.year, filiere=cls.fil,
                                                       institution=cls.inst, niveau=1, numero_inscription='I1')
        ped = InscriptionPedagogique.objects.create(inscription_admin=adm, semestre=cls.sem)
        ie1 = InscriptionElement.objects.create(inscription_ped=ped, em=cls.em1)
        ie2 = InscriptionElement.objects.create(inscription_ped=ped, em=cls.em2)
        cls.sess = SessionEvaluation.objects.create(institution=cls.inst, annee_univ=cls.year,
                                                    type_session='normale', type_semestre='Impairs', est_close=True)
        # EM1 : CC=14, EXAM=14 → ME=14 (validé). EM2 : CC=8, EXAM=8 → ME=8 (< 10).
        for ie, (cc, ex) in [(ie1, (14, 14)), (ie2, (8, 8))]:
            Note.objects.create(inscription_element=ie, session=cls.sess, type_note='CC',   valeur=Decimal(str(cc)))
            Note.objects.create(inscription_element=ie, session=cls.sess, type_note='EXAM', valeur=Decimal(str(ex)))

    def test_em_compense_compte_ses_credits(self):
        """Module validé (moyenne 11) → l'EM à 8/20 est compensé (VCI) : ses crédits comptent."""
        res = calculer_resultat_semestre_consolide(self.etu, self.sem, self.year)
        self.assertTrue(res['est_admis'], "Semestre MGS=11, MM=11 → doit être admis.")
        self.assertEqual(
            res['credits_valides'], 20,
            "L'EM compensé (8/20) doit acquérir ses crédits → 20, pas 10.",
        )
        e2 = next(e for m in res['modules'] for e in m['elements'] if e['code'] == 'E2')
        self.assertEqual(e2['me'], 8.0)
        self.assertEqual(
            e2['decision'], 'Validé',
            "La décision de l'EM compensé doit être propagée à 'Validé' (VCI).",
        )

    def test_deliberation_applique_rattrapage_meme_session_ouverte(self):
        """La délibération (calculer_moyenne_annuelle) applique max(SN, SR) MÊME si la
        session de rattrapage n'est PAS clôturée — alignée sur le relevé (demande métier).
        L'avertissement « provisoire » est porté à part (pv_diagnostic)."""
        from apps.evaluations.services.calcul_notes import NoteCalculService

        # Sans rattrapage : moyenne S1 = (E1=14 + E2=8) / 2 = 11.00
        moy0, cr0, _ = NoteCalculService.calculer_moyenne_annuelle(self.etu, self.year, 1)
        self.assertEqual(float(moy0), 11.00)

        # Session de rattrapage Impairs OUVERTE (est_close=False) + meilleur EXAM pour E2.
        sr = SessionEvaluation.objects.create(
            institution=self.inst, annee_univ=self.year,
            type_session='rattrapage', type_semestre='Impairs', est_close=False)
        ie2 = InscriptionElement.objects.get(em=self.em2)
        # E2 au rattrapage : ME = (CC×2 + EXAM_rat×3)/5 = (8×2 + 14×3)/5 = 11.6 > 8
        Note.objects.create(inscription_element=ie2, session=sr, type_note='EXAM', valeur=Decimal('14'))

        moy1, cr1, _ = NoteCalculService.calculer_moyenne_annuelle(self.etu, self.year, 1)
        self.assertGreater(
            float(moy1), float(moy0),
            "max(SN, SR) doit améliorer la moyenne MÊME avec une session rattrapage ouverte.")
        # (E1=14 + E2=11.6) / 2 = 12.80
        self.assertEqual(float(moy1), 12.80)
