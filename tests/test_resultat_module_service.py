"""
Tests d'integration pour ResultatModuleService (calcul_module.py).

Source legale :
- Arrete 562 Art. 13 : moyenne module = Σ(note_element × coeff) / Σ(coeff),
  module valide si moyenne >= 10 ET pas d'eliminatoire.
  Compensation intra-module : element < 10 dans module valide -> VCI.
- Arrete 562 Art. 14 : compensation semestrielle — module NV avec moyenne >= 8
  devient V si le semestre est admis (elements NV -> VCS).
- Arrete 562 Art. 15 : la validation (directe ou par compensation) emporte
  la capitalisation des credits du module.

Ces tests sont l'ORACLE de non-regression pour la migration MySQL -> PostgreSQL :
toutes les valeurs attendues sont derivees A LA MAIN de la formule et figent
l'arrondi quantize(Decimal('0.01'), ROUND_HALF_UP) fait cote Python.

NB : le flag est_eliminatoire est pose en amont par NoteCalculService.calculer_element
(seuil < 6, deja teste dans test_calculer_element.py). calcul_module.py ne fait que
LIRE ce flag — on le fixe donc directement sur les ResultatElement de test.

Couvre :
- moyenne ponderee + arrondi HALF_UP au centieme (cas x.xx5 ou HALF_UP != HALF_EVEN)
- precedence des coefficients : element.coefficient > em.coefficient > Decimal('1')
  (coefficient 0 ou None = falsy -> fallback)
- total_coeff == 0 : retour None + suppression du ResultatModule existant
- selection des elements : em__module_lmd d'abord, fallback element__module
  UNIQUEMENT si la premiere requete est vide
- matrice des codes E/V/VCI/NV sur ResultatElement, V/NV sur ResultatModule
- rafraichir_codes_apres_semestre : compensation Art. 14 (bornes 8.00 / 7.99),
  VCS vs VCI, immuabilite de E et V, idempotence
- credits_valides selon le statut du module
"""
from decimal import Decimal

import pytest

from apps.evaluations.models import ResultatElement, ResultatModule
from apps.evaluations.services.calcul_module import ResultatModuleService
from tests.factories.evaluations import (
    SessionNormaleImpairsFactory, ResultatElementFactory,
)
from tests.factories.inscriptions import (
    InscriptionAdministrativeFactory,
    InscriptionPedagogiqueFactory,
    InscriptionElementFactory,
)
from tests.factories.em import ModuleLMDFactory, ElementModuleFactory, EMLegacyFactory


# ── Fixtures locales ────────────────────────────────────────────────────────────

@pytest.fixture
def session(institution):
    return SessionNormaleImpairsFactory(institution=institution)


@pytest.fixture
def insc_ped(institution, semestre_S1, filiere_dlp):
    """InscriptionPedagogique coherente (meme semestre + filiere que le module)."""
    insc_admin = InscriptionAdministrativeFactory(
        filiere=filiere_dlp, institution=institution,
    )
    return InscriptionPedagogiqueFactory(
        inscription_admin=insc_admin, semestre=semestre_S1,
    )


@pytest.fixture
def module(institution, semestre_S1, filiere_dlp):
    """Module LMD 6 credits, rattache au meme semestre/filiere que insc_ped."""
    return ModuleLMDFactory(
        semestre=semestre_S1, filiere=filiere_dlp,
        institution=institution, credits=6,
    )


# ── Helper local : IE + ResultatElement rattaches a un module ───────────────────

def _ajoute_element(insc_ped, module, session, note, *,
                    element_coeff=None, em_coeff=None,
                    avec_element=False, avec_em=True,
                    est_eliminatoire=False, code_statut=''):
    """
    Cree une InscriptionElement + son ResultatElement pour cette session.

    - avec_em=True      : lien planification em.module_lmd -> module
                          (chemin PRINCIPAL de calculer())
    - avec_element=True : lien academique element.module -> module
                          (chemin FALLBACK de calculer())
    - element_coeff / em_coeff : None = defaut factory (1.00) cote element,
                          NULL en base cote em (IntegerField nullable).
    """
    element = None
    if avec_element:
        kwargs = {'module': module}
        if element_coeff is not None:
            kwargs['coefficient'] = element_coeff
        element = ElementModuleFactory(**kwargs)

    em = None
    if avec_em:
        kwargs = {'module_lmd': module, 'semestre': insc_ped.semestre}
        if em_coeff is not None:
            kwargs['coefficient'] = em_coeff
        em = EMLegacyFactory(**kwargs)

    ie = InscriptionElementFactory(inscription_ped=insc_ped, element=element, em=em)
    note = Decimal(note)
    res = ResultatElementFactory(
        inscription_element=ie, session=session,
        note_finale=note,
        est_valide=(note >= Decimal('10')) and not est_eliminatoire,
        est_eliminatoire=est_eliminatoire,
        code_statut=code_statut,
    )
    return ie, res


