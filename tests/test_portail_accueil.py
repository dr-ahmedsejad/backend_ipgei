"""
GET /portail/accueil/ : l'accueil de l'app en une requête. Chaque partie est
la réponse EXACTE de sa vue habituelle (même format, lu par le même code dans
l'app) ; une partie en échec vaut null sans empêcher les autres.
"""
from decimal import Decimal

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from tests.factories.auth import EtudiantUserFactory
from tests.factories.em import EMLegacyFactory
from tests.factories.evaluations import ResultatElementFactory, SessionNormaleImpairsFactory
from tests.factories.inscriptions import (InscriptionAdministrativeFactory,
                                          InscriptionElementFactory, InscriptionPedagogiqueFactory)
from tests.factories.parametres import SemestreFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def etudiant():
    cache.clear()
    ia = InscriptionAdministrativeFactory()
    ip = InscriptionPedagogiqueFactory(inscription_admin=ia)
    el = InscriptionElementFactory(inscription_ped=ip, em=EMLegacyFactory(semestre=SemestreFactory()))
    ResultatElementFactory(inscription_element=el, session=SessionNormaleImpairsFactory(),
                           note_finale=Decimal('13.00'), est_valide=True)
    e = ia.etudiant
    e.user = EtudiantUserFactory(username=f'etu_accueil_{e.pk}')
    e.save(update_fields=['user'])
    client = APIClient()
    client.force_authenticate(e.user)
    return client, e.user


def test_chaque_partie_est_la_reponse_de_sa_vue(etudiant):
    client, _ = etudiant
    r = client.get('/api/v1/portail/accueil/')
    assert r.status_code == 200, r.data
    assert set(r.data) == {'notes', 'absences', 'profil', 'emploi', 'periodes', 'non_lues', 'notifications'}
    for partie, url in [('notes', 'notes/'), ('absences', 'absences/'), ('profil', 'profil/'),
                        ('emploi', 'emploi-du-temps/'), ('periodes', 'reclamations/periodes-actives/')]:
        seule = client.get(f'/api/v1/portail/{url}')
        assert seule.status_code == 200, (partie, seule.data)
        assert r.data[partie] == seule.data, partie


def test_non_lues_et_derniere_notification(etudiant):
    from apps.notifications.models import Notification
    client, user = etudiant
    Notification.objects.create(destinataire=user, titre='Ancienne', message='a', lue=True)
    Notification.objects.create(destinataire=user, titre='Récente', message='b')
    r = client.get('/api/v1/portail/accueil/')
    assert r.data['non_lues'] == 1
    assert [n['titre'] for n in r.data['notifications']] == ['Récente']
    assert r.data['notifications'][0]['lue'] is False
    assert r.data['non_lues'] == client.get('/api/v1/notifications/unread-count/').data['count']


def test_une_partie_en_echec_n_empeche_pas_les_autres(etudiant, monkeypatch):
    from apps.portail import views
    client, _ = etudiant

    def panne(self, request):
        raise RuntimeError('panne')
    monkeypatch.setattr(views.MesAbsencesView, 'get', panne)
    r = client.get('/api/v1/portail/accueil/')
    assert r.status_code == 200
    assert r.data['absences'] is None
    assert r.data['notes'] is not None


def test_reserve_aux_etudiants():
    assert APIClient().get('/api/v1/portail/accueil/').status_code in (401, 403)
