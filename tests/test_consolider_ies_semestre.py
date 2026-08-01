"""
Tests DIRECTS de `_consolider_ies_semestre` (apps/documents/services.py).

Oracle de non-régression MySQL → PostgreSQL : chaque valeur attendue est
dérivée À LA MAIN des 4 règles métier documentées dans la docstring :

  RÈGLE 1 — DERNIÈRE NOTE : CC et EXAM = dernière saisie (année la plus
            récente), écrasement INDIVIDUEL, même si la note est plus basse.
  RÈGLE 2 — TP PRÉSERVÉ  : le TP est TOUJOURS celui de l'IP d'origine.
  RÈGLE 3 — EM ACQUIS FIGÉ : statut V/VC/VCI/VCS à l'origine → notes immuables.
  RÈGLE 4 — FORMULE FIXE : ME = (CC*2 + EXAM*3 + TP*1)/6 si has_tp
                           ME = (CC*2 + EXAM*3)/5 sinon — absentes = 0,
                           arrondi ROUND_HALF_UP à 2 décimales.

+ caducité du rattrapage antérieur (fix bug 22640), filtre snapshot
  `annee <= annee_courante`, plafond rattrapage (Art. 18 max SN/SR).
"""
import pytest
from decimal import Decimal

from apps.documents.services import _consolider_ies_semestre

from tests.factories.parametres import YearFactory
from tests.factories.scolarite import EtudiantFactory
from tests.factories.em import EMLegacyFactory
from tests.factories.inscriptions import (
    InscriptionAdministrativeFactory,
    InscriptionPedagogiqueFactory,
    InscriptionElementFactory,
)
from tests.factories.evaluations import (
    SessionNormaleImpairsFactory,
    SessionRattrapageImpairsFactory,
    NoteFactory,
)


# Clés exactes du dict retourné (contrat de sortie — services.py lignes 880-884)
CLES_ATTENDUES = {
    'em', 'ie', 'annee_source', 'annee_source_id',
    'est_dette', 'est_courante', 'is_acquis',
    'cc', 'tp', 'exam', 'exam_rat', 'has_rat', 'me',
}


# ── Helpers locaux (aucun fichier partagé modifié) ─────────────────────────────

def _annee_avec_sessions(annee_str, institution, plafond=None):
    """Year + sessions normale/rattrapage Impairs. `plafond` active le snapshot
    rattrapage_plafond sur la session de rattrapage."""
    year = YearFactory(annee=annee_str)
    sn = SessionNormaleImpairsFactory(annee_univ=year, institution=institution)
    extra = {}
    if plafond is not None:
        extra = {
            'rattrapage_plafond_actif': True,
            'rattrapage_plafond':       Decimal(str(plafond)),
        }
    sr = SessionRattrapageImpairsFactory(annee_univ=year, institution=institution, **extra)
    return year, sn, sr


def _inscrire(etudiant, year, semestre, em, filiere, institution, est_dette=False):
    """Chaîne IA -> IP -> IE (chemin legacy pur : element=None)."""
    ia = InscriptionAdministrativeFactory(
        etudiant=etudiant, annee_univ=year,
        filiere=filiere, institution=institution,
    )
    ip = InscriptionPedagogiqueFactory(inscription_admin=ia, semestre=semestre)
    ie = InscriptionElementFactory(
        inscription_ped=ip, em=em, element=None, est_dette=est_dette,
    )
    return ie


def _note(ie, session, type_note, valeur):
    return NoteFactory(
        inscription_element=ie, session=session,
        type_note=type_note, valeur=Decimal(str(valeur)),
    )


# ── Fixtures locales ───────────────────────────────────────────────────────────

@pytest.fixture
def etudiant(db):
    return EtudiantFactory()


@pytest.fixture
def em_sans_tp(db, semestre_S1, institution):
    return EMLegacyFactory(has_tp=False, semestre=semestre_S1, institution=institution)


@pytest.fixture
def em_avec_tp(db, semestre_S1, institution):
    return EMLegacyFactory(has_tp=True, semestre=semestre_S1, institution=institution)


# ── Cas de base ────────────────────────────────────────────────────────────────