# ── Moyenne ponderee + arrondi HALF_UP ──────────────────────────────────────────
class TestMoyennePonderee:
    """moyenne = Σ(note × coeff) / Σ(coeff), quantize(0.01, ROUND_HALF_UP)."""

    def test_moyenne_ponderee_nominale(self, session, insc_ped, module):
        # Coefficients element : 2 et 3 -> (12*2 + 14*3) / 5 = 66/5 = 13.20
        _ajoute_element(insc_ped, module, session, '12.00',
                        avec_element=True, element_coeff=Decimal('2.00'))
        _ajoute_element(insc_ped, module, session, '14.00',
                        avec_element=True, element_coeff=Decimal('3.00'))

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm.moyenne == Decimal('13.20')
        assert rm.est_valide is True
        assert rm.code_statut == 'V'
        assert rm.credits_valides == 6

    def test_arrondi_half_up_differe_de_half_even(self, session, insc_ped, module):
        # (10.00 + 10.09) / 2 = 20.09/2 = 10.045
        # ROUND_HALF_UP  -> 10.05 (ROUND_HALF_EVEN donnerait 10.04 : chiffre pair)
        _ajoute_element(insc_ped, module, session, '10.00')
        _ajoute_element(insc_ped, module, session, '10.09')

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm.moyenne == Decimal('10.05')
        assert rm.est_valide is True

    def test_arrondi_x_xx5_fait_basculer_la_validation(self, session, insc_ped, module):
        # (9.99 + 10.00) / 2 = 19.99/2 = 9.995 -> HALF_UP -> 10.00
        # L'arrondi fait passer le module de NV a V : oracle critique de migration.
        _ajoute_element(insc_ped, module, session, '9.99')
        _ajoute_element(insc_ped, module, session, '10.00')

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm.moyenne == Decimal('10.00')
        assert rm.est_valide is True
        assert rm.code_statut == 'V'
        assert rm.credits_valides == 6

    def test_moyenne_tiers_periodique_arrondi_bas(self, session, insc_ped, module):
        # (10 + 10 + 11) / 3 = 31/3 = 10.3333... -> 10.33
        _ajoute_element(insc_ped, module, session, '10.00')
        _ajoute_element(insc_ped, module, session, '10.00')
        _ajoute_element(insc_ped, module, session, '11.00')

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm.moyenne == Decimal('10.33')

    def test_moyenne_tiers_periodique_arrondi_haut(self, session, insc_ped, module):
        # coeffs 1 et 2 -> (10*1 + 11*2) / 3 = 32/3 = 10.6666... -> 10.67
        _ajoute_element(insc_ped, module, session, '10.00',
                        avec_element=True, element_coeff=Decimal('1.00'))
        _ajoute_element(insc_ped, module, session, '11.00',
                        avec_element=True, element_coeff=Decimal('2.00'))

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm.moyenne == Decimal('10.67')

    def test_moyenne_septiemes_periodique(self, session, insc_ped, module):
        # coeffs 3, 2, 2 -> (12*3 + 13*2 + 9*2) / 7 = (36+26+18)/7 = 80/7
        # = 11.428571... -> 11.43
        _ajoute_element(insc_ped, module, session, '12.00',
                        avec_element=True, element_coeff=Decimal('3.00'))
        _ajoute_element(insc_ped, module, session, '13.00',
                        avec_element=True, element_coeff=Decimal('2.00'))
        _ajoute_element(insc_ped, module, session, '9.00',
                        avec_element=True, element_coeff=Decimal('2.00'))

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm.moyenne == Decimal('11.43')


