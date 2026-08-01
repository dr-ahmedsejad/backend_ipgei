"""
Tests des helpers de calcul (calcul_notes.py).

Sources legales :
- Arrete 562 (DLP) Art. 12-25 (Licence Professionnelle, seuil 65 %)
- Decret 2018-070 (DNI) Art. 17-26 (Diplome Ingenieur, seuil 75 %)

Code SIGA : apps/evaluations/services/calcul_notes.py
"""
from decimal import Decimal
import pytest

from apps.evaluations.services.calcul_notes import (
    _calculer_me_em,
    NoteCalculService,
)


@pytest.fixture
def params_default(db):
    """Ponderation institutionnelle par defaut : CC=2, EXAM=3, TP=1."""
    from apps.scolarite.models import ParametresPonderation
    return ParametresPonderation.get()


# ── Formule SANS TP ──────────────────────────────────────────────────────────────
class TestMeEmSansTp:
    """has_tp=False -> (CC * coeff_cc + EXAM * coeff_exam) / (coeff_cc + coeff_exam)
    Avec defaults CC=2, EXAM=3 -> divis = 5."""

    @pytest.mark.unit
    def test_cc_10_exam_15_donne_13(self, params_default):
        # (10*2 + 15*3) / 5 = 65/5 = 13.00
        result = _calculer_me_em(
            cc=Decimal('10'), tp=None, exam=Decimal('15'),
            has_tp=False, params=params_default,
        )
        assert result == Decimal('13.00')

    @pytest.mark.unit
    def test_cc_absent_compte_zero(self, params_default):
        # cc=None -> 0 ; (0*2 + 12*3)/5 = 36/5 = 7.20
        result = _calculer_me_em(
            cc=None, tp=None, exam=Decimal('12'),
            has_tp=False, params=params_default,
        )
        assert result == Decimal('7.20')

    @pytest.mark.unit
    def test_exam_absent_compte_zero(self, params_default):
        # cc=14, exam=None -> 0 ; (14*2 + 0*3)/5 = 28/5 = 5.60
        result = _calculer_me_em(
            cc=Decimal('14'), tp=None, exam=None,
            has_tp=False, params=params_default,
        )
        assert result == Decimal('5.60')

    @pytest.mark.unit
    def test_arrondi_round_half_up(self, params_default):
        # cc=12.5, exam=12.5 -> (12.5*2 + 12.5*3)/5 = 62.5/5 = 12.50
        result = _calculer_me_em(
            cc=Decimal('12.50'), tp=None, exam=Decimal('12.50'),
            has_tp=False, params=params_default,
        )
        assert result == Decimal('12.50')

    @pytest.mark.unit
    def test_arrondi_demi_centieme(self, params_default):
        # cc=11.34, exam=15.67 -> (22.68 + 47.01)/5 = 69.69/5 = 13.938 -> 13.94 (HALF_UP)
        result = _calculer_me_em(
            cc=Decimal('11.34'), tp=None, exam=Decimal('15.67'),
            has_tp=False, params=params_default,
        )
        assert result == Decimal('13.94')


# ── Formule AVEC TP ──────────────────────────────────────────────────────────────
class TestMeEmAvecTp:
    """has_tp=True -> (CC*coeff_cc + EXAM*coeff_exam + TP*coeff_tp) / (sum)
    Avec defaults : CC=2, EXAM=3, TP=1 -> divis = 6."""

    @pytest.mark.unit
    def test_cc_10_tp_15_exam_12_donne_11_83(self, params_default):
        # (10*2 + 15*1 + 12*3) / 6 = (20+15+36)/6 = 71/6 = 11.8333... -> 11.83 (HALF_UP)
        result = _calculer_me_em(
            cc=Decimal('10'), tp=Decimal('15'), exam=Decimal('12'),
            has_tp=True, params=params_default,
        )
        assert result == Decimal('11.83')

    @pytest.mark.unit
    def test_tp_absent_compte_zero(self, params_default):
        # cc=12, tp=None, exam=14 -> (24 + 0 + 42)/6 = 66/6 = 11.00
        result = _calculer_me_em(
            cc=Decimal('12'), tp=None, exam=Decimal('14'),
            has_tp=True, params=params_default,
        )
        assert result == Decimal('11.00')

    @pytest.mark.unit
    def test_toutes_notes_absentes_donne_zero(self, params_default):
        # cc=tp=exam=None -> 0
        result = _calculer_me_em(
            cc=None, tp=None, exam=None,
            has_tp=True, params=params_default,
        )
        assert result == Decimal('0.00')


