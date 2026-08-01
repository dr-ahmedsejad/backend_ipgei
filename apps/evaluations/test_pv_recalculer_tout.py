"""
Non-régression de l'action d'écriture PVDeliberation.recalculer_tout.

recalculer_tout recompose toute la chaîne (éléments → modules → semestres →
lignes + décisions). Sur un fixture synthétique (sans ResultatElement), le test
sert surtout de garde-fou de CÂBLAGE : il exerce le chemin complet et vérifie
que l'endpoint répond 200 avec la structure attendue. L'équivalence avant/après
extraction est la garantie de non-régression.

Base SQLite en mémoire (siga.settings.test).
"""
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from tests.factories.parametres import InstitutionFactory, NiveauFactory, SemestreFactory, YearFactory
from tests.factories.scolarite import FiliereFactory
from tests.factories.deliberation import PVDeliberationSemestrielFactory
from tests.factories.evaluations import SessionNormaleImpairsFactory, ResultatSemestreFactory
from tests.factories.inscriptions import (
    InscriptionAdministrativeFactory, InscriptionPedagogiqueFactory,
)

User = get_user_model()


@pytest.mark.django_db
def test_recalculer_tout_endpoint_repond_et_structure():
    institution = InstitutionFactory(est_principale=True)
    niveau = NiveauFactory(niveau='L1')
    semestre = SemestreFactory(
        code_semestre='S1', semestre='Semestre 1', type_semestre='I',
        niveau_semestre=niveau, credits=30,
    )
    annee = YearFactory(annee='2025-2026')
    filiere = FiliereFactory(institution=institution)
    session = SessionNormaleImpairsFactory(institution=institution, annee_univ=annee)
    pv = PVDeliberationSemestrielFactory(
        institution=institution, filiere=filiere, session=session,
        niveau=1, semestre_code='S1',
    )

    for _ in range(2):
        ia = InscriptionAdministrativeFactory(
            filiere=filiere, annee_univ=annee, niveau=1, institution=institution,
        )
        ip = InscriptionPedagogiqueFactory(inscription_admin=ia, semestre=semestre)
        ResultatSemestreFactory(
            inscription_ped=ip, session=session, moyenne=Decimal('14'),
            credits_valides=30, est_admis=True, code_statut='V',
        )

    admin = User.objects.create_user(
        username='admin_recalc', email='ar@test.local', password='x', role='admin',
    )
    client = APIClient()
    client.force_authenticate(admin)

    resp = client.post(f'/api/v1/evaluations/pvs/{pv.id}/recalculer-tout/')

    assert resp.status_code == 200, resp.content[:400]
    body = resp.json()
    for key in ('elements_recalcules', 'modules_recalcules', 'semestres_recalcules',
                'lignes_maj', 'decisions_calculees', 'obligations_generees'):
        assert key in body, body
        assert isinstance(body[key], int)