class TestCasDeBase:

    def test_aucune_inscription_pedagogique_renvoie_liste_vide(
            self, etudiant, semestre_S1, institution):
        annee, _, _ = _annee_avec_sessions('2025-2026', institution)
        assert _consolider_ies_semestre(etudiant, semestre_S1, annee) == []

    def test_semestre_simple_sans_dette_valeurs_exactes(
            self, etudiant, semestre_S1, filiere_dlp, institution, em_sans_tp):
        """1 seule année, CC=12 EXAM=14 sans TP.
        ME = (12*2 + 14*3) / 5 = 66/5 = 13.20 (règle 4, dénominateur fixe)."""
        annee, sn, _ = _annee_avec_sessions('2025-2026', institution)
        ie = _inscrire(etudiant, annee, semestre_S1, em_sans_tp, filiere_dlp, institution)
        _note(ie, sn, 'CC', 12)
        _note(ie, sn, 'EXAM', 14)

        res = _consolider_ies_semestre(etudiant, semestre_S1, annee)

        assert len(res) == 1
        d = res[0]
        assert d['em'].pk == em_sans_tp.pk
        assert d['ie'].pk == ie.pk
        assert d['annee_source'] == '2025-2026'
        assert d['annee_source_id'] == annee.pk
        assert d['est_dette'] is False
        assert d['est_courante'] is True
        assert d['is_acquis'] is False
        assert d['cc'] == 12.0
        assert d['tp'] is None
        assert d['exam'] == 14.0
        assert d['exam_rat'] is None
        assert d['has_rat'] is False
        assert d['me'] == 13.2

    def test_structure_exacte_des_cles_du_dict(
            self, etudiant, semestre_S1, filiere_dlp, institution, em_sans_tp):
        """Le contrat de sortie (13 clés) ne doit pas bouger MySQL → PG."""
        annee, sn, _ = _annee_avec_sessions('2025-2026', institution)
        ie = _inscrire(etudiant, annee, semestre_S1, em_sans_tp, filiere_dlp, institution)
        _note(ie, sn, 'EXAM', 10)

        res = _consolider_ies_semestre(etudiant, semestre_S1, annee)

        assert set(res[0].keys()) == CLES_ATTENDUES
        # EXAM seul saisi : ME = (0*2 + 10*3) / 5 = 6.00 (CC absent -> 0)
        assert res[0]['me'] == 6.0

    def test_max_sn_sr_intra_annee_art_18(
            self, etudiant, semestre_S1, filiere_dlp, institution, em_sans_tp):
        """Rattrapage la même année, sans plafond :
        ME_n = (10*2 + 8*3)/5  = 44/5 = 8.80
        ME_r = (10*2 + 12*3)/5 = 56/5 = 11.20
        ME final = max(8.80, 11.20) = 11.20."""
        annee, sn, sr = _annee_avec_sessions('2025-2026', institution)
        ie = _inscrire(etudiant, annee, semestre_S1, em_sans_tp, filiere_dlp, institution)
        _note(ie, sn, 'CC', 10)
        _note(ie, sn, 'EXAM', 8)
        _note(ie, sr, 'EXAM', 12)

        res = _consolider_ies_semestre(etudiant, semestre_S1, annee)

        d = res[0]
        assert d['exam_rat'] == 12.0
        assert d['has_rat'] is True
        assert d['me'] == 11.2

    def test_plafond_rattrapage_applique(
            self, etudiant, semestre_S1, filiere_dlp, institution, em_sans_tp):
        """Validation GRÂCE au rattrapage (ME_n=8.80 < 10, ME_r=11.20 >= 10)
        avec plafond snapshot 10.00 sur la session SR : ME = min(11.20, 10.00) = 10.00."""
        annee, sn, sr = _annee_avec_sessions('2025-2026', institution, plafond='10.00')
        ie = _inscrire(etudiant, annee, semestre_S1, em_sans_tp, filiere_dlp, institution)
        _note(ie, sn, 'CC', 10)
        _note(ie, sn, 'EXAM', 8)
        _note(ie, sr, 'EXAM', 12)

        res = _consolider_ies_semestre(etudiant, semestre_S1, annee)

        assert res[0]['me'] == 10.0


# ── Dette / dernière note (règle 1) ────────────────────────────────────────────