# ── Mention (calculer_mention) ─────────────────────────────────────────────────
class TestCalculerMention:
    """Bareme : >=16 Tres Bien | >=14 Bien | >=12 Assez Bien | >=10 Passable | <10 Insuffisant."""

    @pytest.mark.unit
    @pytest.mark.parametrize('moyenne, attendu', [
        (Decimal('17.50'), 'Très Bien'),
        (Decimal('16.00'), 'Très Bien'),     # borne basse incluse
        (Decimal('15.99'), 'Bien'),
        (Decimal('14.00'), 'Bien'),
        (Decimal('13.99'), 'Assez Bien'),
        (Decimal('12.00'), 'Assez Bien'),
        (Decimal('11.99'), 'Passable'),
        (Decimal('10.00'), 'Passable'),
        (Decimal('9.99'),  'Insuffisant'),
        (Decimal('0.00'),  'Insuffisant'),
    ])
    def test_bornes_mention(self, moyenne, attendu):
        assert NoteCalculService.calculer_mention(moyenne) == attendu


# ── Regle MAX rattrapage (Art. 18 DLP / Art. 22 DNI) ───────────────────────────
class TestRegleMaxRattrapage:
    """L'etudiant garde la note la plus favorable entre normale et rattrapage."""

    @pytest.mark.unit
    def test_normale_meilleure(self):
        result = NoteCalculService.appliquer_regle_maximum_rattrapage(
            Decimal('15'), Decimal('12'),
        )
        assert result == Decimal('15')

    @pytest.mark.unit
    def test_rattrapage_meilleure(self):
        result = NoteCalculService.appliquer_regle_maximum_rattrapage(
            Decimal('8'), Decimal('14'),
        )
        assert result == Decimal('14')

    @pytest.mark.unit
    def test_egalite_donne_meme_valeur(self):
        result = NoteCalculService.appliquer_regle_maximum_rattrapage(
            Decimal('12.50'), Decimal('12.50'),
        )
        assert result == Decimal('12.50')

    @pytest.mark.unit
    def test_rattrapage_zero_garde_normale(self):
        # Etudiant n'est pas venu au rattrapage : note SR = 0, doit garder SN
        result = NoteCalculService.appliquer_regle_maximum_rattrapage(
            Decimal('11'), Decimal('0'),
        )
        assert result == Decimal('11')


