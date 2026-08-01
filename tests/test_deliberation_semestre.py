"""
Tests pour DeliberationSemestreService (Art. 15-17 Arrete 562).

Workflow couvert :
  peupler_lignes()       -> cree LigneDeliberation par etudiant inscrit
  calculer_decisions()   -> applique Art. 15 (admis/ajourne/rachat manuel)
  generer_obligations()  -> Art. 17 (rattrapage obligatoire/facultatif/aucun)

Bug regression couverts :
  - filtrage par self.pv.session (sinon credits/code_statut d'une session
    posterieure ecrasent ceux du PV normal).
"""
from decimal import Decimal
import pytest

from apps.evaluations.services.deliberation_semestre import DeliberationSemestreService
from tests.factories.deliberation import PVDeliberationSemestrielFactory
from tests.factories.evaluations import (
    SessionNormaleImpairsFactory, SessionRattrapageImpairsFactory,
    ResultatSemestreFactory,
)
from tests.factories.inscriptions import (
    InscriptionAdministrativeFactory, InscriptionPedagogiqueFactory,
)
from tests.factories.scolarite import FiliereFactory
from tests.factories.parametres import SemestreFactory, YearFactory


# ── Fixtures composites ────────────────────────────────────────────────────────
@pytest.fixture
def filiere(institution):
    return FiliereFactory(institution=institution)


@pytest.fixture
def annee(institution):
    return YearFactory(annee='2025-2026')


@pytest.fixture
def session_normale(institution, annee):
    return SessionNormaleImpairsFactory(institution=institution, annee_univ=annee)


@pytest.fixture
def pv_s1(institution, filiere, semestre_S1, session_normale):
    """PV semestriel S1 pour la filiere."""
    return PVDeliberationSemestrielFactory(
        institution=institution, filiere=filiere,
        session=session_normale, niveau=1, semestre_code='S1',
    )


def _create_etudiant_admis(filiere, annee, semestre, session, moyenne=Decimal('14'), credits=30):
    """Helper : etudiant inscrit + ResultatSemestre admis."""
    insc_admin = InscriptionAdministrativeFactory(
        filiere=filiere, annee_univ=annee, niveau=1, institution=filiere.institution,
    )
    insc_ped = InscriptionPedagogiqueFactory(
        inscription_admin=insc_admin, semestre=semestre,
    )
    ResultatSemestreFactory(
        inscription_ped=insc_ped, session=session,
        moyenne=moyenne, credits_valides=credits, est_admis=True,
    )
    return insc_ped


def _create_etudiant_ajourne(filiere, annee, semestre, session, moyenne=Decimal('7')):
    insc_admin = InscriptionAdministrativeFactory(
        filiere=filiere, annee_univ=annee, niveau=1, institution=filiere.institution,
    )
    insc_ped = InscriptionPedagogiqueFactory(
        inscription_admin=insc_admin, semestre=semestre,
    )
    ResultatSemestreFactory(
        inscription_ped=insc_ped, session=session,
        moyenne=moyenne, credits_valides=0, est_admis=False,
    )
    return insc_ped


# ── peupler_lignes ─────────────────────────────────────────────────────────────
class TestPeuplerLignes:

    def test_cree_une_ligne_par_etudiant(
        self, pv_s1, filiere, annee, semestre_S1, session_normale,
    ):
        _create_etudiant_admis(filiere, annee, semestre_S1, session_normale)
        _create_etudiant_admis(filiere, annee, semestre_S1, session_normale)
        _create_etudiant_ajourne(filiere, annee, semestre_S1, session_normale)

        service = DeliberationSemestreService(pv_s1)
        n = service.peupler_lignes()

        assert n == 3
        assert pv_s1.lignes.count() == 3

    def test_idempotent_appel_double(
        self, pv_s1, filiere, annee, semestre_S1, session_normale,
    ):
        _create_etudiant_admis(filiere, annee, semestre_S1, session_normale)

        service = DeliberationSemestreService(pv_s1)
        service.peupler_lignes()
        service.peupler_lignes()    # 2e appel ne duplique pas

        assert pv_s1.lignes.count() == 1

    def test_pv_non_semestriel_leve_valueerror(self, institution, filiere, annee, session_normale):
        """Service refuse PV de type 'annuel'."""
        from tests.factories.deliberation import PVDeliberationAnnuelFactory
        pv_annuel = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere, annee_univ=annee, niveau=1,
        )
        with pytest.raises(ValueError, match='semestriel'):
            DeliberationSemestreService(pv_annuel)

    def test_filtre_par_filiere_niveau_semestre(
        self, pv_s1, filiere, annee, semestre_S1, semestre_S2, session_normale,
    ):
        """Etudiant inscrit en S2 ne doit PAS apparaitre dans le PV S1."""
        _create_etudiant_admis(filiere, annee, semestre_S1, session_normale)
        _create_etudiant_admis(filiere, annee, semestre_S2, session_normale)  # autre semestre

        service = DeliberationSemestreService(pv_s1)
        n = service.peupler_lignes()

        assert n == 1   # seul le S1 est dans le PV S1


