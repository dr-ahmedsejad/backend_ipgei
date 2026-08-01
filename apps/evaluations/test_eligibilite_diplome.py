"""
Tests de non-régression — correctifs B et C sur
NoteCalculService.verifier_eligibilite_diplome.

  B : la méthode accepte le paramètre `annee_univ` (plus de TypeError lors de la
      délibération annuelle Ingénieur niveau 3 — Art. 16 Décret 2018-070).
  C : le décompte des 180 crédits ne double-compte plus les sessions normale +
      rattrapage d'un même semestre (sinon 180 → 360 → diplôme accordé à tort).

Exécution :  python manage.py test apps.evaluations.tests_eligibilite_diplome
Base de test jetable — aucune donnée de production touchée.
"""
from decimal import Decimal

from django.test import TestCase

from apps.parametres.models import Year, Niveau, Semestre, Institution
from apps.departement.models import Departement
from apps.scolarite.models import Filiere
from apps.absence.models import Etudiant
from apps.inscriptions.models import InscriptionAdministrative, InscriptionPedagogique
from apps.evaluations.models import SessionEvaluation, ResultatSemestre
from apps.evaluations.services.calcul_notes import NoteCalculService


class VerifierEligibiliteDiplomeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(
            acronyme='TST', nom='Institut Test', est_principale=True,
        )
        cls.year = Year.objects.create(annee='2024-2025')
        cls.niveau = Niveau.objects.create(niveau='1')
        cls.semestre = Semestre.objects.create(
            code_semestre='S1', semestre='Semestre 1',
            niveau_semestre=cls.niveau, type_semestre='I', credits=30,
        )
        cls.dept = Departement.objects.create(nom='Dept Test', institution=cls.inst)
        cls.filiere = Filiere.objects.create(
            code='LPTEST', intitule_fr='Licence Test',
            type_diplome='LP', institution=cls.inst,
        )
        cls.etudiant = Etudiant.objects.create(
            matricule='ETU-TEST-1', nom='Etudiant Test',
            departement=cls.dept, genre='M', filiere=cls.filiere,
        )
        cls.insc_admin = InscriptionAdministrative.objects.create(
            etudiant=cls.etudiant, annee_univ=cls.year, filiere=cls.filiere,
            institution=cls.inst, niveau=1, numero_inscription='INSC-TEST-1',
        )
        cls.insc_ped = InscriptionPedagogique.objects.create(
            inscription_admin=cls.insc_admin, semestre=cls.semestre,
        )

        # Deux sessions CLÔTURÉES (normale + rattrapage) pour la MÊME parité/année.
        cls.session_sn = SessionEvaluation.objects.create(
            institution=cls.inst, annee_univ=cls.year,
            type_session='normale', type_semestre='Impairs', est_close=True,
        )
        cls.session_sr = SessionEvaluation.objects.create(
            institution=cls.inst, annee_univ=cls.year,
            type_session='rattrapage', type_semestre='Impairs', est_close=True,
        )

        # Le MÊME semestre (insc_ped) a un ResultatSemestre dans CHAQUE session,
        # tous deux est_admis avec 30 crédits. L'ancien code sommait 30 + 30 = 60
        # (double-comptage). Le code corrigé consolide à un seul RS → 30.
        ResultatSemestre.objects.create(
            inscription_ped=cls.insc_ped, session=cls.session_sn,
            moyenne=Decimal('12.00'), credits_valides=30, est_admis=True, code_statut='V',
        )
        ResultatSemestre.objects.create(
            inscription_ped=cls.insc_ped, session=cls.session_sr,
            moyenne=Decimal('12.00'), credits_valides=30, est_admis=True, code_statut='V',
        )

    def test_B_accepte_annee_univ_sans_typeerror(self):
        """B : l'appel avec annee_univ ne lève plus TypeError."""
        try:
            res = NoteCalculService.verifier_eligibilite_diplome(
                etudiant=self.etudiant, annee_univ=self.year,
            )
        except TypeError as exc:
            self.fail(f"B NON CORRIGÉ : verifier_eligibilite_diplome refuse annee_univ → {exc}")
        self.assertIsInstance(res, dict)
        self.assertIn('credits_valides', res)

    def test_C_pas_de_double_comptage_sn_sr(self):
        """C : SN + SR d'un même semestre = 30 crédits consolidés, pas 60."""
        res = NoteCalculService.verifier_eligibilite_diplome(etudiant=self.etudiant)
        self.assertEqual(
            res['credits_valides'], 30,
            f"BUG C : double-comptage SN+SR → crédits = {res['credits_valides']} au lieu de 30.",
        )

    # ── Correctif D : note S6 lue depuis le ResultatSemestre consolidé ────────
    # Art. 25 Arrêté 562 (moyenne >= 12 au 6e semestre) / Art. 16 Décret
    # 2018-070 (PFE >= 12). Avant correctif, la vérification portait sur la
    # moyenne ANNUELLE du PV niveau 3 (S5+S6 mélangés).

    def _creer_s6(self, moyenne):
        """Crée S6 + inscription pédagogique + ResultatSemestre consolidé."""
        niveau3 = Niveau.objects.create(niveau='3')
        sem6 = Semestre.objects.create(
            code_semestre='S6', semestre='Semestre 6',
            niveau_semestre=niveau3, type_semestre='P', credits=30,
        )
        ip6 = InscriptionPedagogique.objects.create(
            inscription_admin=self.insc_admin, semestre=sem6,
        )
        session_pn = SessionEvaluation.objects.create(
            institution=self.inst, annee_univ=self.year,
            type_session='normale', type_semestre='Pairs', est_close=True,
        )
        ResultatSemestre.objects.create(
            inscription_ped=ip6, session=session_pn,
            moyenne=Decimal(str(moyenne)), credits_valides=30,
            est_admis=True, code_statut='V',
        )

    def test_D_s6_insuffisant_bloque_pfe(self):
        """D : moyenne S6 = 11.50 < 12 → pfe_valide=False + motif Art. 25/16."""
        self._creer_s6('11.50')
        res = NoteCalculService.verifier_eligibilite_diplome(etudiant=self.etudiant)
        self.assertEqual(res['note_s6'], Decimal('11.50'))
        self.assertFalse(
            res['pfe_valide'],
            "BUG D : S6 à 11.50/20 devrait invalider la condition diplôme (seuil 12).",
        )
        self.assertTrue(
            any('S6' in m for m in res['motifs']),
            f"Motif S6 manquant : {res['motifs']}",
        )

    def test_D_s6_suffisant_valide_pfe(self):
        """D : moyenne S6 = 13.00 >= 12 → pfe_valide=True."""
        self._creer_s6('13.00')
        res = NoteCalculService.verifier_eligibilite_diplome(etudiant=self.etudiant)
        self.assertEqual(res['note_s6'], Decimal('13.00'))
        self.assertTrue(
            res['pfe_valide'],
            "BUG D : S6 à 13.00/20 devrait satisfaire la condition diplôme.",
        )