# ── Precedence des coefficients ─────────────────────────────────────────────────
class TestPrecedenceCoefficients:
    """coeff = element.coefficient (si truthy) > em.coefficient (si truthy) > Decimal('1').
    Un coefficient 0 ou None est falsy -> fallback au niveau suivant."""

    def test_element_coefficient_prioritaire_sur_em(self, session, insc_ped, module):
        # A : element.coeff=3, em.coeff=5, note 8 | B : element.coeff=1, em.coeff=1, note 16
        # Avec coeffs ELEMENT : (8*3 + 16*1)/4 = 40/4 = 10.00 -> V
        # (avec coeffs EM ce serait (8*5 + 16*1)/6 = 56/6 = 9.33 -> NV)
        _ajoute_element(insc_ped, module, session, '8.00',
                        avec_element=True, element_coeff=Decimal('3.00'), em_coeff=5)
        _ajoute_element(insc_ped, module, session, '16.00',
                        avec_element=True, element_coeff=Decimal('1.00'), em_coeff=1)

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm.moyenne == Decimal('10.00')
        assert rm.est_valide is True

    def test_em_coefficient_utilise_si_element_absent(self, session, insc_ped, module):
        # element=None -> em.coefficient (IntegerField, melange Decimal × int)
        # (8*4 + 18*1)/5 = 50/5 = 10.00
        # (avec le defaut 1 partout ce serait (8+18)/2 = 13.00)
        _ajoute_element(insc_ped, module, session, '8.00', em_coeff=4)
        _ajoute_element(insc_ped, module, session, '18.00', em_coeff=1)

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm.moyenne == Decimal('10.00')

    def test_element_coefficient_zero_fallback_sur_em(self, session, insc_ped, module):
        # element.coefficient=0.00 est falsy -> on retombe sur em.coefficient=3
        # A : coeff retenu 3, note 8 | B : element.coeff=1 (truthy, prime sur em=7), note 16
        # (8*3 + 16*1)/4 = 40/4 = 10.00
        # (si 0 etait utilise tel quel : 16/1 = 16.00 ; si 0 -> defaut 1 : 24/2 = 12.00)
        _ajoute_element(insc_ped, module, session, '8.00',
                        avec_element=True, element_coeff=Decimal('0.00'), em_coeff=3)
        _ajoute_element(insc_ped, module, session, '16.00',
                        avec_element=True, element_coeff=Decimal('1.00'), em_coeff=7)

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm.moyenne == Decimal('10.00')

    def test_em_coefficient_none_donne_defaut_1(self, session, insc_ped, module):
        # A : element=None, em.coefficient=NULL -> defaut Decimal('1'), note 9
        # B : element=None, em.coefficient=3, note 13
        # (9*1 + 13*3)/4 = 48/4 = 12.00
        _ajoute_element(insc_ped, module, session, '9.00')          # em_coeff None -> 1
        _ajoute_element(insc_ped, module, session, '13.00', em_coeff=3)

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm.moyenne == Decimal('12.00')


# ── total_coeff == 0 : aucun element evalue ─────────────────────────────────────
class TestTotalCoeffZero:
    """Aucun ResultatElement pour la session -> pas de moyenne calculable :
    calculer() retourne None et SUPPRIME le ResultatModule existant."""

    def test_aucun_resultat_element_retourne_none(self, session, insc_ped, module):
        # IE inscrite mais jamais evaluee (pas de ResultatElement pour la session)
        em = EMLegacyFactory(module_lmd=module, semestre=insc_ped.semestre)
        InscriptionElementFactory(inscription_ped=insc_ped, em=em, element=None)

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm is None
        assert ResultatModule.objects.filter(
            inscription_ped=insc_ped, module=module, session=session,
        ).count() == 0

    def test_supprime_resultat_module_existant(self, session, insc_ped, module):
        """Cas 'notes effacees apres coup' : le RM orphelin doit disparaitre."""
        em = EMLegacyFactory(module_lmd=module, semestre=insc_ped.semestre)
        InscriptionElementFactory(inscription_ped=insc_ped, em=em, element=None)
        ResultatModule.objects.create(
            inscription_ped=insc_ped, module=module, session=session,
            moyenne=Decimal('12.00'), credits_valides=6,
            est_valide=True, a_eliminatoire=False, code_statut='V',
        )

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm is None
        assert ResultatModule.objects.filter(
            inscription_ped=insc_ped, module=module, session=session,
        ).count() == 0