# ── Bug regression : filtre par session du PV ──────────────────────────────────
class TestPeuplerLignesFiltreSession:
    """Bug fixe : sans filtre session=self.pv.session, un PV de session normale
    pouvait recevoir credits/moyenne d'une session de rattrapage posterieure."""

    def test_pv_normale_ne_prend_pas_resultat_rattrapage(
        self, pv_s1, filiere, annee, semestre_S1, session_normale, institution,
    ):
        # Etudiant ajourne en SN (moy=7, 0 credit)
        insc_admin = InscriptionAdministrativeFactory(
            filiere=filiere, annee_univ=annee, niveau=1, institution=institution,
        )
        insc_ped = InscriptionPedagogiqueFactory(
            inscription_admin=insc_admin, semestre=semestre_S1,
        )
        ResultatSemestreFactory(
            inscription_ped=insc_ped, session=session_normale,
            moyenne=Decimal('7'), credits_valides=0, est_admis=False,
        )
        # Meme etudiant : ResultatSemestre cree pour la SR (admis 30 credits)
        session_sr = SessionRattrapageImpairsFactory(
            institution=institution, annee_univ=annee,
        )
        ResultatSemestreFactory(
            inscription_ped=insc_ped, session=session_sr,
            moyenne=Decimal('11'), credits_valides=30, est_admis=True,
        )

        # Le PV S1 cible la SN -> doit afficher 7 et 0 credit, pas 11 et 30
        service = DeliberationSemestreService(pv_s1)
        service.peupler_lignes()

        ligne = pv_s1.lignes.get(inscription_admin=insc_admin)
        assert ligne.moyenne_annuelle == Decimal('7')
        assert ligne.credits_annuels == 0


# ── calculer_decisions ─────────────────────────────────────────────────────────
class TestCalculerDecisions:

    def test_admis_si_resultat_semestre_admis(
        self, pv_s1, filiere, annee, semestre_S1, session_normale,
    ):
        _create_etudiant_admis(filiere, annee, semestre_S1, session_normale)
        service = DeliberationSemestreService(pv_s1)
        service.peupler_lignes()
        service.calculer_decisions()

        decisions = list(pv_s1.lignes.values_list('decision', flat=True))
        assert decisions == ['admis']

    def test_ajourne_si_pas_admis(
        self, pv_s1, filiere, annee, semestre_S1, session_normale,
    ):
        _create_etudiant_ajourne(filiere, annee, semestre_S1, session_normale)
        service = DeliberationSemestreService(pv_s1)
        service.peupler_lignes()
        service.calculer_decisions()

        decisions = list(pv_s1.lignes.values_list('decision', flat=True))
        assert decisions == ['ajourned']

    def test_rachat_manuel_pas_ecrase(
        self, pv_s1, filiere, annee, semestre_S1, session_normale,
    ):
        """Le jury a pose 'rachat' manuellement -> calculer_decisions ne le change pas."""
        _create_etudiant_ajourne(filiere, annee, semestre_S1, session_normale)
        service = DeliberationSemestreService(pv_s1)
        service.peupler_lignes()
        # Force rachat manuel
        ligne = pv_s1.lignes.first()
        ligne.decision = 'rachat'
        ligne.save()

        service.calculer_decisions()

        ligne.refresh_from_db()
        assert ligne.decision == 'rachat'

    def test_idempotent(self, pv_s1, filiere, annee, semestre_S1, session_normale):
        _create_etudiant_admis(filiere, annee, semestre_S1, session_normale)
        service = DeliberationSemestreService(pv_s1)
        service.peupler_lignes()
        service.calculer_decisions()
        service.calculer_decisions()    # 2e appel : meme resultat

        assert list(pv_s1.lignes.values_list('decision', flat=True)) == ['admis']

    def test_decision_change_si_resultat_change(
        self, pv_s1, filiere, annee, semestre_S1, session_normale,
    ):
        """Recalcul apres modification du ResultatSemestre."""
        from apps.evaluations.models import ResultatSemestre
        insc_ped = _create_etudiant_ajourne(filiere, annee, semestre_S1, session_normale)
        service = DeliberationSemestreService(pv_s1)
        service.peupler_lignes()
        service.calculer_decisions()
        assert pv_s1.lignes.first().decision == 'ajourned'

        # Recalcul note -> admis
        rs = ResultatSemestre.objects.get(inscription_ped=insc_ped, session=session_normale)
        rs.est_admis = True
        rs.save()
        service.calculer_decisions()

        assert pv_s1.lignes.first().decision == 'admis'


