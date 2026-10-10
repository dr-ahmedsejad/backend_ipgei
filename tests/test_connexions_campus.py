"""
Connexions depuis le campus : des centaines d'étudiants partagent UNE adresse
publique. Mesuré le 10/10/2026 avant correction :
- connexion ET renouvellement de session limités à 5 par quart d'heure et par
  adresse : dès le 6e étudiant, tous les suivants étaient refusés ;
- verrou axes par adresse seule : 5 fautes de frappe d'étudiants différents
  bloquaient tout le campus un quart d'heure.
Désormais : limites et verrou PAR COMPTE (règle du SIGA privé, CDD).
"""
import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

LOGIN = '/api/v1/auth/login/'
REFRESH = '/api/v1/auth/token/refresh/'
CAMPUS = '41.188.10.20'
PROXY = '172.18.0.5'   # le conteneur nginx, vu du backend


@pytest.fixture(autouse=True)
def monde(settings, db):
    settings.AXES_ENABLED = True
    settings.AXES_FAILURE_LIMIT = 5
    cache.clear()
    from apps.parametres.models import Year
    Year.objects.get_or_create(annee='2025-2026', defaults={'est_active': True})
    yield
    cache.clear()


def campus():
    return APIClient(REMOTE_ADDR=PROXY, HTTP_X_REAL_IP=CAMPUS, HTTP_X_FORWARDED_FOR=CAMPUS)


def compte(i):
    from apps.authentication.models import CustomUser
    return CustomUser.objects.create_user(username=f'etu{i}', email=f'etu{i}@ipgei.mr',
                                          password=f'Bon-Mdp-{i}', role='admin')


def test_dix_connexions_reussies_depuis_le_campus():
    statuts = []
    for i in range(10):
        compte(i)
        statuts.append(campus().post(LOGIN, {'username': f'etu{i}', 'password': f'Bon-Mdp-{i}'},
                                     format='json').status_code)
    assert 429 not in statuts, statuts


def test_fautes_de_frappe_ne_bloquent_pas_le_campus():
    for i in range(6):
        compte(i)
    # 5 étudiants différents se trompent chacun une fois…
    for i in range(5):
        assert campus().post(LOGIN, {'username': f'etu{i}', 'password': 'faux'},
                             format='json').status_code != 429
    # … le 6e se connecte quand même.
    r = campus().post(LOGIN, {'username': 'etu5', 'password': 'Bon-Mdp-5'}, format='json')
    assert r.status_code == 200, r.data


def test_un_compte_attaque_est_bloque_les_autres_non():
    compte(1)
    compte(2)
    codes = [campus().post(LOGIN, {'username': 'etu1', 'password': 'faux'}, format='json').status_code
             for _ in range(7)]
    assert 429 in codes                              # etu1 verrouillé
    r = campus().post(LOGIN, {'username': 'etu2', 'password': 'Bon-Mdp-2'}, format='json')
    assert r.status_code == 200, r.data              # etu2, même adresse : libre


def test_renouvellements_du_campus_non_limites():
    from rest_framework_simplejwt.tokens import RefreshToken
    statuts = []
    for i in range(10):
        c = campus()
        c.cookies['refresh_token'] = str(RefreshToken.for_user(compte(i)))
        statuts.append(c.post(REFRESH).status_code)
    assert 429 not in statuts, statuts
