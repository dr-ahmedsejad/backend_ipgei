"""
Attribution des diplômes — alimentation du RegistreDiplome (jusque-là jamais peuplé).
  - numéro officiel DLP/DNI + offset (generer_numero_diplome) ;
  - attribution idempotente, append-only, gate FIN DE CYCLE (attribuer_diplomes_pv).

L'éligibilité (180 crédits + note finale) et la moyenne du cycle passent par le
moteur du relevé (consolidation) → on mocke ces helpers pour tester la LOGIQUE
d'attribution de façon déterministe.

Runner : pytest.
  .venv/Scripts/python.exe -m pytest apps/documents/test_attribution_diplome.py
"""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from apps.parametres.models import Year, Institution
from apps.departement.models import Departement
from apps.scolarite.models import Filiere
from apps.absence.models import Etudiant
from apps.inscriptions.models import InscriptionAdministrative
from apps.evaluations.models import PVDeliberation, LigneDeliberation
from apps.evaluations.services.deliberation_annuelle import DeliberationAnnuelleService
from apps.documents.models import RegistreDiplome
from apps.documents import services as docsvc


class NumeroDiplomeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(
            acronyme='ISS', nom='Inst', est_principale=True, diplome_sequence_debut=500)
        cls.dept = Departement.objects.create(nom='D', institution=cls.inst)
        cls.lp = Filiere.objects.create(
            code='SDID', intitule_fr='SDID', type_diplome='LP',
            institution=cls.inst, niveau_debut=1, niveau_fin=3)
        cls.ing = Filiere.objects.create(
            code='GINF', intitule_fr='Génie Info', type_diplome='ING',
            institution=cls.inst, niveau_debut=1, niveau_fin=3)
        cls.etu = Etudiant.objects.create(
            matricule='E1', nom='X', genre='M', departement=cls.dept, filiere=cls.lp)

    def _registre(self, numero, fil):
        return RegistreDiplome.objects.create(
            institution=self.inst, etudiant=self.etu, filiere=fil,
            numero_diplome=numero, mention='Bien', moyenne_generale=Decimal('14'),
            date_delivrance=date(2026, 6, 29), annee_universitaire='2025-2026')

    def test_prefixe_dlp_offset(self):
        """LP → DLP ; aucun existant → démarre à l'offset (500), pas à 1."""
        self.assertEqual(
            docsvc.generer_numero_diplome(self.inst, self.lp, '2025-2026'),
            'DLP-ISS-2026-0500')

    def test_prefixe_dni_ingenieur(self):
        """ING → DNI."""
        self.assertEqual(
            docsvc.generer_numero_diplome(self.inst, self.ing, '2025-2026'),
            'DNI-ISS-2026-0500')

    def test_sequence_incremente(self):
        """Repli acronyme : un diplôme existant à 0500 → le suivant est 0501."""
        self._registre('DLP-ISS-2026-0500', self.lp)
        self.assertEqual(
            docsvc.generer_numero_diplome(self.inst, self.lp, '2025-2026'),
            'DLP-ISS-2026-0501')

    # ── Schéma avec code d'établissement : <PREFIXE>-<ANNÉE>-<CODE><SÉQ> ──────────

    def test_code_etablissement_dlp(self):
        """Code '05' → DLP-2026-0501 (code encodé en tête, séquence à 01)."""
        inst = Institution.objects.create(
            acronyme='XX', nom='X', est_principale=False, code_etablissement='05')
        fil = Filiere.objects.create(
            code='LPX', intitule_fr='X', type_diplome='LP',
            institution=inst, niveau_debut=1, niveau_fin=3)
        self.assertEqual(
            docsvc.generer_numero_diplome(inst, fil, '2025-2026'), 'DLP-2026-0501')

    def test_code_etablissement_dni_et_increment(self):
        """Code '02' + ING → DNI-2026-0201 ; un existant → +1 = 0202."""
        inst = Institution.objects.create(
            acronyme='YY', nom='Y', est_principale=False, code_etablissement='02')
        fil = Filiere.objects.create(
            code='INGY', intitule_fr='Y', type_diplome='ING',
            institution=inst, niveau_debut=1, niveau_fin=3)
        self.assertEqual(
            docsvc.generer_numero_diplome(inst, fil, '2025-2026'), 'DNI-2026-0201')
        RegistreDiplome.objects.create(
            institution=inst, etudiant=self.etu, filiere=fil,
            numero_diplome='DNI-2026-0201', mention='Bien',
            moyenne_generale=Decimal('14'), date_delivrance=date(2026, 6, 29),
            annee_universitaire='2025-2026')
        self.assertEqual(
            docsvc.generer_numero_diplome(inst, fil, '2025-2026'), 'DNI-2026-0202')


class AttributionDiplomeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(
            acronyme='ISS', nom='Inst', est_principale=True, diplome_sequence_debut=500)
        cls.year = Year.objects.create(annee='2025-2026')
        cls.dept = Departement.objects.create(nom='D', institution=cls.inst)

    def _pv(self, niveau_fin=3, niveau=3, decision='admis', filles=False):
        fil = Filiere.objects.create(
            code=f'F{niveau_fin}{niveau}{decision}{filles}', intitule_fr='F',
            type_diplome='LP', institution=self.inst,
            niveau_debut=1, niveau_fin=niveau_fin)
        if filles:
            Filiere.objects.create(
                code=f'FILLE{fil.code}', intitule_fr='fille', type_diplome='LP',
                institution=self.inst, niveau_debut=1, niveau_fin=3, filiere_parent=fil)
        etu = Etudiant.objects.create(
            matricule=f'M{fil.code}', nom='X', genre='F',
            departement=self.dept, filiere=fil)
        ia = InscriptionAdministrative.objects.create(
            etudiant=etu, annee_univ=self.year, filiere=fil, institution=self.inst,
            niveau=niveau, numero_inscription=f'I{fil.code}')
        pv = PVDeliberation.objects.create(
            filiere=fil, institution=self.inst, niveau=niveau,
            type_pv='annuel', annee_univ=self.year)
        LigneDeliberation.objects.create(
            pv=pv, inscription_admin=ia, decision=decision,
            decision_annuelle='passage_droit' if decision == 'admis' else 'redoublement',
            moyenne_annuelle=Decimal('14'), credits_annuels=60)
        return pv, etu

    @staticmethod
    def _mock_elig(credits=180, note='13.00', moy_cycle='14.50'):
        return (
            mock.patch.object(DeliberationAnnuelleService,
                              '_credits_capitalises_diplome', return_value=credits),
            mock.patch.object(DeliberationAnnuelleService,
                              '_moyenne_semestre_final', return_value=Decimal(note)),
            mock.patch.object(docsvc, '_moyenne_generale_cycle',
                              return_value=Decimal(moy_cycle)),
        )

    def test_attribution_cree_registre(self):
        pv, etu = self._pv()
        pa, pb, pc = self._mock_elig(moy_cycle='14.50')
        with pa, pb, pc:
            stats = docsvc.attribuer_diplomes_pv(pv)
        self.assertEqual(stats['crees'], 1)
        r = RegistreDiplome.objects.get(etudiant=etu)
        self.assertEqual(r.numero_diplome, 'DLP-ISS-2026-0500')
        self.assertEqual(r.moyenne_generale, Decimal('14.50'))
        self.assertEqual(r.mention, 'Bien')          # 14.50 → Bien (grille diplôme)
        self.assertEqual(r.annee_universitaire, '2025-2026')

    def test_idempotent(self):
        pv, etu = self._pv()
        pa, pb, pc = self._mock_elig()
        with pa, pb, pc:
            docsvc.attribuer_diplomes_pv(pv)
            stats2 = docsvc.attribuer_diplomes_pv(pv)
        self.assertEqual(stats2['crees'], 0)
        self.assertEqual(stats2['deja'], 1)
        self.assertEqual(RegistreDiplome.objects.filter(etudiant=etu).count(), 1)

    def test_non_eligible_pas_de_diplome(self):
        """< 180 crédits → non diplômé (défense en profondeur), aucun registre."""
        pv, etu = self._pv()
        pa, pb, pc = self._mock_elig(credits=176)
        with pa, pb, pc:
            stats = docsvc.attribuer_diplomes_pv(pv)
        self.assertEqual(stats['crees'], 0)
        self.assertEqual(stats['non_eligibles'], 1)
        self.assertFalse(RegistreDiplome.objects.filter(etudiant=etu).exists())

    def test_redoublement_ignore(self):
        pv, etu = self._pv(decision='ajourned')
        pa, pb, pc = self._mock_elig()
        with pa, pb, pc:
            stats = docsvc.attribuer_diplomes_pv(pv)
        self.assertEqual(stats['crees'], 0)
        self.assertEqual(stats['ignores'], 1)

    def test_pas_fin_de_cycle_l2(self):
        """PV L2 (niveau != niveau_fin) → aucune attribution."""
        pv, etu = self._pv(niveau=2)
        pa, pb, pc = self._mock_elig()
        with pa, pb, pc:
            stats = docsvc.attribuer_diplomes_pv(pv)
        self.assertEqual(stats['crees'], 0)
        self.assertFalse(RegistreDiplome.objects.filter(etudiant=etu).exists())

    def test_tronc_commun_exempte(self):
        """Filière AVEC filles (tronc commun) à son niveau_fin → pas d'attribution."""
        pv, etu = self._pv(niveau_fin=1, niveau=1, filles=True)
        pa, pb, pc = self._mock_elig()
        with pa, pb, pc:
            stats = docsvc.attribuer_diplomes_pv(pv)
        self.assertEqual(stats['crees'], 0)