# ── generer_obligations (Art. 17) ──────────────────────────────────────────────
class TestGenerObligations:
    """Art. 17 : 3 cas
      - code='E'                -> obligatoire (eliminatoire)
      - code='NV' ET module < 8 -> obligatoire
      - code='NV' ET module ≥ 8 -> facultatif
      - code='V'/'VCI'/'VCS'    -> rien
    """

    def test_aucune_obligation_pour_etudiant_admis(
        self, pv_s1, filiere, annee, semestre_S1, session_normale,
    ):
        """Etudiant admis : pas d'obligation generee."""
        _create_etudiant_admis(filiere, annee, semestre_S1, session_normale)
        service = DeliberationSemestreService(pv_s1)
        service.peupler_lignes()
        service.calculer_decisions()
        n = service.generer_obligations()

        assert n == 0

    def test_idempotent_supprime_anciennes_obligations(
        self, pv_s1, filiere, annee, semestre_S1, session_normale,
    ):
        """generer_obligations() reecrit, ne duplique pas."""
        _create_etudiant_ajourne(filiere, annee, semestre_S1, session_normale)
        service = DeliberationSemestreService(pv_s1)
        service.peupler_lignes()
        service.calculer_decisions()

        n1 = service.generer_obligations()
        n2 = service.generer_obligations()    # 2e appel = meme resultat

        from apps.evaluations.models import ObligationRattrapage
        assert ObligationRattrapage.objects.count() == n2     # pas de duplication

        # n1 et n2 peuvent etre 0 si l'etudiant n'a pas de InscriptionElement+ResultatElement
        # (le test ne crée pas les elements/resultats individuels — voir test integration)
        assert n1 == n2


# ── Obligations : différenciation par régime (Arrêté 562 vs Décret 2018-070) ──

def _ajoute_element_nv_module_sous_8(insc_ped, session):
    """
    EM rattaché à un module LMD avec ResultatElement NV et module à 7/20
    (< 8 — non compensable). LP : obligatoire (Art. 17 al. 2) ;
    ING : facultatif (Art. 21 Décret).
    """
    from decimal import Decimal as D
    from apps.evaluations.models import ResultatModule
    from tests.factories.em import ModuleLMDFactory, EMLegacyFactory
    from tests.factories.inscriptions import InscriptionElementFactory
    from tests.factories.evaluations import ResultatElementFactory

    module = ModuleLMDFactory(
        filiere=insc_ped.inscription_admin.filiere,
        semestre=insc_ped.semestre,
    )
    em = EMLegacyFactory(module_lmd=module, semestre=insc_ped.semestre)
    ie = InscriptionElementFactory(inscription_ped=insc_ped, em=em, element=None)
    ResultatElementFactory(
        inscription_element=ie, session=session,
        note_finale=D('7.00'), est_valide=False,
        est_eliminatoire=False, code_statut='NV',
    )
    ResultatModule.objects.create(
        inscription_ped=insc_ped, module=module, session=session,
        moyenne=D('7.00'), credits_valides=0,
        est_valide=False, a_eliminatoire=False, code_statut='NV',
    )
    return ie