class TestDetteDerniereNote:

    def test_dette_derniere_note_retenue_meme_plus_basse(
            self, etudiant, semestre_S1, filiere_dlp, institution, em_sans_tp):
        """2024-2025 : CC=12 EXAM=14 (ME 13.20). Dette redoublée en 2025-2026 :
        CC=8 EXAM=6 → dernière saisie retenue même si plus basse.
        ME = (8*2 + 6*3) / 5 = 34/5 = 6.80."""
        annee1, sn1, _ = _annee_avec_sessions('2024-2025', institution)
        annee2, sn2, _ = _annee_avec_sessions('2025-2026', institution)
        ie1 = _inscrire(etudiant, annee1, semestre_S1, em_sans_tp, filiere_dlp, institution)
        ie2 = _inscrire(etudiant, annee2, semestre_S1, em_sans_tp, filiere_dlp,
                        institution, est_dette=True)
        _note(ie1, sn1, 'CC', 12)
        _note(ie1, sn1, 'EXAM', 14)
        _note(ie2, sn2, 'CC', 8)
        _note(ie2, sn2, 'EXAM', 6)

        res = _consolider_ies_semestre(etudiant, semestre_S1, annee2)

        assert len(res) == 1   # même code_em -> une seule ligne consolidée
        d = res[0]
        assert d['cc'] == 8.0
        assert d['exam'] == 6.0
        assert d['me'] == 6.8
        assert d['est_dette'] is True          # IE retenue = celle de la dette
        assert d['ie'].pk == ie2.pk
        assert d['annee_source'] == '2025-2026'
        assert d['annee_source_id'] == annee2.pk
        assert d['est_courante'] is True
        assert d['is_acquis'] is False

    def test_ecrasement_individuel_cc_seul_ressaisi(
            self, etudiant, semestre_S1, filiere_dlp, institution, em_sans_tp):
        """Seul le CC est ressaisi en dette (CC=9) : l'EXAM d'origine (14) reste.
        ME = (9*2 + 14*3) / 5 = 60/5 = 12.00."""
        annee1, sn1, _ = _annee_avec_sessions('2024-2025', institution)
        annee2, sn2, _ = _annee_avec_sessions('2025-2026', institution)
        ie1 = _inscrire(etudiant, annee1, semestre_S1, em_sans_tp, filiere_dlp, institution)
        ie2 = _inscrire(etudiant, annee2, semestre_S1, em_sans_tp, filiere_dlp,
                        institution, est_dette=True)
        _note(ie1, sn1, 'CC', 12)
        _note(ie1, sn1, 'EXAM', 14)
        _note(ie2, sn2, 'CC', 9)

        res = _consolider_ies_semestre(etudiant, semestre_S1, annee2)

        d = res[0]
        assert d['cc'] == 9.0
        assert d['exam'] == 14.0
        assert d['me'] == 12.0
        # L'occurrence 2025-2026 a des saisies -> elle devient l'IE retenue
        assert d['ie'].pk == ie2.pk
        assert d['annee_source'] == '2025-2026'

    def test_rattrapage_anterieur_devient_caduc_bug_22640(
            self, etudiant, semestre_S1, filiere_dlp, institution, em_sans_tp):
        """2024-2025 : CC=10 EXAM=5 + rattrapage 12. Retente en 2025-2026 avec
        EXAM=7 seul (pas de rattrapage) : l'ancien RAT ne doit PAS rester collé.
        ME = ME_n = (10*2 + 7*3) / 5 = 41/5 = 8.20 — has_rat False."""
        annee1, sn1, sr1 = _annee_avec_sessions('2024-2025', institution)
        annee2, sn2, _ = _annee_avec_sessions('2025-2026', institution)
        ie1 = _inscrire(etudiant, annee1, semestre_S1, em_sans_tp, filiere_dlp, institution)
        ie2 = _inscrire(etudiant, annee2, semestre_S1, em_sans_tp, filiere_dlp,
                        institution, est_dette=True)
        _note(ie1, sn1, 'CC', 10)
        _note(ie1, sn1, 'EXAM', 5)
        _note(ie1, sr1, 'EXAM', 12)
        _note(ie2, sn2, 'EXAM', 7)

        res = _consolider_ies_semestre(etudiant, semestre_S1, annee2)

        d = res[0]
        assert d['cc'] == 10.0        # CC non ressaisi -> origine conservée
        assert d['exam'] == 7.0
        assert d['exam_rat'] is None  # RAT 2024-2025 caduc
        assert d['has_rat'] is False
        assert d['me'] == 8.2

        # Snapshot 2024-2025 : le rattrapage d'origine reste visible.
        # ME_n = (10*2 + 5*3)/5 = 7.00 ; ME_r = (10*2 + 12*3)/5 = 11.20 -> max 11.20
        res_n1 = _consolider_ies_semestre(etudiant, semestre_S1, annee1)
        assert res_n1[0]['exam_rat'] == 12.0
        assert res_n1[0]['has_rat'] is True
        assert res_n1[0]['me'] == 11.2

    def test_occurrence_ulterieure_sans_aucune_saisie_ignoree(
            self, etudiant, semestre_S1, filiere_dlp, institution, em_sans_tp):
        """Réinscription en dette SANS aucune note saisie : l'origine reste
        intégralement retenue (y compris son rattrapage).
        ME_n = (12*2 + 14*3)/5 = 13.20 ; ME_r = (12*2 + 15*3)/5 = 13.80 -> 13.80."""
        annee1, sn1, sr1 = _annee_avec_sessions('2024-2025', institution)
        annee2, _, _ = _annee_avec_sessions('2025-2026', institution)
        ie1 = _inscrire(etudiant, annee1, semestre_S1, em_sans_tp, filiere_dlp, institution)
        _inscrire(etudiant, annee2, semestre_S1, em_sans_tp, filiere_dlp,
                  institution, est_dette=True)   # aucune note
        _note(ie1, sn1, 'CC', 12)
        _note(ie1, sn1, 'EXAM', 14)
        _note(ie1, sr1, 'EXAM', 15)

        res = _consolider_ies_semestre(etudiant, semestre_S1, annee2)

        d = res[0]
        assert d['ie'].pk == ie1.pk
        assert d['annee_source'] == '2024-2025'
        assert d['annee_source_id'] == annee1.pk
        assert d['est_dette'] is False           # IE retenue = origine (pas dette)
        assert d['est_courante'] is False
        assert d['cc'] == 12.0
        assert d['exam'] == 14.0
        assert d['exam_rat'] == 15.0
        assert d['has_rat'] is True
        assert d['me'] == 13.8


