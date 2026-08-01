"""
Génération GROUPÉE de documents officiels (2026-06-27).

Vérifie `services.generer_documents_groupe` : sélection des étudiants concernés,
fusion en UN PDF, idempotence (réutilise les docs déjà créés), et erreur si aucun
étudiant. Le rendu PDF (wkhtmltopdf) est MOCKÉ → test rapide et hermétique.
"""
from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter
from django.contrib.auth import get_user_model

from apps.parametres.models import Institution, Year, Niveau, Semestre
from apps.scolarite.models import Filiere
from apps.departement.models import Departement
from apps.absence.models import Etudiant
from apps.inscriptions.models import (
    InscriptionAdministrative, InscriptionPedagogique, InscriptionElement,
)
from apps.em.models import EM
from apps.documents.models import DocumentOfficiel
from apps.documents import services

User = get_user_model()


def _mini_pdf() -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=72, height=72)
    b = BytesIO()
    w.write(b)
    return b.getvalue()


@pytest.fixture
def ctx(db):
    inst  = Institution.objects.create(acronyme='TST', nom='T', est_principale=True)
    niv   = Niveau.objects.create(niveau='L1')
    fil   = Filiere.objects.create(code='LP', intitule_fr='LP', institution=inst)
    other = Filiere.objects.create(code='XX', intitule_fr='XX', institution=inst)
    dept  = Departement.objects.create(nom='G1', institution=inst, filiere=fil, niveau=niv)
    sem   = Semestre.objects.create(code_semestre='S1', semestre='S1', niveau_semestre=niv, type_semestre='I')
    year  = Year.objects.create(annee='2024-2025', est_active=True)
    em    = EM.objects.create(code_em='M1', intitule='M1', departement=dept, semestre=sem, institution=inst)
    user  = User.objects.create_user(username='u', email='u@t.l', password='Xk93!plqz72', role='admin')

    def ins(mat, filiere, with_ip=False):
        etu = Etudiant.objects.create(matricule=mat, nom=mat, departement=dept, genre='M')
        adm = InscriptionAdministrative.objects.create(
            etudiant=etu, annee_univ=year, filiere=filiere, institution=inst,
            niveau=1, numero_inscription=f'INS-{mat}')
        if with_ip:
            ped = InscriptionPedagogique.objects.create(inscription_admin=adm, semestre=sem)
            InscriptionElement.objects.create(inscription_ped=ped, em=em)
        return etu

    ins('A', fil, with_ip=True)
    ins('B', fil, with_ip=True)
    ins('C', other, with_ip=True)  # autre filière → exclu
    return {'fil': fil, 'sem': sem, 'user': user}


@pytest.fixture(autouse=True)
def _mock_render(monkeypatch):
    # Évite wkhtmltopdf : chaque "document" = 1 page PDF valide.
    monkeypatch.setattr(services, '_generer_pdf', lambda *a, **k: _mini_pdf())


class TestGenerationGroupee:

    def test_attestation_selection_et_fusion(self, ctx):
        pdf, nb_ok, nb_total, err = services.generer_documents_groupe(
            'attestation_inscription', '2024-2025', ctx['fil'].id, None, ctx['user'])
        # A et B uniquement (C est dans une autre filière)
        assert (nb_ok, nb_total, err) == (2, 2, [])
        assert len(PdfReader(BytesIO(pdf)).pages) == 2
        assert DocumentOfficiel.objects.filter(type_document='attestation_inscription').count() == 2

    def test_releve_selection_par_semestre(self, ctx):
        pdf, nb_ok, nb_total, err = services.generer_documents_groupe(
            'releve_semestre', '2024-2025', ctx['fil'].id, ctx['sem'].id, ctx['user'])
        assert nb_ok == 2 and len(PdfReader(BytesIO(pdf)).pages) == 2

    def test_releve_sans_semestre_leve_valueerror(self, ctx):
        with pytest.raises(ValueError):
            services.generer_documents_groupe('releve_semestre', '2024-2025', ctx['fil'].id, None, ctx['user'])

    def test_idempotent_reutilise_les_docs(self, ctx):
        services.generer_documents_groupe('attestation_inscription', '2024-2025', ctx['fil'].id, None, ctx['user'])
        n1 = DocumentOfficiel.objects.count()
        services.generer_documents_groupe('attestation_inscription', '2024-2025', ctx['fil'].id, None, ctx['user'])
        n2 = DocumentOfficiel.objects.count()
        assert n1 == n2 == 2  # ré-exécution → réutilise, aucun nouveau numéro

    def test_aucun_etudiant_leve_valueerror(self, ctx):
        with pytest.raises(ValueError):
            services.generer_documents_groupe('attestation_inscription', '2099-2100', ctx['fil'].id, None, ctx['user'])
