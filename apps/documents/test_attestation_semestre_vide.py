"""
Régression (cas 23641) — l'attestation d'inscription ne doit PAS afficher un
semestre dont l'inscription pédagogique ne porte aucun élément.

Contexte : après suppression des dettes fantômes (InscriptionElement est_dette),
l'InscriptionPedagogique qui les contenait subsiste à vide. L'attestation listait
encore « Semestre 1 / Semestre 2 » alors qu'il n'y a plus rien à y attester.

Garde-fou (option 2) : `_build_context_inscription` filtre les semestres sans EM.
"""
import pytest

from apps.parametres.models import Institution, Year, Niveau, Semestre
from apps.scolarite.models import Filiere
from apps.departement.models import Departement
from apps.absence.models import Etudiant
from apps.em.models import EM
from apps.inscriptions.models import (
    InscriptionAdministrative, InscriptionPedagogique, InscriptionElement,
)
from apps.documents.models import DocumentOfficiel
from apps.documents.services import _build_context_inscription


@pytest.fixture
def scenario(db):
    inst   = Institution.objects.create(acronyme='TST', nom='T', est_principale=True)
    niveau = Niveau.objects.create(niveau='L3')
    fil    = Filiere.objects.create(code='SEA', intitule_fr='Stat éco', institution=inst)
    dept   = Departement.objects.create(nom='G1', institution=inst, filiere=fil, niveau=niveau)
    sem1   = Semestre.objects.create(code_semestre='S1', semestre='Semestre 1',
                                     niveau_semestre=niveau, type_semestre='I')
    sem5   = Semestre.objects.create(code_semestre='S5', semestre='Semestre 5',
                                     niveau_semestre=niveau, type_semestre='I')
    year   = Year.objects.create(annee='2025-2026', est_active=True)
    etu    = Etudiant.objects.create(matricule='23641', nom='Mama', departement=dept,
                                     genre='M', filiere=fil)
    ia = InscriptionAdministrative.objects.create(
        etudiant=etu, annee_univ=year, filiere=fil, institution=inst,
        niveau=3, numero_inscription='INS-23641')
    # S1 : coquille vide (les dettes ont été supprimées) — ne doit PAS apparaître.
    InscriptionPedagogique.objects.create(inscription_admin=ia, semestre=sem1)
    # S5 : semestre courant avec un élément — doit apparaître.
    ip5 = InscriptionPedagogique.objects.create(inscription_admin=ia, semestre=sem5)
    em5 = EM.objects.create(code_em='SEA171', intitule='Logiciels d\'enquête',
                            departement=dept, semestre=sem5, institution=inst)
    InscriptionElement.objects.create(inscription_ped=ip5, em=em5, est_dette=False)

    doc = DocumentOfficiel.objects.create(
        institution=inst, etudiant=etu, type_document='attestation_inscription',
        numero_serie='AI-2026-00001', annee_universitaire='2025-2026')
    return {'doc': doc, 'etu': etu, 'inst': inst}


def test_semestre_vide_masque(scenario):
    ctx = _build_context_inscription(scenario['doc'], scenario['etu'], scenario['inst'], {})
    sems = ctx['elements_par_semestre']
    # Seul S5 (avec EM) est présent ; S1 (coquille vide) est masqué.
    assert len(sems) == 1
    assert all(s['modules'] for s in sems)          # aucun semestre sans module
    codes = [e['code'] for s in sems for m in s['modules'] for e in m['elements']]
    assert 'SEA171' in codes


def test_aucun_semestre_si_tous_vides(scenario):
    # Si l'inscription n'a QUE des coquilles vides → tableau vide (rien à attester).
    InscriptionElement.objects.all().delete()
    ctx = _build_context_inscription(scenario['doc'], scenario['etu'], scenario['inst'], {})
    assert ctx['elements_par_semestre'] == []