# ── TP préservé (règle 2) ──────────────────────────────────────────────────────

class TestTpPreserve:

    def test_tp_toujours_celui_de_l_origine(
            self, etudiant, semestre_S1, filiere_dlp, institution, em_avec_tp):
        """EM avec TP. Origine 2024-2025 : CC=10 TP=16 EXAM=8. Dette 2025-2026 :
        CC=12 EXAM=10 et un TP=5 saisi PAR ERREUR (jamais retapable).
        ME = (12*2 + 10*3 + 16*1) / 6 = 70/6 = 11.666... -> 11.67 (half-up)."""
        annee1, sn1, _ = _annee_avec_sessions('2024-2025', institution)
        annee2, sn2, _ = _annee_avec_sessions('2025-2026', institution)
        ie1 = _inscrire(etudiant, annee1, semestre_S1, em_avec_tp, filiere_dlp, institution)
        ie2 = _inscrire(etudiant, annee2, semestre_S1, em_avec_tp, filiere_dlp,
                        institution, est_dette=True)
        _note(ie1, sn1, 'CC', 10)
        _note(ie1, sn1, 'TP', 16)
        _note(ie1, sn1, 'EXAM', 8)
        _note(ie2, sn2, 'CC', 12)
        _note(ie2, sn2, 'TP', 5)     # ignoré (règle 2)
        _note(ie2, sn2, 'EXAM', 10)

        res = _consolider_ies_semestre(etudiant, semestre_S1, annee2)

        d = res[0]
        assert d['tp'] == 16.0
        assert d['cc'] == 12.0
        assert d['exam'] == 10.0
        assert d['me'] == 11.67


# ── EM acquis figé (règle 3) ───────────────────────────────────────────────────