# ── Progression annuelle (Art. 20 DLP — seuil 65 %) ────────────────────────────
class TestProgressionAnnuelle:
    """Code actuel : seuil hardcode 65 % (Arrete 562). Verrou S5 si S1+S2 non valides."""

    @pytest.mark.unit
    def test_passage_avec_60_credits_sur_60(self):
        """Plein taux -> passe."""
        r = NoteCalculService.calculer_progression_annuelle(
            credits_filiere=60, credits_capitalises=60, s1_s2_valides=True,
        )
        assert r['peut_progresser'] is True
        assert r['taux_capitalisation'] == 100.0
        assert r['bloque_s5'] is False

    @pytest.mark.unit
    def test_passage_avec_39_credits_sur_60_seuil_exact(self):
        """39/60 = 65 % pile -> passe (Art. 20)."""
        r = NoteCalculService.calculer_progression_annuelle(
            credits_filiere=60, credits_capitalises=39, s1_s2_valides=True,
        )
        assert r['peut_progresser'] is True
        assert r['taux_capitalisation'] == 65.0

    @pytest.mark.unit
    def test_blocage_avec_38_credits_sur_60(self):
        """38/60 = 63.3 % -> bloque."""
        r = NoteCalculService.calculer_progression_annuelle(
            credits_filiere=60, credits_capitalises=38, s1_s2_valides=True,
        )
        assert r['peut_progresser'] is False
        assert '63.3' in r['motif'] or '63.33' in r['motif']

    @pytest.mark.unit
    def test_blocage_zero_credit(self):
        r = NoteCalculService.calculer_progression_annuelle(
            credits_filiere=60, credits_capitalises=0, s1_s2_valides=False,
        )
        assert r['peut_progresser'] is False
        assert r['taux_capitalisation'] == 0.0

    @pytest.mark.unit
    def test_verrou_s5_actif_si_s1_s2_non_valides(self):
        """Meme si l'etudiant a 60 credits L2, S5 bloque tant que L1 (S1+S2) non valides."""
        r = NoteCalculService.calculer_progression_annuelle(
            credits_filiere=60, credits_capitalises=60,
            s1_s2_valides=False, demande_s5=True,
        )
        assert r['bloque_s5'] is True
        assert r['peut_progresser'] is False
        assert 'S1' in r['motif'] and 'S2' in r['motif']

    @pytest.mark.unit
    def test_verrou_s5_inactif_si_s1_s2_valides(self):
        r = NoteCalculService.calculer_progression_annuelle(
            credits_filiere=60, credits_capitalises=60,
            s1_s2_valides=True, demande_s5=True,
        )
        assert r['bloque_s5'] is False
        assert r['peut_progresser'] is True

    @pytest.mark.unit
    def test_verrou_s5_ignore_si_demande_s5_false(self):
        """Pas en S5 -> verrou n'a pas a s'appliquer."""
        r = NoteCalculService.calculer_progression_annuelle(
            credits_filiere=60, credits_capitalises=39,
            s1_s2_valides=False, demande_s5=False,
        )
        assert r['bloque_s5'] is False
        assert r['peut_progresser'] is True   # 65 % atteint, S5 non demande

    @pytest.mark.unit
    def test_motif_explicite_quand_blocage(self):
        r = NoteCalculService.calculer_progression_annuelle(
            credits_filiere=60, credits_capitalises=20, s1_s2_valides=True,
        )
        assert r['peut_progresser'] is False
        assert '65' in r['motif']     # mentionne le seuil reglementaire

    @pytest.mark.unit
    def test_credits_filiere_zero_evite_division(self):
        """Edge : pas de division par zero."""
        r = NoteCalculService.calculer_progression_annuelle(
            credits_filiere=0, credits_capitalises=0, s1_s2_valides=False,
        )
        assert r['taux_capitalisation'] == 0.0
        assert r['peut_progresser'] is False


# ── GAP REGLEMENTAIRE DNI : code n'implemente pas le seuil 75 % du Decret 2018-070 ──
class TestGapDniSeuil75Percent:
    """Le code actuel utilise 65 % (Arrete 562 LP) en dur.
    Pour le DNI (Decret 2018-070 Art. 24), le seuil est 75 % = 45/60 credits.

    Ces tests documentent le gap. xfail tant que le code ne separe pas DLP/DNI.
    """

    @pytest.mark.unit
    @pytest.mark.xfail(
        reason='Code hardcode 65 %. Pour DNI il faudrait parametrer le seuil par type_diplome.',
        strict=True,
    )
    def test_dni_45_credits_doit_passer_mais_44_doit_bloquer(self):
        # Selon Art. 24 DNI : 45/60 = 75 % exact -> passe
        r = NoteCalculService.calculer_progression_annuelle(
            credits_filiere=60, credits_capitalises=44, s1_s2_valides=True,
        )
        # 44 < 45 (75 %) -> doit bloquer pour DNI
        # mais code actuel autorise (44 > 65 %)
        assert r['peut_progresser'] is False


# ── Coherence pilote : la BD est bien isolee, l'institution est creee ───────────
class TestInfraIsoLation:
    """Smoke test pour valider que l'infra fonctionne et que les fixtures sont OK."""

    def test_institution_principale_creee(self, institution):
        assert institution.est_principale is True
        assert institution.acronyme == 'TEST'

    def test_semestres_distincts_S1_S2(self, semestre_S1, semestre_S2):
        assert semestre_S1.type_semestre == 'I'
        assert semestre_S2.type_semestre == 'P'
        assert semestre_S1.niveau_semestre.niveau == 'L1'
        assert semestre_S1.niveau_semestre.id == semestre_S2.niveau_semestre.id

    def test_filiere_dlp_par_defaut(self, filiere_dlp):
        assert filiere_dlp.type_diplome == 'LP'
        assert filiere_dlp.nb_semestres == 6
        assert filiere_dlp.credits_total == 180