# ── Selection des elements : em__module_lmd puis fallback element__module ───────
class TestSelectionElements:

    def test_fallback_element_module_si_aucun_lien_em(self, session, insc_ped, module):
        # IE reliee au module UNIQUEMENT via element.module (em=None)
        # -> la 1re requete (em__module_lmd) est vide, le fallback prend le relais
        _ajoute_element(insc_ped, module, session, '13.00',
                        avec_element=True, avec_em=False)

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm is not None
        assert rm.moyenne == Decimal('13.00')
        assert rm.est_valide is True

    def test_pas_de_fallback_si_lien_em_present(self, session, insc_ped, module):
        # IE1 via em.module_lmd (note 12) ; IE2 via element.module seul (note 6, eliminatoire)
        # La 1re requete est NON vide -> IE2 est IGNOREE (pas de fusion des deux chemins) :
        # moyenne = 12.00 et a_eliminatoire=False (et non (12+6)/2 = 9.00 eliminatoire)
        _ajoute_element(insc_ped, module, session, '12.00')      # via em
        _, res_orphelin = _ajoute_element(
            insc_ped, module, session, '6.00',
            avec_element=True, avec_em=False, est_eliminatoire=True,
        )

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm.moyenne == Decimal('12.00')
        assert rm.a_eliminatoire is False
        assert rm.est_valide is True
        # L'element hors perimetre n'a pas recu de code
        res_orphelin.refresh_from_db()
        assert res_orphelin.code_statut == ''


# ── Matrice des codes E/V/VCI/NV (calculer + _assigner_codes_elements) ──────────
class TestCodesElements:
    """Ordre strict : est_eliminatoire -> E ; note >= 10 -> V ;
    module valide -> VCI ; sinon NV."""

    def test_eliminatoire_donne_E_et_bloque_le_module(self, session, insc_ped, module):
        # (4 + 16)/2 = 10.00 >= 10 MAIS a_eliminatoire -> module NV, credits 0
        _, res_e = _ajoute_element(insc_ped, module, session, '4.00',
                                   est_eliminatoire=True)
        _, res_v = _ajoute_element(insc_ped, module, session, '16.00')

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm.moyenne == Decimal('10.00')
        assert rm.a_eliminatoire is True
        assert rm.est_valide is False
        assert rm.code_statut == 'NV'
        assert rm.credits_valides == 0

        res_e.refresh_from_db()
        res_v.refresh_from_db()
        assert res_e.code_statut == 'E'
        assert res_e.est_valide is False
        assert res_v.code_statut == 'V'
        assert res_v.est_valide is True

    def test_eliminatoire_prioritaire_meme_si_note_haute(self, session, insc_ped, module):
        # Le flag est_eliminatoire est la SEULE source : il prime sur note >= 10
        _, res = _ajoute_element(insc_ped, module, session, '12.00',
                                 est_eliminatoire=True)

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        res.refresh_from_db()
        assert res.code_statut == 'E'
        assert res.est_valide is False
        assert rm.code_statut == 'NV'
        assert rm.est_valide is False

    def test_note_10_pile_donne_V(self, session, insc_ped, module):
        # Borne incluse : note_finale >= 10 -> V ; moyenne 10.00 pile -> module V
        _, res_a = _ajoute_element(insc_ped, module, session, '10.00')
        _, res_b = _ajoute_element(insc_ped, module, session, '10.00')

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm.moyenne == Decimal('10.00')
        assert rm.est_valide is True
        assert rm.code_statut == 'V'
        assert rm.credits_valides == 6
        for res in (res_a, res_b):
            res.refresh_from_db()
            assert res.code_statut == 'V'
            assert res.est_valide is True

    def test_note_9_99_dans_module_valide_donne_VCI(self, session, insc_ped, module):
        # (9.99 + 14.01)/2 = 24.00/2 = 12.00 -> module V
        # Element 9.99 (< 10 d'un centieme) -> VCI, est_valide True (Art. 13)
        _, res_vci = _ajoute_element(insc_ped, module, session, '9.99')
        _, res_v = _ajoute_element(insc_ped, module, session, '14.01')

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm.moyenne == Decimal('12.00')
        assert rm.est_valide is True

        res_vci.refresh_from_db()
        res_v.refresh_from_db()
        assert res_vci.code_statut == 'VCI'
        assert res_vci.est_valide is True
        assert res_v.code_statut == 'V'

    def test_module_non_valide_elements_NV(self, session, insc_ped, module):
        # (8 + 9)/2 = 8.50 < 10 -> module NV, elements NV (provisoire -> VCS possible)
        _, res_a = _ajoute_element(insc_ped, module, session, '8.00')
        _, res_b = _ajoute_element(insc_ped, module, session, '9.00')

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm.moyenne == Decimal('8.50')
        assert rm.est_valide is False
        assert rm.code_statut == 'NV'
        assert rm.credits_valides == 0
        for res in (res_a, res_b):
            res.refresh_from_db()
            assert res.code_statut == 'NV'
            assert res.est_valide is False

    def test_module_9_99_non_valide(self, session, insc_ped, module):
        # Borne module juste sous le seuil : moyenne 9.99 -> NV, credits 0
        _, res = _ajoute_element(insc_ped, module, session, '9.99')

        rm = ResultatModuleService(session).calculer(insc_ped, module)

        assert rm.moyenne == Decimal('9.99')
        assert rm.est_valide is False
        assert rm.code_statut == 'NV'
        assert rm.credits_valides == 0
        res.refresh_from_db()
        assert res.code_statut == 'NV'

    def test_calculer_idempotent(self, session, insc_ped, module):
        """update_or_create : 2 appels = 1 seul ResultatModule, meme ligne."""
        _ajoute_element(insc_ped, module, session, '12.00')
        _ajoute_element(insc_ped, module, session, '8.00')

        service = ResultatModuleService(session)
        rm1 = service.calculer(insc_ped, module)
        rm2 = service.calculer(insc_ped, module)

        assert rm1.id == rm2.id
        assert rm2.moyenne == Decimal('10.00')   # (12+8)/2
        assert ResultatModule.objects.filter(
            inscription_ped=insc_ped, module=module, session=session,
        ).count() == 1


