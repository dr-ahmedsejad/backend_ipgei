"""
Caractérisation de do_importer (Phase 3) : un étudiant inscrit à un EM, un xlsx
avec sa note CC -> la note est créée avec la bonne valeur. Fige le comportement
AVANT de décomposer la méthode (208 lignes).

Base SQLite en mémoire (siga.settings.test).
"""
from decimal import Decimal
from io import BytesIO

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from tests.factories.parametres import InstitutionFactory, NiveauFactory, SemestreFactory, YearFactory
from tests.factories.scolarite import FiliereFactory
from tests.factories.evaluations import SessionNormaleImpairsFactory
from tests.factories.em import EMLegacyFactory
from tests.factories.inscriptions import (
    InscriptionAdministrativeFactory, InscriptionPedagogiqueFactory, InscriptionElementFactory,
)
from apps.evaluations.models import Note

User = get_user_model()


@pytest.mark.django_db
def test_importer_cree_la_note_du_matricule():
    inst = InstitutionFactory(est_principale=True)
    niveau = NiveauFactory(niveau='L1')
    semestre = SemestreFactory(
        code_semestre='S1', semestre='S1', type_semestre='I',
        niveau_semestre=niveau, credits=30,
    )
    annee = YearFactory(annee='2025-2026')
    filiere = FiliereFactory(institution=inst)
    session = SessionNormaleImpairsFactory(institution=inst, annee_univ=annee)
    em = EMLegacyFactory(institution=inst, semestre=semestre)

    ia = InscriptionAdministrativeFactory(
        filiere=filiere, annee_univ=annee, niveau=1, institution=inst,
    )
    ip = InscriptionPedagogiqueFactory(inscription_admin=ia, semestre=semestre)
    ie = InscriptionElementFactory(inscription_ped=ip, em=em)
    matricule = ia.etudiant.matricule

    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(['matricule', 'note_cc'])
    ws.append([matricule, 14])
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    fichier = SimpleUploadedFile(
        'notes.xlsx', buf.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )

    admin = User.objects.create_user(
        username='admin_imp', email='ai@test.local', password='x', role='admin',
    )
    client = APIClient()
    client.force_authenticate(admin)

    resp = client.post(
        '/api/v1/evaluations/notes/importer/',
        {'fichier': fichier, 'session': session.id, 'element': em.id},
        format='multipart',
    )
    assert resp.status_code == 200, resp.content[:400]
    body = resp.json()
    # Comportement figé : 1 note créée, 0 erreur, valeur correcte en base.
    assert body['created'] == 1, body
    assert not body['errors'], body
    note = Note.objects.get(inscription_element=ie, session=session, type_note='CC')
    assert note.valeur == Decimal('14.00')
