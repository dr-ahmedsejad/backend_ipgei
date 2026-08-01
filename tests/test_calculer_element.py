"""
Tests d'integration pour NoteCalculService.calculer_element().

Source legale :
- Arrete 562 Art. 12 : EM valide si moy >= 10
- Arrete 562 Art. 18 : MAX(SN, SR) en rattrapage
- Strategie A : note absente = 0/20

Couvre :
- creation ResultatElement avec valeurs correctes
- est_valide / est_eliminatoire selon seuils
- idempotence (recalcul sans creation duplicate)
- session rattrapage : MAX (SN, SR)
- session rattrapage : pas de note SR -> retombe sur SN
"""
from decimal import Decimal
import pytest

from apps.evaluations.services.calcul_notes import NoteCalculService
from tests.factories.evaluations import (
    SessionNormaleImpairsFactory, SessionRattrapageImpairsFactory, NoteFactory,
)
from tests.factories.inscriptions import InscriptionElementFactory
from tests.factories.em import ElementModuleFactory, EMLegacyFactory


@pytest.fixture
def session_normale(institution):
    return SessionNormaleImpairsFactory(institution=institution)


@pytest.fixture
def session_rattrapage(institution, session_normale):
    """Rattrapage liee a la meme institution + meme parite."""
    return SessionRattrapageImpairsFactory(
        institution=institution,
        annee_univ=session_normale.annee_univ,
    )


@pytest.fixture
def insc_element_sans_tp(db):
    """InscriptionElement avec EM legacy (has_tp=False)."""
    em = EMLegacyFactory(has_tp=False)
    elem = ElementModuleFactory(seuil_eliminatoire=Decimal('6.00'))
    return InscriptionElementFactory(em=em, element=elem)


@pytest.fixture
def insc_element_avec_tp(db):
    em = EMLegacyFactory(has_tp=True)
    elem = ElementModuleFactory(seuil_eliminatoire=Decimal('6.00'))
    return InscriptionElementFactory(em=em, element=elem)


# ── Calcul element session normale ─────────────────────────────────────────────
class TestCalculerElementNormale:

    def test_em_valide_avec_cc_12_exam_14(self, session_normale, insc_element_sans_tp):
        # (12*2 + 14*3) / 5 = 66/5 = 13.20 >= 10 -> valide
        NoteFactory(inscription_element=insc_element_sans_tp, session=session_normale,
                    type_note='CC', valeur=Decimal('12'))
        NoteFactory(inscription_element=insc_element_sans_tp, session=session_normale,
                    type_note='EXAM', valeur=Decimal('14'))

        service = NoteCalculService(session_normale)
        result = service.calculer_element(insc_element_sans_tp)

        assert result.note_finale == Decimal('13.20')
        assert result.est_valide is True
        assert result.est_eliminatoire is False

    def test_em_invalide_mais_pas_eliminatoire(self, session_normale, insc_element_sans_tp):
        # (8*2 + 8*3) / 5 = 40/5 = 8.00 -> < 10 invalide, mais >= 6 pas eliminatoire
        NoteFactory(inscription_element=insc_element_sans_tp, session=session_normale,
                    type_note='CC', valeur=Decimal('8'))
        NoteFactory(inscription_element=insc_element_sans_tp, session=session_normale,
                    type_note='EXAM', valeur=Decimal('8'))

        service = NoteCalculService(session_normale)
        result = service.calculer_element(insc_element_sans_tp)

        assert result.note_finale == Decimal('8.00')
        assert result.est_valide is False
        assert result.est_eliminatoire is False

    def test_em_eliminatoire_note_inferieure_seuil_6(self, session_normale, insc_element_sans_tp):
        # (4*2 + 5*3) / 5 = 23/5 = 4.60 < 6 -> eliminatoire
        NoteFactory(inscription_element=insc_element_sans_tp, session=session_normale,
                    type_note='CC', valeur=Decimal('4'))
        NoteFactory(inscription_element=insc_element_sans_tp, session=session_normale,
                    type_note='EXAM', valeur=Decimal('5'))

        service = NoteCalculService(session_normale)
        result = service.calculer_element(insc_element_sans_tp)

        assert result.note_finale == Decimal('4.60')
        assert result.est_valide is False
        assert result.est_eliminatoire is True

    def test_em_avec_tp_calcul_correct(self, session_normale, insc_element_avec_tp):
        # (12*2 + 15*1 + 14*3) / 6 = (24+15+42)/6 = 81/6 = 13.50
        NoteFactory(inscription_element=insc_element_avec_tp, session=session_normale,
                    type_note='CC', valeur=Decimal('12'))
        NoteFactory(inscription_element=insc_element_avec_tp, session=session_normale,
                    type_note='TP', valeur=Decimal('15'))
        NoteFactory(inscription_element=insc_element_avec_tp, session=session_normale,
                    type_note='EXAM', valeur=Decimal('14'))

        service = NoteCalculService(session_normale)
        result = service.calculer_element(insc_element_avec_tp)

        assert result.note_finale == Decimal('13.50')
        assert result.est_valide is True

    def test_aucune_note_donne_zero_eliminatoire(self, session_normale, insc_element_sans_tp):
        """Strategie A : pas de note = 0 -> eliminatoire."""
        service = NoteCalculService(session_normale)
        result = service.calculer_element(insc_element_sans_tp)

        assert result.note_finale == Decimal('0.00')
        assert result.est_valide is False
        assert result.est_eliminatoire is True

    def test_idempotence_recalcul_met_a_jour(self, session_normale, insc_element_sans_tp):
        """update_or_create : 2 appels = 1 seul ResultatElement, valeur a jour."""
        from apps.evaluations.models import ResultatElement

        NoteFactory(inscription_element=insc_element_sans_tp, session=session_normale,
                    type_note='CC', valeur=Decimal('10'))
        NoteFactory(inscription_element=insc_element_sans_tp, session=session_normale,
                    type_note='EXAM', valeur=Decimal('10'))

        service = NoteCalculService(session_normale)
        r1 = service.calculer_element(insc_element_sans_tp)
        # Modifier la note d'examen
        from apps.evaluations.models import Note
        n_exam = Note.objects.get(inscription_element=insc_element_sans_tp,
                                   session=session_normale, type_note='EXAM')
        n_exam.valeur = Decimal('15')
        n_exam.save()
        r2 = service.calculer_element(insc_element_sans_tp)

        assert r1.id == r2.id     # meme ligne (update, pas create)
        assert r2.note_finale == Decimal('13.00')  # (10*2 + 15*3)/5 = 65/5 = 13
        assert ResultatElement.objects.filter(
            inscription_element=insc_element_sans_tp, session=session_normale,
        ).count() == 1