class TestEmAcquisFige:

    def test_em_acquis_ignore_les_resaisies_ulterieures(
            self, etudiant, semestre_S1, filiere_dlp, institution, em_sans_tp):
        """Origine 2024-2025 validée (code_statut='V') CC=12 EXAM=14 -> ME 13.20.
        Ressaisies 2025-2026 (CC=5 EXAM=4) IGNORÉES : notes immuables."""
        annee1, sn1, _ = _annee_avec_sessions('2024-2025', institution)
        annee2, sn2, _ = _annee_avec_sessions('2025-2026', institution)
        ie1 = _inscrire(etudiant, annee1, semestre_S1, em_sans_tp, filiere_dlp, institution)
        ie2 = _inscrire(etudiant, annee2, semestre_S1, em_sans_tp, filiere_dlp, institution)
        _note(ie1, sn1, 'CC', 12)
        _note(ie1, sn1, 'EXAM', 14)
        # Le post_save de Note (apps/evaluations/signals.py) a déjà créé le
        # ResultatElement (code_statut='' par défaut) -> on pose le statut
        # acquis via update_or_create (unique_together inscription_element+session).
        from apps.evaluations.models import ResultatElement
        ResultatElement.objects.update_or_create(
            inscription_element=ie1, session=sn1,
            defaults={
                'note_finale': Decimal('13.20'),
                'est_valide':  True,
                'code_statut': 'V',
            },
        )
        _note(ie2, sn2, 'CC', 5)
        _note(ie2, sn2, 'EXAM', 4)

        res = _consolider_ies_semestre(etudiant, semestre_S1, annee2)

        assert len(res) == 1
        d = res[0]
        assert d['is_acquis'] is True
        assert d['est_dette'] is False
        assert d['cc'] == 12.0
        assert d['exam'] == 14.0
        assert d['me'] == 13.2
        assert d['ie'].pk == ie1.pk
        assert d['annee_source'] == '2024-2025'
        assert d['est_courante'] is False   # annee_courante=2025-2026 != origine


# ── Filtre annee_courante (snapshot) ───────────────────────────────────────────

class TestFiltreAnneeCourante:

    def _setup_deux_annees(self, etudiant, semestre, em, filiere, institution):
        annee1, sn1, _ = _annee_avec_sessions('2024-2025', institution)
        annee2, sn2, _ = _annee_avec_sessions('2025-2026', institution)
        ie1 = _inscrire(etudiant, annee1, semestre, em, filiere, institution)
        ie2 = _inscrire(etudiant, annee2, semestre, em, filiere, institution,
                        est_dette=True)
        _note(ie1, sn1, 'CC', 12)
        _note(ie1, sn1, 'EXAM', 14)
        _note(ie2, sn2, 'CC', 8)
        _note(ie2, sn2, 'EXAM', 6)
        return annee1, annee2

    def test_snapshot_annee_anterieure_ignore_les_annees_futures(
            self, etudiant, semestre_S1, filiere_dlp, institution, em_sans_tp):
        """Relevé 2024-2025 : les saisies 2025-2026 ne polluent pas le snapshot.
        ME = (12*2 + 14*3)/5 = 13.20."""
        annee1, _ = self._setup_deux_annees(
            etudiant, semestre_S1, em_sans_tp, filiere_dlp, institution)

        res = _consolider_ies_semestre(etudiant, semestre_S1, annee1)

        d = res[0]
        assert d['cc'] == 12.0
        assert d['exam'] == 14.0
        assert d['me'] == 13.2
        assert d['annee_source'] == '2024-2025'
        assert d['est_courante'] is True
        assert d['est_dette'] is False

    def test_annee_courante_none_consolide_toutes_les_annees(
            self, etudiant, semestre_S1, filiere_dlp, institution, em_sans_tp):
        """annee_courante=None : aucun filtre d'année (toutes années confondues,
        dernière saisie l'emporte) et est_courante=False partout.
        ME = (8*2 + 6*3)/5 = 6.80."""
        self._setup_deux_annees(
            etudiant, semestre_S1, em_sans_tp, filiere_dlp, institution)

        res = _consolider_ies_semestre(etudiant, semestre_S1, None)

        d = res[0]
        assert d['cc'] == 8.0
        assert d['exam'] == 6.0
        assert d['me'] == 6.8
        assert d['annee_source'] == '2025-2026'
        assert d['est_courante'] is False