# ── rafraichir_codes_apres_semestre : compensation Art. 14 + VCS/VCI ────────────
class TestRafraichirCodesApresSemestre:

    def test_compensation_moyenne_8_pile_semestre_admis(self, session, insc_ped, module):
        # (7 + 9)/2 = 8.00 pile -> compensable (borne >= 8 INCLUSE)
        # Semestre admis -> module NV devient V, credits captialises (Art. 15),
        # elements NV -> VCS (module_directement_valide = 8.00 >= 10 est False)
        _, res_a = _ajoute_element(insc_ped, module, session, '7.00')
        _, res_b = _ajoute_element(insc_ped, module, session, '9.00')

        service = ResultatModuleService(session)
        rm = service.calculer(insc_ped, module)
        assert rm.code_statut == 'NV'
        assert rm.credits_valides == 0

        service.rafraichir_codes_apres_semestre(insc_ped, est_semestre_admis=True)

        rm.refresh_from_db()
        assert rm.code_statut == 'V'
        assert rm.est_valide is True
        assert rm.credits_valides == 6
        for res in (res_a, res_b):
            res.refresh_from_db()
            assert res.code_statut == 'VCS'
            assert res.est_valide is True

    def test_pas_de_compensation_moyenne_7_99(self, session, insc_ped, module):
        # (7.99 + 7.99)/2 = 7.99 < 8 -> module NON compensable meme semestre admis
        _, res_a = _ajoute_element(insc_ped, module, session, '7.99')
        _, res_b = _ajoute_element(insc_ped, module, session, '7.99')

        service = ResultatModuleService(session)
        rm = service.calculer(insc_ped, module)
        service.rafraichir_codes_apres_semestre(insc_ped, est_semestre_admis=True)

        rm.refresh_from_db()
        assert rm.moyenne == Decimal('7.99')
        assert rm.code_statut == 'NV'
        assert rm.est_valide is False
        assert rm.credits_valides == 0
        for res in (res_a, res_b):
            res.refresh_from_db()
            assert res.code_statut == 'NV'
            assert res.est_valide is False

    def test_pas_de_compensation_semestre_non_admis(self, session, insc_ped, module):
        # Module compensable (9.00 >= 8) mais semestre NON admis -> reste NV
        _, res_a = _ajoute_element(insc_ped, module, session, '9.00')
        _, res_b = _ajoute_element(insc_ped, module, session, '9.00')

        service = ResultatModuleService(session)
        rm = service.calculer(insc_ped, module)
        service.rafraichir_codes_apres_semestre(insc_ped, est_semestre_admis=False)

        rm.refresh_from_db()
        assert rm.code_statut == 'NV'
        assert rm.credits_valides == 0
        for res in (res_a, res_b):
            res.refresh_from_db()
            assert res.code_statut == 'NV'
            assert res.est_valide is False

    def test_module_directement_valide_pose_VCI_pas_VCS(self, session, insc_ped, module):
        # Module V direct (moyenne (9+15)/2 = 12.00 >= 10) : l'element < 10 recoit
        # VCI (deja pose par calculer) et rafraichir le laisse VCI — jamais VCS.
        _, res_vci = _ajoute_element(insc_ped, module, session, '9.00')
        _, res_v = _ajoute_element(insc_ped, module, session, '15.00')

        service = ResultatModuleService(session)
        service.calculer(insc_ped, module)
        service.rafraichir_codes_apres_semestre(insc_ped, est_semestre_admis=True)

        res_vci.refresh_from_db()
        res_v.refresh_from_db()
        assert res_vci.code_statut == 'VCI'
        assert res_vci.est_valide is True
        assert res_v.code_statut == 'V'

    def test_element_code_vide_recoit_VCI_meme_semestre_non_admis(
        self, session, insc_ped, module,
    ):
        # Element jamais passe par _assigner_codes_elements (code_statut='')
        # dans un module deja V (moyenne >= 10) : rafraichir pose VCI.
        # La propagation VCI (etape 2) ne depend PAS de est_semestre_admis.
        _, res = _ajoute_element(insc_ped, module, session, '9.00', code_statut='')
        ResultatModule.objects.create(
            inscription_ped=insc_ped, module=module, session=session,
            moyenne=Decimal('12.00'), credits_valides=6,
            est_valide=True, a_eliminatoire=False, code_statut='V',
        )

        service = ResultatModuleService(session)
        service.rafraichir_codes_apres_semestre(insc_ped, est_semestre_admis=False)

        res.refresh_from_db()
        assert res.code_statut == 'VCI'
        assert res.est_valide is True

    def test_codes_E_et_V_immuables_dans_module_compense(self, session, insc_ped, module):
        # (15 + 4 + 9.50)/3 = 28.50/3 = 9.50 -> module NV (et a_eliminatoire=True).
        # rafraichir(est_semestre_admis=True) compense (9.50 >= 8) SANS re-tester
        # a_eliminatoire : c'est l'appelant (calculer_semestre) qui passe
        # est_semestre_admis=False en presence d'eliminatoire. Comportement fige ici.
        _, res_v = _ajoute_element(insc_ped, module, session, '15.00')
        _, res_e = _ajoute_element(insc_ped, module, session, '4.00',
                                   est_eliminatoire=True)
        _, res_nv = _ajoute_element(insc_ped, module, session, '9.50')

        service = ResultatModuleService(session)
        rm = service.calculer(insc_ped, module)
        assert rm.moyenne == Decimal('9.50')
        assert rm.code_statut == 'NV'

        service.rafraichir_codes_apres_semestre(insc_ped, est_semestre_admis=True)

        rm.refresh_from_db()
        assert rm.code_statut == 'V'          # compense (le service ne re-teste pas E)
        assert rm.credits_valides == 6
        res_v.refresh_from_db()
        res_e.refresh_from_db()
        res_nv.refresh_from_db()
        assert res_v.code_statut == 'V'       # V immuable
        assert res_v.est_valide is True
        assert res_e.code_statut == 'E'       # E immuable
        assert res_e.est_valide is False
        assert res_nv.code_statut == 'VCS'    # NV -> VCS (9.50 < 10 : pas VCI)
        assert res_nv.est_valide is True

    def test_rafraichir_idempotent(self, session, insc_ped, module):
        # Double execution = memes etats, aucune ligne supplementaire
        _, res_a = _ajoute_element(insc_ped, module, session, '7.00')
        _, res_b = _ajoute_element(insc_ped, module, session, '9.00')

        service = ResultatModuleService(session)
        rm = service.calculer(insc_ped, module)
        service.rafraichir_codes_apres_semestre(insc_ped, est_semestre_admis=True)
        service.rafraichir_codes_apres_semestre(insc_ped, est_semestre_admis=True)

        rm.refresh_from_db()
        assert rm.code_statut == 'V'
        assert rm.credits_valides == 6
        assert rm.est_valide is True
        for res in (res_a, res_b):
            res.refresh_from_db()
            assert res.code_statut == 'VCS'
            assert res.est_valide is True
        assert ResultatModule.objects.filter(
            inscription_ped=insc_ped, session=session,
        ).count() == 1
        assert ResultatElement.objects.filter(session=session).count() == 2


