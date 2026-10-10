"""
Notifications push de l'IPGEI (app Groupe Polytechnique) — reprises de l'ISS.
Firebase n'est JAMAIS appelé : push.envoyer est simulé.
"""
import threading

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from apps.notifications import push
from apps.notifications.models import AppareilPush, Notification, PushEnvoye

pytestmark = pytest.mark.django_db


def _compte(username, role='etudiant'):
    from apps.authentication.models import CustomUser
    return CustomUser.objects.create_user(username=username, email=f'{username}@ipgei.mr',
                                          password='Xk93!plqz72', role=role)


@pytest.fixture
def envois(monkeypatch, settings):
    settings.ETABLISSEMENT_CODE, settings.ETABLISSEMENT_SIGLE = 'ipgei', 'IPGEI'
    monkeypatch.setattr(push, 'cle_gp', lambda: '/app/secrets/firebase-gp-ipgei.json')
    monkeypatch.setattr(push, 'configure', lambda chemin=None: True)
    faits, fils = [], set()

    def envoyer(jeton, titre, corps, donnees=None, chemin=None):
        fils.add(threading.get_ident())
        if jeton.startswith('PERIME'):
            raise push.JetonInvalide('UNREGISTERED')
        faits.append({'jeton': jeton, 'titre': titre, 'donnees': donnees, 'cle': chemin})

    monkeypatch.setattr(push, 'envoyer', envoyer)
    return faits, fils


def test_sans_cle_rien_ne_part(monkeypatch):
    monkeypatch.setattr(push, 'cle_gp', lambda: '')
    u = _compte('e0')
    AppareilPush.objects.create(user_id=u.pk, jeton='J0')
    Notification.objects.create(destinataire=u, titre='X', message='y')
    call_command('envoyer_push')
    assert not PushEnvoye.objects.exists()


def test_le_sigle_l_etablissement_et_le_profil(envois):
    faits, _ = envois
    etu, ens = _compte('e1'), _compte('p1', role='enseignant')
    AppareilPush.objects.create(user_id=etu.pk, jeton='JE')
    AppareilPush.objects.create(user_id=ens.pk, jeton='JP')
    Notification.objects.create(destinataire=etu, titre='Résultats', message='S3', lien='/notes')
    Notification.objects.create(destinataire=ens, titre='Réclamation reçue', message='MP A')
    call_command('envoyer_push')
    par = {f['jeton']: f for f in faits}
    assert par['JE']['titre'] == 'IPGEI — Résultats'
    assert par['JE']['donnees']['etablissement'] == 'ipgei' and par['JE']['donnees']['profil'] == 'etudiant'
    assert par['JP']['donnees']['profil'] == 'enseignant'
    assert par['JE']['cle'] == '/app/secrets/firebase-gp-ipgei.json'


def test_en_parallele_jeton_perime_oublie_rien_ne_repart(envois):
    faits, fils = envois
    users = [_compte(f'e{i}x') for i in range(30)]
    for i, u in enumerate(users):
        AppareilPush.objects.create(user_id=u.pk, jeton=('PERIME' if i == 0 else 'J') + str(u.pk))
        Notification.objects.create(destinataire=u, titre='Emploi validé', message='m')
    call_command('envoyer_push')
    assert len(faits) == 29 and len(fils) > 1
    assert not AppareilPush.objects.filter(jeton__startswith='PERIME').exists()
    call_command('envoyer_push')
    assert len(faits) == 29


def test_une_notification_lue_ne_part_pas(envois):
    faits, _ = envois
    u = _compte('e2')
    AppareilPush.objects.create(user_id=u.pk, jeton='JL')
    Notification.objects.create(destinataire=u, titre='Lue', message='m', lue=True)
    call_command('envoyer_push')
    assert faits == []


def test_inscription_desinscription():
    u = _compte('e3')
    c = APIClient()
    c.force_authenticate(u)
    assert c.post('/api/v1/notifications/appareils/', {'jeton': 'JX', 'langue': 'ar'},
                  format='json').status_code == 201
    a = AppareilPush.objects.get(jeton='JX')
    assert (a.user_id, a.langue, a.projet) == (u.pk, 'ar', 'gp')
    assert c.delete('/api/v1/notifications/appareils/', {'jeton': 'JX'}, format='json').status_code == 204
    assert not AppareilPush.objects.exists()


def test_oublier_et_remplacer_sans_session():
    u = _compte('e4')
    AppareilPush.objects.create(user_id=u.pk, jeton='VIEUX')
    anonyme = APIClient()
    assert anonyme.post('/api/v1/notifications/appareils/remplacer/',
                        {'ancien': 'VIEUX', 'nouveau': 'NEUF'}, format='json').status_code == 204
    assert AppareilPush.objects.get(user_id=u.pk).jeton == 'NEUF'
    assert anonyme.post('/api/v1/notifications/appareils/oublier/', {'jeton': 'NEUF'},
                        format='json').status_code == 204
    assert not AppareilPush.objects.exists()