# ── Session rattrapage : Art. 18 (MAX) ─────────────────────────────────────────
class TestCalculerElementRattrapage:

    def test_rattrapage_meilleur_que_normale_garde_rattrapage(
        self, session_normale, session_rattrapage, insc_element_sans_tp,
    ):
        # SN : CC=8, EXAM=8 -> 8.00 (insuffisant)
        NoteFactory(inscription_element=insc_element_sans_tp, session=session_normale,
                    type_note='CC', valeur=Decimal('8'))
        NoteFactory(inscription_element=insc_element_sans_tp, session=session_normale,
                    type_note='EXAM', valeur=Decimal('8'))
        # SR : EXAM=15 (CC herite de SN=8) -> (8*2 + 15*3)/5 = 61/5 = 12.20
        NoteFactory(inscription_element=insc_element_sans_tp, session=session_rattrapage,
                    type_note='EXAM', valeur=Decimal('15'))

        service = NoteCalculService(session_rattrapage)
        result = service.calculer_element(insc_element_sans_tp)

        assert result.note_finale == Decimal('12.20')   # MAX(8.00, 12.20)
        assert result.est_valide is True

    def test_rattrapage_pire_que_normale_garde_normale(
        self, session_normale, session_rattrapage, insc_element_sans_tp,
    ):
        # SN : CC=14, EXAM=14 -> 14.00
        NoteFactory(inscription_element=insc_element_sans_tp, session=session_normale,
                    type_note='CC', valeur=Decimal('14'))
        NoteFactory(inscription_element=insc_element_sans_tp, session=session_normale,
                    type_note='EXAM', valeur=Decimal('14'))
        # SR : EXAM=8 -> (14*2 + 8*3)/5 = 52/5 = 10.40
        NoteFactory(inscription_element=insc_element_sans_tp, session=session_rattrapage,
                    type_note='EXAM', valeur=Decimal('8'))

        service = NoteCalculService(session_rattrapage)
        result = service.calculer_element(insc_element_sans_tp)

        assert result.note_finale == Decimal('14.00')   # MAX(14.00, 10.40)
        assert result.est_valide is True

    def test_rattrapage_sans_note_exam_retombe_sur_sn(
        self, session_normale, session_rattrapage, insc_element_sans_tp,
    ):
        """Etudiant n'a pas pris le rattrapage : on garde la SN."""
        NoteFactory(inscription_element=insc_element_sans_tp, session=session_normale,
                    type_note='CC', valeur=Decimal('11'))
        NoteFactory(inscription_element=insc_element_sans_tp, session=session_normale,
                    type_note='EXAM', valeur=Decimal('9'))
        # Pas de note SR

        service = NoteCalculService(session_rattrapage)
        result = service.calculer_element(insc_element_sans_tp)

        # SN = (11*2 + 9*3)/5 = 49/5 = 9.80
        assert result.note_finale == Decimal('9.80')
        assert result.est_valide is False