# ── credits_valides selon le statut du module ───────────────────────────────────
class TestCreditsValides:

    def test_credits_selon_statut_V_NV_puis_compensation(
        self, session, insc_ped, institution, semestre_S1, filiere_dlp,
    ):
        # Module A (4 credits) valide directement -> credits_valides = 4
        # Module B (9 credits) NV (9.00) -> 0, puis compense (Art. 14/15) -> 9
        module_a = ModuleLMDFactory(
            semestre=semestre_S1, filiere=filiere_dlp,
            institution=institution, credits=4,
        )
        module_b = ModuleLMDFactory(
            semestre=semestre_S1, filiere=filiere_dlp,
            institution=institution, credits=9,
        )
        _ajoute_element(insc_ped, module_a, session, '12.00')
        _ajoute_element(insc_ped, module_b, session, '9.00')

        service = ResultatModuleService(session)
        rm_a = service.calculer(insc_ped, module_a)
        rm_b = service.calculer(insc_ped, module_b)

        assert rm_a.est_valide is True
        assert rm_a.credits_valides == 4
        assert rm_b.est_valide is False
        assert rm_b.credits_valides == 0

        service.rafraichir_codes_apres_semestre(insc_ped, est_semestre_admis=True)

        rm_a.refresh_from_db()
        rm_b.refresh_from_db()
        assert rm_a.credits_valides == 4      # inchange
        assert rm_b.code_statut == 'V'
        assert rm_b.credits_valides == 9      # capitalisation Art. 15


