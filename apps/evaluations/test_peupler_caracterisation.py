"""
Test de CARACTÉRISATION de do_peupler (Phase 3 — décomposition).

Fige la réponse complète sur un fixture connu AVANT de décomposer la méthode en
sous-fonctions. Si la décomposition change le moindre comportement, ce test casse.

Fixture : PV semestriel normale, 2 étudiants admis (ResultatSemestre code_statut='V').

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
def test_peupler_reponse_complete_figee():
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
        username='admin_carac', email='c@test.local', password='x', role='admin',
    )
    client = APIClient()
    client.force_authenticate(admin)

    resp = client.post(f'/api/v1/evaluations/pvs/{pv.id}/peupler/')
    assert resp.status_code == 200, resp.content[:400]
    body = resp.json()

    # Comportement figé (valeurs déterministes pour ce fixture)
    assert body['lignes_creees_ou_maj'] == 2
    assert body['decisions_calculees'] == 2
    assert body['semestres_recalcules'] == 0   # code_statut='V' -> pas de recalcul
    assert body['sessions_pretes'] is True      # PV semestriel -> reste True
    # Types figés (valeurs data-dépendantes mais structure stable)
    assert isinstance(body['obligations_generees'], int)
    assert isinstance(body['warnings'], list)
