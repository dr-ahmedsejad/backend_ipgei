"""
Attestation de diplôme de licence professionnelle — contexte bilingue.

Fige :
  - les données viennent du REGISTRE DES DIPLÔMES (mention, filière, année,
    date du PV de jury), pas de la requête ;
  - la mention FR est traduite en arabe (Passable → مقبول) ;
  - les accords de genre FR + AR (étudiante / Née / استوفت / سلمت لها) ;
  - la génération est REFUSÉE si l'étudiant n'est pas au registre.
"""
from datetime import date
from decimal import Decimal

import pytest

from apps.parametres.models import Institution, Year, Niveau
from apps.scolarite.models import Filiere
from apps.departement.models import Departement
from apps.absence.models import Etudiant
from apps.documents.models import DocumentOfficiel, RegistreDiplome
from apps.documents.services import (
    _build_context_attestation_diplome, _creer_document_officiel,
)


@pytest.fixture
def diplomee(db):
    inst = Institution.objects.create(
        acronyme='ISS', nom='Institut Supérieur de la Statistique',
        nom_fr='Institut Supérieur de la Statistique', nom_ar='المعهد العالي للإحصاء',
        groupe_fr='Groupe Polytechnique', groupe_ar='مجمع بوليتكنيك',
        directeur_titre_fr="Le Directeur de l'Institut Supérieur de la Statistique",
        directeur_nom_ar='الدكتور أبوبكر المعلوم احميد',
        commandant_titre_fr='Le Commandant du Groupe Polytechnique',
        commandant_nom_ar='العقيد المهندس عثمان بكار اسويد احمد',
        est_principale=True,
    )
    niveau = Niveau.objects.create(niveau='L3')
    fil = Filiere.objects.create(
        code='SDID', intitule_fr='Science de Données et Informatique Décisionnelle',
        intitule_ar='علم البيانات والحوسبة القرارية', institution=inst,
    )
    dept = Departement.objects.create(nom='G1', institution=inst, filiere=fil, niveau=niveau)
    etu = Etudiant.objects.create(
        matricule='23656', nom='Hindou Bebay Boubi', departement=dept, genre='F',
        nom_fr='Bebay Boubi', prenom_fr='Hindou',
        nom_ar='ببايه بوبي', prenom_ar='هندو',
        cni='3909038988', date_naissance=date(2005, 10, 29),
        lieu_naissance_fr='Riyad', lieu_naissance_ar='الرياض', filiere=fil,
    )
    Year.objects.create(annee='2025-2026', est_active=True)
    RegistreDiplome.objects.create(
        institution=inst, etudiant=etu, filiere=fil, numero_diplome='DI-2026-00042',
        mention='Passable', moyenne_generale=Decimal('14.50'),  # stocké 'Passable' mais grille (14.5 ≥ 14) → Bien
        date_delivrance=date(2026, 6, 29), annee_universitaire='2025-2026',
    )
    doc = DocumentOfficiel.objects.create(
        institution=inst, etudiant=etu, type_document='attestation_diplome',
        numero_serie='AD-2026-00001', annee_universitaire='2025-2026',
    )
    return {'inst': inst, 'etu': etu, 'fil': fil, 'doc': doc}


class TestContexteAttestationDiplome:

    def test_donnees_registre_et_mention_bilingue(self, diplomee):
        ctx = _build_context_attestation_diplome(
            diplomee['doc'], diplomee['etu'], diplomee['inst'], {})
        assert ctx['matricule'] == '23656'
        assert ctx['nni'] == '3909038988'
        assert ctx['filiere_fr'] == 'Science de Données et Informatique Décisionnelle'
        assert ctx['filiere_ar'] == 'علم البيانات والحوسبة القرارية'
        assert ctx['annee'] == '2025-2026'
        assert ctx['jury_date'] == '29/06/2026'          # date_delivrance du registre
        assert ctx['date_naissance'] == '29/10/2005'
        # Mention via la GRILLE DIPLÔME calculée sur la moyenne (14.50 → Bien),
        # qui prime sur la mention stockée. + équivalent arabe.
        assert ctx['mention_fr'] == 'Bien'
        assert ctx['mention_ar'] == 'جيد'

    def test_accords_de_genre_feminin(self, diplomee):
        ctx = _build_context_attestation_diplome(
            diplomee['doc'], diplomee['etu'], diplomee['inst'], {})
        assert ctx['etu_label_fr'] == "L'étudiante"
        assert ctx['ne_fr'] == 'Née'
        assert ctx['etu_label_ar'] == 'الطالبة'
        assert ctx['satisf_ar'] == 'قد استوفت'
        assert ctx['delivree_ar'] == 'سلمت لها'
        assert ctx['nom_fr'] == 'Hindou Bebay Boubi'
        assert ctx['nom_ar'] == 'هندو ببايه بوبي'

    def test_signataires_depuis_config(self, diplomee):
        ctx = _build_context_attestation_diplome(
            diplomee['doc'], diplomee['etu'], diplomee['inst'], {})
        # Titre de FONCTION dérivé DYNAMIQUEMENT du nom de l'établissement (élision
        # « de l' »), et non l'honorifique (« Dr ») stocké dans directeur_titre_*.
        assert ctx['dir_titre_fr'] == "Directeur de l'Institut Supérieur de la Statistique"
        assert ctx['dir_titre_ar'] == 'مدير المعهد العالي للإحصاء'
        assert ctx['dir_nom_ar'] == 'الدكتور أبوبكر المعلوم احميد'
        assert ctx['cmd_titre_fr'] == 'Le Commandant du Groupe Polytechnique'
        assert ctx['cmd_nom_ar'] == 'العقيد المهندس عثمان بكار اسويد احمد'
        assert ctx['groupe_fr'] == 'Groupe Polytechnique'

    def test_generation_refusee_sans_registre(self, diplomee, django_user_model):
        """Un étudiant SANS diplôme au registre ne peut pas obtenir d'attestation."""
        autre = Etudiant.objects.create(
            matricule='99999', nom='Sans Diplome',
            departement=diplomee['etu'].departement, genre='M', filiere=diplomee['fil'],
        )
        with pytest.raises(ValueError):
            _creer_document_officiel(autre, 'attestation_diplome', '2025-2026', None, None)

    def test_genre_masculin(self, diplomee):
        etu_h = Etudiant.objects.create(
            matricule='23700', nom='Ahmed Salem', departement=diplomee['etu'].departement,
            genre='M', nom_fr='Salem', prenom_fr='Ahmed', filiere=diplomee['fil'],
        )
        ctx = _build_context_attestation_diplome(diplomee['doc'], etu_h, diplomee['inst'], {})
        assert ctx['etu_label_fr'] == "L'étudiant"
        assert ctx['ne_fr'] == 'Né'
        assert ctx['etu_label_ar'] == 'الطالب'
        assert ctx['satisf_ar'] == 'قد استوفى'
        assert ctx['delivree_ar'] == 'سلمت له'
