"""Règle conseil scientifique : un élément (EM) validé GRÂCE au rattrapage a sa
moyenne plafonnée — valeur PARAMÉTRABLE (défaut 10/20), figée par session.

Cible : NoteCalculService.appliquer_regle_maximum_rattrapage(moy_ord, moy_ratt, plafond)
— fonction pure (staticmethod), aucun accès BD. `plafond=None` => aucun plafond.
Runner : pytest.
"""
from decimal import Decimal

from apps.evaluations.services.calcul_notes import NoteCalculService

cap = NoteCalculService.appliquer_regle_maximum_rattrapage
P10 = Decimal('10')


def test_valide_grace_au_rattrapage_plafonne():
    # Échoué en normale (8), réussi au rattrapage (14), plafond 10 → 10.
    assert cap(Decimal('8'), Decimal('14'), P10) == Decimal('10')


def test_rattrapage_pile_a_10():
    assert cap(Decimal('5'), Decimal('10'), P10) == Decimal('10')


def test_deja_valide_en_normale_pas_de_plafond():
    # Validé dès la 1re session → garde la note la plus favorable, sans plafond.
    assert cap(Decimal('12'), Decimal('9'), P10) == Decimal('12')
    assert cap(Decimal('10'), Decimal('14'), P10) == Decimal('14')


def test_toujours_non_valide_inchange():
    # Échec aux deux sessions → note la plus favorable (< 10), inchangée.
    assert cap(Decimal('7'), Decimal('9'), P10) == Decimal('9')


def test_juste_sous_10_non_plafonne():
    # 9.99 reste 9.99 (non validé) — le plafond ne s'applique qu'à >= 10.
    assert cap(Decimal('9'), Decimal('9.99'), P10) == Decimal('9.99')


def test_plafond_none_desactive():
    # plafond=None (défaut) → Art. 18 pur, note la plus favorable, AUCUN plafond.
    assert cap(Decimal('8'), Decimal('14')) == Decimal('14')
    assert cap(Decimal('8'), Decimal('14'), None) == Decimal('14')


def test_plafond_parametrable_autre_valeur():
    # Plafond réglé à 12 → min(favorable, 12).
    assert cap(Decimal('8'), Decimal('14'), Decimal('12')) == Decimal('12')
    assert cap(Decimal('8'), Decimal('11'), Decimal('12')) == Decimal('11')
