"""
Non-régression de l'action d'ÉCRITURE PVDeliberation.peupler.

Contrairement aux extractions de lecture/export, on vérifie l'EFFET DE BORD :
peupler doit créer une LigneDeliberation par étudiant noté (et renvoyer les
compteurs). Garde-fou avant/après extraction vers un service.

Test autonome (ne dépend pas du conftest tests/, hors portée ici).
code_statut='V' sur les ResultatSemestre évite la branche de recalcul auto.

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
from apps.evaluations.models import LigneDeliberation

User = get_user_model()


@pytest.mark.django_db
def test_peupler_endpoint_cree_une_ligne_par_etudiant():
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
        username='admin_peupler', email='ap@test.local', password='x', role='admin',
    )
    client = APIClient()
    client.force_authenticate(admin)

    # AVANT : aucune ligne.
    assert LigneDeliberation.objects.filter(pv=pv).count() == 0

    resp = client.post(f'/api/v1/evaluations/pvs/{pv.id}/peupler/')

    assert resp.status_code == 200, resp.content[:400]
    body = resp.json()
    # EFFET DE BORD vérifié : 2 lignes créées en base + compteur cohérent.
    assert body['lignes_creees_ou_maj'] == 2, body
    assert LigneDeliberation.objects.filter(pv=pv).count() == 2
