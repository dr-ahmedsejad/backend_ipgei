"""
Garde-fou import rattrapage (2026-06-27).

Problème : la feuille de saisie manuelle (build_feuille) masque, en rattrapage,
les étudiants non concernés (ni obligation, ni dette non validée). Mais l'import
xlsx (do_importer) écrivait une note SR pour N'IMPORTE quel inscrit à l'EM, sans
ce filtre → des notes SR atterrissaient sur des non-concernés.

Fix : _importer_lignes reçoit l'ensemble éligible (eligible_rattrapage_ie_ids, la
MÊME source que la feuille) et IGNORE les IE hors de cet ensemble, avec une ligne
d'erreur. En session normale (eligible_ids=None) : aucun filtre.

Ce test vérifie le mécanisme de garde-fou de _importer_lignes.
"""
from decimal import Decimal

import openpyxl
import pytest
from django.contrib.auth import get_user_model

from apps.parametres.models import Institution, Year, Niveau, Semestre
from apps.scolarite.models import Filiere
from apps.departement.models import Departement
from apps.absence.models import Etudiant
from apps.em.models import EM
from apps.inscriptions.models import (
    InscriptionAdministrative, InscriptionPedagogique, InscriptionElement,
)
from apps.evaluations.models import SessionEvaluation, Note
from apps.evaluations.services.note_saisie import _importer_lignes

User = get_user_model()


@pytest.fixture
def ctx(db):
    inst = Institution.objects.create(acronyme='TST', nom='T', est_principale=True)
    niv  = Niveau.objects.create(niveau='L1')
    fil  = Filiere.objects.create(code='LP', intitule_fr='LP', institution=inst)
    dept = Departement.objects.create(nom='G1', institution=inst, filiere=fil, niveau=niv)
    sem  = Semestre.objects.create(code_semestre='S1', semestre='S1', niveau_semestre=niv, type_semestre='I')
    year = Year.objects.create(annee='2024-2025', est_active=True)
    em   = EM.objects.create(code_em='ST71', intitule='Proba', departement=dept, semestre=sem, institution=inst)
    sr   = SessionEvaluation.objects.create(annee_univ=year, institution=inst,
                                            type_session='rattrapage', type_semestre='Impairs')
    user = User.objects.create_user(username='imp', email='i@t.l', password='Xk93!plqz72', role='admin')

    def make_ie(code):
        etu = Etudiant.objects.create(matricule=code, nom=code, departement=dept, genre='M')
        adm = InscriptionAdministrative.objects.create(
            etudiant=etu, annee_univ=year, filiere=fil, institution=inst,
            niveau=1, numero_inscription=f'INS-{code}')
        ped = InscriptionPedagogique.objects.create(inscription_admin=adm, semestre=sem)
        return InscriptionElement.objects.create(inscription_ped=ped, em=em, est_dette=False)

    return {
        'sr': sr, 'user': user,
        'ie_elig':    make_ie('ELIG'),
        'ie_nonelig': make_ie('NONELIG'),
    }


def _ws(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['matricule', 'note_exam'])
    for r in rows:
        ws.append(list(r))
    return ws


HEADERS = {'matricule': 0, 'note_exam': 1}
COL_MAP = {'EXAM': 'note_exam'}


class TestImportGardeFouRattrapage:

    def test_rattrapage_rejette_non_eligible(self, ctx):
        ws = _ws([('ELIG', 12), ('NONELIG', 14)])
        ie_index = {'ELIG': ctx['ie_elig'], 'NONELIG': ctx['ie_nonelig']}
        eligible = {ctx['ie_elig'].id}  # seul ELIG est éligible

        created, updated, errors = _importer_lignes(
            ws, HEADERS, COL_MAP, ie_index, ctx['sr'], ctx['user'], eligible)

        # ELIG : note écrite
        assert Note.objects.filter(
            inscription_element=ctx['ie_elig'], session=ctx['sr'],
            type_note='EXAM', valeur=Decimal('12')).exists()
        # NONELIG : aucune note + ligne d'erreur explicite
        assert not Note.objects.filter(
            inscription_element=ctx['ie_nonelig'], session=ctx['sr']).exists()
        assert any('non concerné' in e['message'] for e in errors)

    def test_session_normale_aucun_filtre(self, ctx):
        # eligible_ids=None (cas session normale) → tous les inscrits saisissables.
        ws = _ws([('ELIG', 12), ('NONELIG', 14)])
        ie_index = {'ELIG': ctx['ie_elig'], 'NONELIG': ctx['ie_nonelig']}

        created, updated, errors = _importer_lignes(
            ws, HEADERS, COL_MAP, ie_index, ctx['sr'], ctx['user'], None)

        assert Note.objects.filter(inscription_element=ctx['ie_elig'], session=ctx['sr']).exists()
        assert Note.objects.filter(inscription_element=ctx['ie_nonelig'], session=ctx['sr']).exists()
        assert not any('non concerné' in e['message'] for e in errors)
