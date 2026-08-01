"""
Régression — diplomation vs progression pour les filières tronc commun.

Bug (2026-06-25) : un étudiant LPSTAT admis en L1 était basculé en 'diplomation'
(« Diplômé — fin de cycle ») parce que niveau_source(1) >= filiere.niveau_fin(1),
alors que LPSTAT est un tronc commun (L1 uniquement) dont le cursus se PROLONGE
chez ses filles SDID/SEA (L2-L3). Il devait être 'progression' → orienté vers une
fille, pas diplômé.

Le correctif clé sur la relation parent/filles du MÊME type de diplôme :
  - une filière dont le cursus se prolonge chez des filles du même diplôme n'est
    jamais un palier de diplôme → progression / à orienter ;
  - une filière complète diplôme bien à son niveau terminal (niveau_fin) ;
  - une fille MASTER (autre diplôme) ne bloque PAS la diplomation de la mère.
"""
import pytest

from apps.inscriptions.services.progression import ProgressionService
from apps.scolarite.models import Filiere


@pytest.fixture
def filieres(db):
    parent = Filiere.objects.create(
        code='TC-L1', intitule_fr='Tronc commun L1',
        type_diplome='LP', niveau_debut=1, niveau_fin=1,
    )
    fille_a = Filiere.objects.create(
        code='SPE-A', intitule_fr='Spécialité A',
        type_diplome='LP', niveau_debut=2, niveau_fin=3, filiere_parent=parent,
    )
    fille_b = Filiere.objects.create(
        code='SPE-B', intitule_fr='Spécialité B',
        type_diplome='LP', niveau_debut=2, niveau_fin=3, filiere_parent=parent,
    )
    complete = Filiere.objects.create(
        code='LIC', intitule_fr='Licence complète',
        type_diplome='LP', niveau_debut=1, niveau_fin=3,
    )
    return {'parent': parent, 'fille_a': fille_a, 'fille_b': fille_b, 'complete': complete}


class TestResolveDecisionDiplomation:

    def test_tronc_commun_l1_admis_reste_progression(self, filieres):
        # LPSTAT-like : L1 admis (niveau_source == niveau_fin == 1) → PAS diplômé.
        d = ProgressionService._resolve_decision_with_diplomation('progression', 1, filieres['parent'])
        assert d == 'progression'

    def test_filiere_complete_l3_diplome(self, filieres):
        d = ProgressionService._resolve_decision_with_diplomation('progression', 3, filieres['complete'])
        assert d == 'diplomation'

    def test_filiere_complete_l1_reste_progression(self, filieres):
        d = ProgressionService._resolve_decision_with_diplomation('progression', 1, filieres['complete'])
        assert d == 'progression'

    def test_fille_l3_diplome(self, filieres):
        # SEA/SDID-like : L3 admis, pas de filles → diplômé.
        d = ProgressionService._resolve_decision_with_diplomation('progression', 3, filieres['fille_a'])
        assert d == 'diplomation'

    def test_filles_autre_diplome_ne_bloque_pas_la_diplomation(self, filieres):
        # Licence complète avec une fille MASTER (nouveau cycle) → diplôme bien à L3.
        Filiere.objects.create(
            code='MAS', intitule_fr='Master',
            type_diplome='M', niveau_debut=1, niveau_fin=2,
            filiere_parent=filieres['complete'],
        )
        d = ProgressionService._resolve_decision_with_diplomation('progression', 3, filieres['complete'])
        assert d == 'diplomation'

    def test_decision_non_progression_inchangee(self, filieres):
        for dec in ('redoublement', 'annee_blanche', 'exclusion'):
            assert ProgressionService._resolve_decision_with_diplomation(dec, 1, filieres['parent']) == dec


class TestNiveauCible:

    def test_tronc_commun_progression_ne_leve_pas(self, filieres):
        # LPSTAT L1 → niveau_cible 2 (chez la fille), pas de ValueError.
        assert ProgressionService._niveau_cible('progression', 1, filieres['parent']) == 2

    def test_filiere_complete_depassement_leve_valueerror(self, filieres):
        # Vraie incohérence (filière complète, sans filles) → garde-fou conservé.
        with pytest.raises(ValueError):
            ProgressionService._niveau_cible('progression', 3, filieres['complete'])

    def test_progression_normale_increment(self, filieres):
        assert ProgressionService._niveau_cible('progression', 2, filieres['fille_a']) == 3

    def test_diplomation_et_exclusion_none(self, filieres):
        assert ProgressionService._niveau_cible('diplomation', 3, filieres['complete']) is None
        assert ProgressionService._niveau_cible('exclusion', 1, filieres['complete']) is None

    def test_redoublement_meme_niveau(self, filieres):
        assert ProgressionService._niveau_cible('redoublement', 2, filieres['fille_a']) == 2


class TestFiliereCible:

    def test_tronc_commun_a_orienter_none(self, filieres):
        # Progression tronc commun → filiere_cible None (à orienter SDID/SEA).
        assert ProgressionService._filiere_cible('progression', 2, filieres['parent']) is None

    def test_progression_normale_reste_filiere(self, filieres):
        # Fille L2→L3 : reste dans la même filière.
        assert ProgressionService._filiere_cible('progression', 3, filieres['fille_a']) == filieres['fille_a']

    def test_redoublement_reste_filiere(self, filieres):
        assert ProgressionService._filiere_cible('redoublement', 1, filieres['parent']) == filieres['parent']

    def test_diplomation_et_exclusion_none(self, filieres):
        assert ProgressionService._filiere_cible('diplomation', None, filieres['complete']) is None
        assert ProgressionService._filiere_cible('exclusion', None, filieres['complete']) is None
