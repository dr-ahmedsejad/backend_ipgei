"""
Non-régression des actions d'écriture PVDeliberation.clore / rouvrir.

Effet de bord vérifié : clore passe est_clos=True ; rouvrir (admin) repasse
est_clos=False. Garde-fou avant/après extraction vers services/pv_actions.

Base SQLite en mémoire (siga.settings.test).
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from tests.factories.parametres import InstitutionFactory, YearFactory
from tests.factories.scolarite import FiliereFactory
from tests.factories.deliberation import PVDeliberationSemestrielFactory
from tests.factories.evaluations import SessionNormaleImpairsFactory

User = get_user_model()


def _pv():
    institution = InstitutionFactory(est_principale=True)
    annee = YearFactory(annee='2025-2026')
    filiere = FiliereFactory(institution=institution)
    session = SessionNormaleImpairsFactory(institution=institution, annee_univ=annee)
    return PVDeliberationSemestrielFactory(
        institution=institution, filiere=filiere, session=session,
        niveau=1, semestre_code='S1',
    )


def _admin_client():
    admin = User.objects.create_user(
        username='admin_cr', email='cr@test.local', password='x', role='admin',
    )
    client = APIClient()
    client.force_authenticate(admin)
    return client


@pytest.mark.django_db
def test_clore_passe_est_clos_true():
    pv = _pv()
    client = _admin_client()
    assert pv.est_clos is False

    resp = client.post(f'/api/v1/evaluations/pvs/{pv.id}/clore/')

    assert resp.status_code == 200, resp.content[:400]
    pv.refresh_from_db()
    assert pv.est_clos is True


@pytest.mark.django_db
def test_rouvrir_passe_est_clos_false():
    pv = _pv()
    pv.est_clos = True
    pv.save(update_fields=['est_clos'])
    client = _admin_client()

    resp = client.post(f'/api/v1/evaluations/pvs/{pv.id}/rouvrir/')

    assert resp.status_code == 200, resp.content[:400]
    pv.refresh_from_db()
    assert pv.est_clos is False
