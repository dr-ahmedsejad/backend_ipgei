"""
Vérification publique (/verifier) — contrat du sérialiseur PUBLIC.

Fige :
  - les données de COMPARAISON sont exposées (nom, matricule, photo, filière,
    mention, année, type, validité) ;
  - AUCUNE donnée technique n'est exposée (hash, chemin de fichier, auteur, token).
"""
from datetime import date
from decimal import Decimal

import pytest

from apps.parametres.models import Institution, Year, Niveau
from apps.scolarite.models import Filiere
from apps.departement.models import Departement
from apps.absence.models import Etudiant
from apps.documents.models import DocumentOfficiel, RegistreDiplome
from apps.documents.serializers import DocumentVerificationSerializer
from apps.documents.views import VerificationThrottle


@pytest.fixture
def doc(db):
    inst = Institution.objects.create(acronyme='ISS', nom='ISS', nom_fr='ISS', est_principale=True)
    niv  = Niveau.objects.create(niveau='L3')
    fil  = Filiere.objects.create(code='SEA', intitule_fr='Stat éco', institution=inst)
    dept = Departement.objects.create(nom='G1', institution=inst, filiere=fil, niveau=niv)
    etu  = Etudiant.objects.create(matricule='23656', nom='Hindou Boubi', departement=dept,
                                   genre='F', nom_fr='Boubi', prenom_fr='Hindou', filiere=fil)
    Year.objects.create(annee='2025-2026', est_active=True)
    RegistreDiplome.objects.create(
        institution=inst, etudiant=etu, filiere=fil, numero_diplome='DI-1',
        mention='Bien', moyenne_generale=Decimal('14.5'),
        date_delivrance=date(2026, 6, 29), annee_universitaire='2025-2026')
    return DocumentOfficiel.objects.create(
        institution=inst, etudiant=etu, type_document='attestation_diplome',
        numero_serie='AD-2026-00001', annee_universitaire='2025-2026',
        hash_sha256='SECRET_HASH', est_valide=True)


def test_donnees_de_comparaison_exposees(doc):
    data = DocumentVerificationSerializer(doc).data
    assert data['etudiant_nom']       == 'Hindou Boubi'
    assert data['etudiant_matricule'] == '23656'
    assert data['type_libelle']       == 'Attestation de diplôme'
    assert data['filiere']            == 'Stat éco'
    assert data['mention']            == 'Bien'
    assert data['annee_universitaire'] == '2025-2026'
    assert data['est_valide'] is True
    assert 'photo_url' in data            # None ici (pas de photo), mais le champ existe


def test_aucune_donnee_technique_exposee(doc):
    data = DocumentVerificationSerializer(doc).data
    for champ in ('hash_sha256', 'fichier_pdf', 'genere_par', 'token_verification',
                  'institution', 'semestre'):
        assert champ not in data, f'Champ technique exposé : {champ}'


def test_throttle_scope_verify():
    assert VerificationThrottle.scope == 'verify'
