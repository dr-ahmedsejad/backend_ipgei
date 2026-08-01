"""
Tests pour DeliberationAnnuelleService et ses sous-classes.

Couverture des regimes :
  - DeliberationAnnuelleLicence  -> Arrete 562 (LP) seuil 65 %
  - DeliberationAnnuelleIngenieur -> Decret 2018-070 (ING) seuil 75 %, verrou S5

Decisions annuelles testees :
  - passage_droit  : 60 credits
  - passage_cond   : taux >= seuil + pas de verrou
  - redoublement   : 1er ajournement
  - exclusion      : 2eme ajournement (deja_redoublant)
  - annee_blanche  : derogation Art. 23/29
"""
from decimal import Decimal
import pytest

from apps.evaluations.services.deliberation_annuelle import (
    DeliberationAnnuelleLicence, DeliberationAnnuelleIngenieur,
    get_deliberation_annuelle_service,
)
from tests.factories.deliberation import (
    PVDeliberationAnnuelFactory, PVDeliberationSemestrielFactory,
)
from tests.factories.scolarite import FiliereFactory, FiliereIngenieurFactory
from tests.factories.parametres import YearFactory
from tests.factories.inscriptions import (
    InscriptionAdministrativeFactory, InscriptionPedagogiqueFactory,
)
from tests.factories.evaluations import (
    SessionNormaleImpairsFactory, SessionRattrapageImpairsFactory,
    SessionNormalePairsFactory, ResultatSemestreFactory,
)


@pytest.fixture
def annee(institution):
    return YearFactory(annee='2025-2026')


@pytest.fixture
def filiere_lp(institution):
    return FiliereFactory(institution=institution, type_diplome='LP')


@pytest.fixture
def filiere_ing(institution):
    return FiliereIngenieurFactory(institution=institution)


# ── Factory dispatcher : type_diplome -> sous-classe ───────────────────────────
class TestServiceFactory:

    def test_filiere_lp_renvoie_classe_licence(self, institution, annee, filiere_lp):
        pv = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee, niveau=1,
        )
        service = get_deliberation_annuelle_service(pv)
        assert isinstance(service, DeliberationAnnuelleLicence)
        assert service.SEUIL_PROGRESSION == Decimal('65')

    def test_filiere_ing_renvoie_classe_ingenieur(self, institution, annee, filiere_ing):
        pv = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_ing, annee_univ=annee, niveau=1,
        )
        service = get_deliberation_annuelle_service(pv)
        assert isinstance(service, DeliberationAnnuelleIngenieur)
        assert service.SEUIL_PROGRESSION == Decimal('75')

    def test_pv_semestriel_leve_valueerror(self, institution, annee, filiere_lp, semestre_S1):
        session = SessionNormaleImpairsFactory(institution=institution, annee_univ=annee)
        pv = PVDeliberationSemestrielFactory(
            institution=institution, filiere=filiere_lp,
            session=session, niveau=1,
        )
        with pytest.raises(ValueError, match='annuel'):
            DeliberationAnnuelleLicence(pv)


# ── verifier_sessions_pretes ───────────────────────────────────────────────────
class TestVerifierSessionsPretes:

    def test_sans_aucune_session_2_warnings(self, institution, annee, filiere_lp):
        pv = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee, niveau=1,
        )
        service = DeliberationAnnuelleLicence(pv)
        result = service.verifier_sessions_pretes()

        assert result['pretes'] is False
        # 2 warnings : SN-Impairs absente + SN-Pairs absente
        assert len(result['warnings']) >= 2
        assert any('Impairs' in w for w in result['warnings'])
        assert any('Pairs' in w for w in result['warnings'])

    def test_4_sessions_closes_pretes(self, institution, annee, filiere_lp):
        SessionNormaleImpairsFactory(institution=institution, annee_univ=annee, est_close=True)
        SessionRattrapageImpairsFactory(institution=institution, annee_univ=annee, est_close=True)
        SessionNormalePairsFactory(institution=institution, annee_univ=annee, est_close=True)
        # 4eme session : SR Pairs
        SessionNormalePairsFactory(
            institution=institution, annee_univ=annee, est_close=True,
            type_session='rattrapage', code='SR-P-X',
        )

        pv = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee, niveau=1,
        )
        service = DeliberationAnnuelleLicence(pv)
        result = service.verifier_sessions_pretes()
        assert result['pretes'] is True
        assert result['warnings'] == []

    def test_session_normale_non_cloturee_warning(self, institution, annee, filiere_lp):
        SessionNormaleImpairsFactory(institution=institution, annee_univ=annee, est_close=False)
        pv = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee, niveau=1,
        )
        service = DeliberationAnnuelleLicence(pv)
        result = service.verifier_sessions_pretes()
        assert any('non cloturee' in w or 'provisoire' in w for w in result['warnings'])


