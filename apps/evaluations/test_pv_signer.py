"""
Non-régression de l'action d'écriture PVDeliberation.signer.

Vérifie l'EFFET DE BORD : signer crée/maj un MembreJury (pv, user) avec son
rôle et une date de signature. Garde-fou avant/après extraction vers un service.

Base SQLite en mémoire (siga.settings.test).
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from tests.factories.parametres import InstitutionFactory, YearFactory
from tests.factories.scolarite import FiliereFactory
from tests.factories.deliberation import PVDeliberationSemestrielFactory
from tests.factories.evaluations import SessionNormaleImpairsFactory
from apps.evaluations.models import MembreJury

User = get_user_model()


@pytest.mark.django_db
def test_signer_endpoint_cree_membre_jury():
    institution = InstitutionFactory(est_principale=True)
    annee = YearFactory(annee='2025-2026')
    filiere = FiliereFactory(institution=institution)
    session = SessionNormaleImpairsFactory(institution=institution, annee_univ=annee)
    pv = PVDeliberationSemestrielFactory(
        institution=institution, filiere=filiere, session=session,
        niveau=1, semestre_code='S1',
    )
    admin = User.objects.create_user(
        username='admin_signer', email='as@test.local', password='x', role='admin',
    )
    client = APIClient()
    client.force_authenticate(admin)

    assert MembreJury.objects.filter(pv=pv).count() == 0

    resp = client.post(
        f'/api/v1/evaluations/pvs/{pv.id}/signer/', {'role': 'president'}, format='json',
    )

    assert resp.status_code == 200, resp.content[:400]
    # EFFET DE BORD : un MembreJury signé est créé pour (pv, user).
    membre = MembreJury.objects.get(pv=pv, user=admin)
    assert membre.role == 'president'
    assert membre.signature_at is not None