# ── Calcul en lot : calculer_tous_modules_semestre ──────────────────────────────
class TestCalculerTousModulesSemestre:

    def test_filtre_actif_et_ecarte_les_none(
        self, session, insc_ped, institution, semestre_S1, filiere_dlp,
    ):
        # module_evalue : RE present -> RM calcule
        # module_sans_notes : IE sans RE -> calculer retourne None (ecarte)
        # module_inactif : actif=False -> pas calcule du tout
        module_evalue = ModuleLMDFactory(
            semestre=semestre_S1, filiere=filiere_dlp,
            institution=institution, credits=6,
        )
        module_sans_notes = ModuleLMDFactory(
            semestre=semestre_S1, filiere=filiere_dlp,
            institution=institution, credits=6,
        )
        module_inactif = ModuleLMDFactory(
            semestre=semestre_S1, filiere=filiere_dlp,
            institution=institution, credits=6, actif=False,
        )
        _ajoute_element(insc_ped, module_evalue, session, '12.00')
        em = EMLegacyFactory(module_lmd=module_sans_notes, semestre=insc_ped.semestre)
        InscriptionElementFactory(inscription_ped=insc_ped, em=em, element=None)
        _ajoute_element(insc_ped, module_inactif, session, '15.00')

        resultats = ResultatModuleService(session).calculer_tous_modules_semestre(insc_ped)

        assert len(resultats) == 1
        assert resultats[0].module_id == module_evalue.id
        assert resultats[0].moyenne == Decimal('12.00')
        assert ResultatModule.objects.filter(
            inscription_ped=insc_ped, session=session,
        ).count() == 1