# ── _est_deja_redoublant ───────────────────────────────────────────────────────
class TestEstDejaRedoublant:

    def test_aucune_progression_renvoie_false(self, institution, annee, filiere_lp):
        insc = InscriptionAdministrativeFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee, niveau=1,
        )
        assert DeliberationAnnuelleLicence._est_deja_redoublant(insc) is False

    def test_progression_anterieure_consomme_droit_renvoie_true(
        self, institution, filiere_lp,
    ):
        from apps.inscriptions.models import Progression

        annee_n1 = YearFactory(annee='2024-2025')
        annee_n  = YearFactory(annee='2025-2026')

        insc = InscriptionAdministrativeFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee_n, niveau=1,
        )

        Progression.objects.create(
            etudiant=insc.etudiant,
            matricule=insc.etudiant.matricule,
            annee_source=annee_n1,        # ANTERIEUR
            niveau_source=1,
            filiere_source=filiere_lp,
            annee_cible=annee_n,
            niveau_cible=1,
            filiere_cible=filiere_lp,
            decision='redoublement',
            consomme_droit_redoublement=True,
            institution=institution,
        )

        assert DeliberationAnnuelleLicence._est_deja_redoublant(insc) is True

    def test_progression_meme_annee_renvoie_false(
        self, institution, annee, filiere_lp,
    ):
        """Progression sur l'annee courante (ex: re-peuplement du PV en cours)
        ne doit PAS compter comme deja redoublant."""
        from apps.inscriptions.models import Progression

        insc = InscriptionAdministrativeFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee, niveau=1,
        )
        Progression.objects.create(
            etudiant=insc.etudiant,
            matricule=insc.etudiant.matricule,
            annee_source=annee,           # MEME annee que insc.annee_univ
            niveau_source=1,
            filiere_source=filiere_lp,
            annee_cible=annee,
            niveau_cible=1,
            filiere_cible=filiere_lp,
            decision='redoublement',
            consomme_droit_redoublement=True,
            institution=institution,
        )
        assert DeliberationAnnuelleLicence._est_deja_redoublant(insc) is False


# ── _calculer_verrou (Licence vs Ingenieur) ────────────────────────────────────
class TestCalculerVerrou:
    """Licence : verrou L3 si L1 (niveau 1) < 60 credits.
    Ingenieur : verrou S5 si S1+S2 < 60 credits."""

    def test_verrou_licence_niveau_1_jamais_actif(self, institution, annee, filiere_lp):
        pv = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee, niveau=1,
        )
        service = DeliberationAnnuelleLicence(pv)
        insc = InscriptionAdministrativeFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee, niveau=1,
        )
        # Pas en niveau 2 -> verrou inactif
        assert service._calculer_verrou(insc) is False

    def test_verrou_licence_niveau_2_actif_si_l1_incomplete(
        self, institution, filiere_lp,
    ):
        annee_n1 = YearFactory(annee='2024-2025')
        annee_n  = YearFactory(annee='2025-2026')

        pv = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee_n, niveau=2,
        )
        service = DeliberationAnnuelleLicence(pv)

        # Etudiant avec une L1 incomplete (40 credits seulement)
        insc_l2 = InscriptionAdministrativeFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee_n, niveau=2,
        )
        # Inscription L1 (annee precedente) avec 40 credits sur 60
        from tests.factories.parametres import SemestreFactory, NiveauFactory
        niv_l1 = NiveauFactory(niveau='L1')
        sem_s1_l1 = SemestreFactory(code_semestre='S1_L1', niveau_semestre=niv_l1, type_semestre='I')
        insc_l1 = InscriptionAdministrativeFactory(
            etudiant=insc_l2.etudiant, institution=institution, filiere=filiere_lp,
            annee_univ=annee_n1, niveau=1,
        )
        ip_l1 = InscriptionPedagogiqueFactory(inscription_admin=insc_l1, semestre=sem_s1_l1)
        sn_anc = SessionNormaleImpairsFactory(institution=institution, annee_univ=annee_n1)
        ResultatSemestreFactory(
            inscription_ped=ip_l1, session=sn_anc,
            credits_valides=40, est_admis=False,
        )

        assert service._calculer_verrou(insc_l2) is True   # 40 < 60

    def test_verrou_ingenieur_S5_appelle_credits_S1_S2(
        self, institution, annee, filiere_ing,
    ):
        """Verrou Ingenieur surcharge : utilise _credits_s1_s2 au lieu de niveau."""
        pv = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_ing, annee_univ=annee, niveau=2,
        )
        service = DeliberationAnnuelleIngenieur(pv)

        insc = InscriptionAdministrativeFactory(
            institution=institution, filiere=filiere_ing, annee_univ=annee, niveau=2,
        )
        # Pas d'historique -> 0 credits S1+S2 -> verrou actif
        assert service._calculer_verrou(insc) is True


