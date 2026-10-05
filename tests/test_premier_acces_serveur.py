"""Premier accès vérifié par le serveur (core/authentication.py).

Un étudiant qui n'a pas encore choisi son mot de passe ne reçoit que ce qui
sert à le faire ; tout le reste répond 403 `premier_acces`.
"""
import pytest
from django.conf import settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from tests.factories.auth import AdminUserFactory, EtudiantUserFactory

pytestmark = pytest.mark.django_db


def _client(user):
    c = APIClient()
    c.cookies[settings.SIMPLE_JWT.get('AUTH_COOKIE', 'access_token')] = str(AccessToken.for_user(user))
    return c


def test_etudiant_au_premier_acces_bloque_hors_parcours():
    u = EtudiantUserFactory(username='etu_premier', doit_changer_mdp=True)
    c = _client(u)
    r = c.get('/api/v1/portail/notes/')
    assert r.status_code == 403
    assert 'premier accès' in str(r.data)


def test_parcours_du_premier_acces_autorise():
    u = EtudiantUserFactory(username='etu_parcours', doit_changer_mdp=True)
    c = _client(u)
    assert c.get('/api/v1/auth/me/').status_code == 200
    # Profil en lecture : autorisé (404 ici : pas de fiche étudiant rattachée).
    assert c.get('/api/v1/portail/profil/').status_code != 403
    # Profil en écriture : refusé.
    assert c.patch('/api/v1/portail/profil/', {'telephone': '1'}).status_code == 403


def test_premier_acces_fait_plus_de_blocage():
    u = EtudiantUserFactory(username='etu_fait', doit_changer_mdp=False)
    assert _client(u).get('/api/v1/portail/notes/').status_code != 403


def test_personnel_jamais_concerne():
    u = AdminUserFactory(username='admin_premier', doit_changer_mdp=True)
    assert _client(u).get('/api/v1/auth/me/').status_code == 200
    # Le portail lui est fermé par son rôle, jamais par le premier accès.
    r = _client(u).get('/api/v1/portail/notes/')
    assert 'premier accès' not in str(r.data)
