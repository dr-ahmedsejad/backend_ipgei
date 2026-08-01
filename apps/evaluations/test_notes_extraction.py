"""
Non-régression des méthodes NoteViewSet extraites mais non couvertes ailleurs :
agrege (lecture) et importer (import xlsx). Smoke tests exerçant le chemin complet
(lookup session, accès EM, parse) — ils auraient attrapé les imports manquants.

Base SQLite en mémoire (siga.settings.test).
"""
from io import BytesIO

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from tests.factories.parametres import InstitutionFactory, YearFactory
from tests.factories.evaluations import SessionNormaleImpairsFactory

User = get_user_model()


def _admin_client():
    admin = User.objects.create_user(
        username='admin_notes', email='an@test.local', password='x', role='admin',
    )
    client = APIClient()
    client.force_authenticate(admin)
    return client


@pytest.mark.django_db
def test_agrege_repond_200():
    institution = InstitutionFactory(est_principale=True)
    annee = YearFactory(annee='2025-2026')
    session = SessionNormaleImpairsFactory(institution=institution, annee_univ=annee)
    client = _admin_client()

    resp = client.get(f'/api/v1/evaluations/notes/agrege/?session={session.id}')
    assert resp.status_code == 200, resp.content[:400]


@pytest.mark.django_db
def test_importer_repond_200_sans_etudiant():
    from openpyxl import Workbook

    institution = InstitutionFactory(est_principale=True)
    annee = YearFactory(annee='2025-2026')
    session = SessionNormaleImpairsFactory(institution=institution, annee_univ=annee)
    client = _admin_client()

    wb = Workbook()
    ws = wb.active
    ws.append(['matricule', 'note_cc'])
    ws.append([999999, 12])
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    fichier = SimpleUploadedFile(
        'notes.xlsx', buf.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )

    resp = client.post(
        '/api/v1/evaluations/notes/importer/',
        {'fichier': fichier, 'session': session.id, 'element': 99999},
        format='multipart',
    )
    # Le chemin complet s'exécute (lookup session, accès EM admin, parse, recalc).
    # Aucun étudiant ne correspond -> 200, 0 créée, erreurs « matricule introuvable ».
    assert resp.status_code == 200, resp.content[:400]
    body = resp.json()
    assert body['created'] == 0
    assert body['errors']
