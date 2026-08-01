"""
Tests — contrôle « minimum deux notes par EM ».
Art. 12 Arrêté 562 / Art. 17 Décret 2018-070 : « L'évaluation de chaque
élément de module doit faire l'objet d'un minimum de deux notes. »

Contrôle non bloquant exposé dans les warnings de POST /pvs/{id}/peupler/.
"""
from decimal import Decimal

import pytest

from apps.evaluations.services.coherence_maquette import verifier_minimum_deux_notes
from tests.factories.evaluations import (
    SessionNormaleImpairsFactory, SessionRattrapageImpairsFactory, NoteFactory,
)
from tests.factories.inscriptions import InscriptionElementFactory
from tests.factories.parametres import YearFactory


@pytest.fixture
def annee(institution):
    return YearFactory(annee='2025-2026')


@pytest.fixture
def session_normale(institution, annee):
    return SessionNormaleImpairsFactory(institution=institution, annee_univ=annee)


def test_une_seule_composante_signale(db, session_normale):
    """EM avec seulement EXAM saisi → warning Art. 12/17."""
    ie = InscriptionElementFactory(element=None)
    NoteFactory(
        inscription_element=ie, session=session_normale,
        type_note='EXAM', valeur=Decimal('10.00'),
    )

    warns = verifier_minimum_deux_notes(session_normale)
    assert len(warns) == 1
    assert 'EXAM' in warns[0]
    assert 'Art. 12' in warns[0] and 'Art. 17' in warns[0]


def test_deux_composantes_ok(db, session_normale):
    """EM avec CC + EXAM saisis → aucun warning."""
    ie = InscriptionElementFactory(element=None)
    NoteFactory(inscription_element=ie, session=session_normale,
                type_note='CC', valeur=Decimal('11.00'))
    NoteFactory(inscription_element=ie, session=session_normale,
                type_note='EXAM', valeur=Decimal('10.00'))

    assert verifier_minimum_deux_notes(session_normale) == []


def test_tp_compte_comme_note(db, session_normale):
    """TP + EXAM = deux notes (Décret Art. 17 : « moyenne pondérée des notes »)."""
    ie = InscriptionElementFactory(element=None)
    NoteFactory(inscription_element=ie, session=session_normale,
                type_note='TP', valeur=Decimal('13.00'))
    NoteFactory(inscription_element=ie, session=session_normale,
                type_note='EXAM', valeur=Decimal('10.00'))

    assert verifier_minimum_deux_notes(session_normale) == []


def test_rattrapage_exclu(db, institution, annee):
    """Session de rattrapage : seul l'examen est repassé → contrôle inactif."""
    session_sr = SessionRattrapageImpairsFactory(
        institution=institution, annee_univ=annee,
    )
    ie = InscriptionElementFactory(element=None)
    NoteFactory(inscription_element=ie, session=session_sr,
                type_note='EXAM', valeur=Decimal('12.00'))

    assert verifier_minimum_deux_notes(session_sr) == []


def test_em_sans_aucune_note_ignore(db, session_normale):
    """EM sans note du tout : ignoré (saisie pas commencée, autre sujet)."""
    InscriptionElementFactory(element=None)
    assert verifier_minimum_deux_notes(session_normale) == []