# ── calculer_decisions Licence (Art. 20-22) ────────────────────────────────────
class TestCalculerDecisionsLicence:
    """Seuil 65 % pour LP. 4 cas : passage_droit / passage_cond / redoublement / exclusion."""

    def _setup_etudiant(
        self, institution, annee, filiere, pv,
        credits_annuels, taux=None, verrou=False,
    ):
        """Cree une LigneDeliberation avec valeurs pretes, pas de Progression."""
        from apps.evaluations.models import LigneDeliberation
        insc = InscriptionAdministrativeFactory(
            institution=institution, filiere=filiere, annee_univ=annee, niveau=pv.niveau,
        )
        if taux is None:
            taux = Decimal(str(credits_annuels)) / Decimal('60') * 100
        return LigneDeliberation.objects.create(
            pv=pv, inscription_admin=insc,
            decision='ajourned',
            credits_annuels=credits_annuels,
            taux_capitalisation=taux,
            verrou_passage=verrou,
        )

    def test_60_credits_donne_passage_droit(self, institution, annee, filiere_lp):
        pv = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee, niveau=1,
        )
        ligne = self._setup_etudiant(institution, annee, filiere_lp, pv,
                                      credits_annuels=60)
        DeliberationAnnuelleLicence(pv).calculer_decisions()
        ligne.refresh_from_db()
        assert ligne.decision_annuelle == 'passage_droit'
        assert ligne.decision == 'admis'

    def test_60_credits_avec_verrou_donne_redoublement(
        self, institution, annee, filiere_lp,
    ):
        """Verrou actif -> meme avec 60 credits (passage de droit) -> redoublement.
        Art. 20 al. 2 : niveau inferieur non valide -> acces au niveau superieur
        interdit, le verrou PRIME sur le passage de droit."""
        pv = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee, niveau=2,
        )
        ligne = self._setup_etudiant(institution, annee, filiere_lp, pv,
                                      credits_annuels=60, verrou=True)
        DeliberationAnnuelleLicence(pv).calculer_decisions()
        ligne.refresh_from_db()
        assert ligne.decision_annuelle == 'redoublement'
        assert ligne.decision == 'ajourned'

    def test_taux_65_donne_passage_cond_si_pas_verrou(
        self, institution, annee, filiere_lp,
    ):
        pv = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee, niveau=1,
        )
        # 39/60 = 65% pile -> passage_cond
        ligne = self._setup_etudiant(institution, annee, filiere_lp, pv,
                                      credits_annuels=39, verrou=False)
        DeliberationAnnuelleLicence(pv).calculer_decisions()
        ligne.refresh_from_db()
        assert ligne.decision_annuelle == 'passage_cond'
        assert ligne.decision == 'admis'

    def test_taux_65_avec_verrou_donne_redoublement(
        self, institution, annee, filiere_lp,
    ):
        """Verrou actif -> meme avec 65% -> redoublement."""
        pv = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee, niveau=2,
        )
        ligne = self._setup_etudiant(institution, annee, filiere_lp, pv,
                                      credits_annuels=39, verrou=True)
        DeliberationAnnuelleLicence(pv).calculer_decisions()
        ligne.refresh_from_db()
        assert ligne.decision_annuelle == 'redoublement'
        assert ligne.decision == 'ajourned'

    def test_taux_inferieur_65_premier_ajournement_redoublement(
        self, institution, annee, filiere_lp,
    ):
        pv = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee, niveau=1,
        )
        ligne = self._setup_etudiant(institution, annee, filiere_lp, pv,
                                      credits_annuels=20)
        DeliberationAnnuelleLicence(pv).calculer_decisions()
        ligne.refresh_from_db()
        # Premier ajournement -> redoublement (Art. 21)
        assert ligne.decision_annuelle == 'redoublement'

    def test_annee_blanche_manuelle_pas_ecrasee(
        self, institution, annee, filiere_lp,
    ):
        pv = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee, niveau=1,
        )
        ligne = self._setup_etudiant(institution, annee, filiere_lp, pv,
                                      credits_annuels=60)
        ligne.decision_annuelle = 'annee_blanche'
        ligne.save()

        DeliberationAnnuelleLicence(pv).calculer_decisions()
        ligne.refresh_from_db()
        assert ligne.decision_annuelle == 'annee_blanche'   # preserve