class TestObligationsParRegime:
    """
    LP (Arrêté 562 Art. 17 al. 2) : NV + module < 8 → OBLIGATOIRE.
    ING (Décret 2018-070 Art. 21) : seul l'éliminatoire est obligatoire ;
    NV → FACULTATIF quelle que soit la moyenne du module.
    """

    def test_lp_nv_module_sous_8_obligatoire(
        self, pv_s1, filiere, annee, semestre_S1, session_normale,
    ):
        insc_ped = _create_etudiant_ajourne(filiere, annee, semestre_S1, session_normale)
        _ajoute_element_nv_module_sous_8(insc_ped, session_normale)

        service = DeliberationSemestreService(pv_s1)
        service.peupler_lignes()
        service.calculer_decisions()
        n = service.generer_obligations()

        from apps.evaluations.models import ObligationRattrapage
        assert n == 1
        obl = ObligationRattrapage.objects.get()
        assert obl.type_obligation == 'obligatoire'
        assert 'Art. 17' in obl.motif

    def test_ing_nv_module_sous_8_facultatif(
        self, institution, annee, semestre_S1, session_normale,
    ):
        from tests.factories.scolarite import FiliereIngenieurFactory
        from tests.factories.deliberation import PVDeliberationSemestrielFactory

        filiere_ing = FiliereIngenieurFactory(institution=institution)
        pv_ing = PVDeliberationSemestrielFactory(
            institution=institution, filiere=filiere_ing,
            session=session_normale, niveau=1, semestre_code='S1',
        )
        insc_ped = _create_etudiant_ajourne(
            filiere_ing, annee, semestre_S1, session_normale,
        )
        _ajoute_element_nv_module_sous_8(insc_ped, session_normale)

        service = DeliberationSemestreService(pv_ing)
        service.peupler_lignes()
        service.calculer_decisions()
        n = service.generer_obligations()

        from apps.evaluations.models import ObligationRattrapage
        assert n == 1
        obl = ObligationRattrapage.objects.get()
        assert obl.type_obligation == 'facultatif'           # Art. 21 Décret
        assert 'Décret 2018-070' in obl.motif

    def test_ing_eliminatoire_reste_obligatoire(
        self, institution, annee, semestre_S1, session_normale,
    ):
        """ING : l'éliminatoire reste obligatoire (« doit obligatoirement »)."""
        from decimal import Decimal as D
        from tests.factories.scolarite import FiliereIngenieurFactory
        from tests.factories.deliberation import PVDeliberationSemestrielFactory
        from tests.factories.em import ModuleLMDFactory, EMLegacyFactory
        from tests.factories.inscriptions import InscriptionElementFactory
        from tests.factories.evaluations import ResultatElementFactory

        filiere_ing = FiliereIngenieurFactory(institution=institution)
        pv_ing = PVDeliberationSemestrielFactory(
            institution=institution, filiere=filiere_ing,
            session=session_normale, niveau=1, semestre_code='S1',
        )
        insc_ped = _create_etudiant_ajourne(
            filiere_ing, annee, semestre_S1, session_normale,
        )
        module = ModuleLMDFactory(filiere=filiere_ing, semestre=insc_ped.semestre)
        em = EMLegacyFactory(module_lmd=module, semestre=insc_ped.semestre)
        ie = InscriptionElementFactory(inscription_ped=insc_ped, em=em, element=None)
        ResultatElementFactory(
            inscription_element=ie, session=session_normale,
            note_finale=D('4.00'), est_valide=False,
            est_eliminatoire=True, code_statut='E',
        )

        service = DeliberationSemestreService(pv_ing)
        service.peupler_lignes()
        service.calculer_decisions()
        service.generer_obligations()

        from apps.evaluations.models import ObligationRattrapage
        obl = ObligationRattrapage.objects.get()
        assert obl.type_obligation == 'obligatoire'
        assert 'Décret 2018-070' in obl.motif
