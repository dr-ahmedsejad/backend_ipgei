"""
Smoke tests des endpoints reports extraits (émargement / collecte / levée PDF +
liste anonymats). Objectif : exercer le chemin complet (lookups, helpers) pour
attraper tout import manquant après extraction. pdfkit est mocké.

Base SQLite en mémoire (siga.settings.test).
"""
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from tests.factories.parametres import InstitutionFactory, NiveauFactory, SemestreFactory, YearFactory
from tests.factories.scolarite import FiliereFactory
from tests.factories.evaluations import SessionNormaleImpairsFactory

User = get_user_model()


def _client():
    admin = User.objects.create_user(
        username='admin_rep', email='ar@test.local', password='x', role='admin',
    )
    c = APIClient()
    c.force_authenticate(admin)
    return c


@pytest.mark.django_db
def test_list_anonymats_200():
    inst = InstitutionFactory(est_principale=True)
    annee = YearFactory(annee='2025-2026')
    session = SessionNormaleImpairsFactory(institution=inst, annee_univ=annee)
    resp = _client().get(f'/api/v1/evaluations/anonymats/?session={session.id}')
    assert resp.status_code == 200, resp.content[:300]


@pytest.mark.django_db
@patch('pdfkit.from_string', return_value=b'%PDF-1.4')
@patch('pdfkit.configuration', return_value=object())
def test_collecte_pdf_pas_de_500(_conf, _fs):
    inst = InstitutionFactory(est_principale=True)
    annee = YearFactory(annee='2025-2026')
    session = SessionNormaleImpairsFactory(institution=inst, annee_univ=annee)
    resp = _client().get(f'/api/v1/evaluations/collecte-notes/pdf/?session={session.id}')
    assert resp.status_code != 500, resp.content[:400]


@pytest.mark.django_db
@patch('pdfkit.from_string', return_value=b'%PDF-1.4')
@patch('pdfkit.configuration', return_value=object())
def test_levee_pdf_pas_de_500(_conf, _fs):
    inst = InstitutionFactory(est_principale=True)
    annee = YearFactory(annee='2025-2026')
    session = SessionNormaleImpairsFactory(institution=inst, annee_univ=annee)
    resp = _client().get(f'/api/v1/evaluations/anonymats/levee/pdf/?session={session.id}')
    assert resp.status_code != 500, resp.content[:400]


@pytest.mark.django_db
@patch('pdfkit.from_string', return_value=b'%PDF-1.4')
@patch('pdfkit.configuration', return_value=object())
def test_emargement_pdf_pas_de_500(_conf, _fs):
    inst = InstitutionFactory(est_principale=True)
    NiveauFactory(niveau='L1')
    SemestreFactory(code_semestre='S1', semestre='S1', type_semestre='I', credits=30)
    annee = YearFactory(annee='2025-2026')
    filiere = FiliereFactory(institution=inst)
    # annee_univ attend l'ID du Year (pas le libellé).
    resp = _client().get(
        f'/api/v1/evaluations/emargement/pdf/?filiere={filiere.id}&niveau=1&semestre=S1&annee_univ={annee.id}'
    )
    assert resp.status_code != 500, resp.content[:400]