# ── calculer_decisions Ingenieur (Decret 2018-070) ─────────────────────────────
class TestCalculerDecisionsIngenieur:
    """Seuil 75% pour ING. PFE >= 12 niveau 3."""

    def test_seuil_75_passage_cond_a_45_credits(self, institution, annee, filiere_ing):
        from apps.evaluations.models import LigneDeliberation
        pv = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_ing, annee_univ=annee, niveau=1,
        )
        insc = InscriptionAdministrativeFactory(
            institution=institution, filiere=filiere_ing, annee_univ=annee, niveau=1,
        )
        # 45/60 = 75 % pile -> passage_cond pour ING
        ligne = LigneDeliberation.objects.create(
            pv=pv, inscription_admin=insc,
            decision='ajourned',
            credits_annuels=45,
            taux_capitalisation=Decimal('75'),
            verrou_passage=False,
        )

        DeliberationAnnuelleIngenieur(pv).calculer_decisions()
        ligne.refresh_from_db()
        assert ligne.decision_annuelle == 'passage_cond'
        assert ligne.decision == 'admis'

    def test_seuil_75_blocage_a_44_credits(self, institution, annee, filiere_ing):
        """44/60 = 73.3 % < 75 % -> redoublement pour ING."""
        from apps.evaluations.models import LigneDeliberation
        pv = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_ing, annee_univ=annee, niveau=1,
        )
        insc = InscriptionAdministrativeFactory(
            institution=institution, filiere=filiere_ing, annee_univ=annee, niveau=1,
        )
        ligne = LigneDeliberation.objects.create(
            pv=pv, inscription_admin=insc,
            decision='ajourned',
            credits_annuels=44,
            taux_capitalisation=Decimal('73.33'),
            verrou_passage=False,
        )

        DeliberationAnnuelleIngenieur(pv).calculer_decisions()
        ligne.refresh_from_db()
        assert ligne.decision_annuelle == 'redoublement'   # < 75 %

    def test_meme_44_credits_passe_pour_licence_mais_pas_ingenieur(
        self, institution, annee, filiere_lp, filiere_ing,
    ):
        """Test cle : 44 credits = passage LP mais redoublement ING (preuve de la separation)."""
        from apps.evaluations.models import LigneDeliberation

        # Cas LP (seuil 65 %)
        pv_lp = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee, niveau=1,
        )
        insc_lp = InscriptionAdministrativeFactory(
            institution=institution, filiere=filiere_lp, annee_univ=annee, niveau=1,
        )
        l_lp = LigneDeliberation.objects.create(
            pv=pv_lp, inscription_admin=insc_lp, decision='ajourned',
            credits_annuels=44, taux_capitalisation=Decimal('73.33'),
        )
        DeliberationAnnuelleLicence(pv_lp).calculer_decisions()
        l_lp.refresh_from_db()

        # Cas ING (seuil 75 %)
        pv_ing = PVDeliberationAnnuelFactory(
            institution=institution, filiere=filiere_ing, annee_univ=annee, niveau=1,
        )
        insc_ing = InscriptionAdministrativeFactory(
            institution=institution, filiere=filiere_ing, annee_univ=annee, niveau=1,
        )
        l_ing = LigneDeliberation.objects.create(
            pv=pv_ing, inscription_admin=insc_ing, decision='ajourned',
            credits_annuels=44, taux_capitalisation=Decimal('73.33'),
        )
        DeliberationAnnuelleIngenieur(pv_ing).calculer_decisions()
        l_ing.refresh_from_db()

        # PROUVE QUE LE CODE DIFFERENCIE DLP/DNI :
        assert l_lp.decision_annuelle == 'passage_cond'   # 73.3 > 65
        assert l_ing.decision_annuelle == 'redoublement'  # 73.3 < 75
