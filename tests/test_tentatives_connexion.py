"""
Tentatives de connexion, axes ACTIVÉ (il est coupé dans le reste des tests).

Mesuré le 04/10/2026, avant correction :
- au 5e échec, le blocage répondait « Aucun compte actif… » : rien n'indiquait
  qu'on était bloqué, et chaque nouvel essai relançait les 15 minutes ;
- une connexion réussie ne remettait pas le compteur à zéro ;
- la 6e connexion CORRECTE en 15 minutes depuis une adresse était refusée ;
- derrière nginx, tout le monde partageait l'adresse du conteneur : cinq
  erreurs n'importe où bloquaient tout l'établissement ;
- changer X-Forwarded-For à chaque essai contournait la limite.
"""
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

URL = '/api/v1/auth/login/'
PROXY = '172.18.0.5'          # le conteneur nginx, vu du backend


@pytest.fixture(autouse=True)
def monde(settings, db):
    settings.AXES_ENABLED = True
    settings.AXES_FAILURE_LIMIT = 5
    settings.LOGIN_ECHECS_PAR_IP = 20
    cache.clear()
    from apps.parametres.models import Year
    from apps.authentication.models import CustomUser
    Year.objects.create(annee='2025-2026', est_active=True)
    CustomUser.objects.create_user(username='alice', email='alice@iss.mr', password='Bon-Mdp-123', role='admin')
    CustomUser.objects.create_user(username='bob', email='bob@iss.mr', password='Bon-Mdp-456', role='admin')
    yield
    cache.clear()


MDP = {'alice': 'Bon-Mdp-123', 'bob': 'Bon-Mdp-456'}


def essai(user, bon, client='41.0.0.1', **entetes):
    """Une tentative passée par nginx : REMOTE_ADDR = le proxy, X-Real-IP = le client."""
    meta = {'REMOTE_ADDR': PROXY, 'HTTP_X_REAL_IP': client}
    meta.update(entetes)
    return APIClient().post(URL, {'username': user, 'password': MDP.get(user, 'x') if bon else 'faux'},
                            format='json', **meta)


def codes(user, bon, n, **kw):
    return [essai(user, bon, **kw).status_code for _ in range(n)]


def echecs(**filtre):
    from axes.models import AccessAttempt
    return sum(a.failures_since_start for a in AccessAttempt.objects.filter(**filtre))


# ── Le compte ────────────────────────────────────────────────────────────────

class TestCompte:

    def test_le_cinquieme_echec_bloque_et_le_dit(self):
        assert codes('alice', False, 4) == [401] * 4
        r = essai('alice', False)
        assert r.status_code == 429
        assert 'pour ce compte' in r.json()['error']
        # La page de connexion lit les minutes pour son compte à rebours.
        assert '15 minutes' in r.json()['error']

    def test_bloque_meme_avec_le_bon_mot_de_passe(self):
        codes('alice', False, 5)
        r = essai('alice', True)
        assert r.status_code == 429 and 'bloqué' in r.json()['error']

    def test_quatre_echecs_puis_le_bon_passe_et_remet_a_zero(self):
        codes('alice', False, 4)
        assert essai('alice', True).status_code == 200
        assert echecs(username='alice') == 0

    def test_les_fautes_ne_s_additionnent_plus_d_une_connexion_a_l_autre(self):
        codes('alice', False, 4)
        assert essai('alice', True).status_code == 200
        assert codes('alice', False, 4) == [401] * 4
        assert essai('alice', True).status_code == 200

    def test_reessayer_pendant_le_blocage_ne_le_prolonge_pas(self):
        codes('alice', False, 5)
        from axes.models import AccessAttempt
        avant = AccessAttempt.objects.get(username='alice').attempt_time
        codes('alice', False, 3)
        codes('alice', True, 2)
        assert echecs(username='alice') == 5
        assert AccessAttempt.objects.get(username='alice').attempt_time == avant

    def test_le_blocage_tombe_apres_quinze_minutes(self):
        codes('alice', False, 5)
        from axes.models import AccessAttempt
        AccessAttempt.objects.update(attempt_time=timezone.now() - timedelta(minutes=16))
        assert essai('alice', True).status_code == 200

    def test_le_temps_restant_est_celui_qui_reste(self):
        codes('alice', False, 5)
        from axes.models import AccessAttempt
        AccessAttempt.objects.update(attempt_time=timezone.now() - timedelta(minutes=11))
        assert '4 minutes' in essai('alice', True).json()['error']


# ── Ceux qui partagent l'adresse ─────────────────────────────────────────────

class TestMemeAdresse:

    def test_six_connexions_correctes_passent(self):
        assert codes('alice', True, 6) == [200] * 6

    def test_un_camarade_n_est_pas_bloque(self):
        codes('alice', False, 5)
        assert essai('bob', True).status_code == 200

    def test_le_compte_reste_ouvert_depuis_une_autre_adresse(self):
        codes('alice', False, 5)
        assert essai('alice', True, client='41.0.0.2').status_code == 200


# ── L'adresse réelle derrière nginx ──────────────────────────────────────────

class TestAdresseReelle:

    def test_deux_clients_derriere_le_meme_proxy_sont_distincts(self):
        codes('alice', False, 5, client='41.0.0.1')
        assert echecs(ip_address=PROXY) == 0
        assert echecs(ip_address='41.0.0.1') == 5
        assert essai('bob', True, client='41.0.0.9').status_code == 200

    def test_x_forwarded_for_ne_change_rien(self):
        res = [essai('alice', False, HTTP_X_FORWARDED_FOR='9.9.9.%d' % i).status_code
               for i in range(5)]
        assert res == [401] * 4 + [429]

    def test_x_real_ip_ignore_hors_proxy(self):
        """Venue directement d'Internet, une requête garde son adresse, quel
        que soit l'en-tête qu'elle envoie."""
        res = [essai('alice', False, REMOTE_ADDR='8.8.8.8', client='41.0.0.%d' % i).status_code
               for i in range(5)]
        assert res == [401] * 4 + [429]
        assert echecs(ip_address='8.8.8.8') == 5


class TestAdresseClientUnitaire:

    @pytest.mark.parametrize('remote, real, attendu', [
        ('172.18.0.5', '41.0.0.1', '41.0.0.1'),     # derrière nginx (docker)
        ('127.0.0.1', '41.0.0.1', '41.0.0.1'),      # proxy local
        ('172.18.0.5', '', '172.18.0.5'),           # pas d'en-tête
        ('172.18.0.5', 'pas-une-ip', '172.18.0.5'), # en-tête illisible
        ('8.8.8.8', '41.0.0.1', '8.8.8.8'),         # direct depuis Internet
    ])
    def test_adresse(self, remote, real, attendu):
        from django.test import RequestFactory
        from core.ip_client import adresse_client
        req = RequestFactory().get('/', REMOTE_ADDR=remote, HTTP_X_REAL_IP=real)
        assert adresse_client(req) == attendu


# ── L'adresse entière : un essai par compte, sur beaucoup de comptes ─────────

class TestAdresse:

    def test_vingt_echecs_tous_comptes_confondus_bloquent_l_adresse(self):
        res = [essai('inconnu%02d' % i, False).status_code for i in range(20)]
        assert res == [401] * 19 + [429]
        r = essai('bob', True)
        assert r.status_code == 429 and 'depuis cette connexion' in r.json()['error']

    def test_une_autre_adresse_n_est_pas_touchee(self):
        for i in range(20):
            essai('inconnu%02d' % i, False)
        assert essai('bob', True, client='41.0.0.2').status_code == 200

    def test_les_fautes_suivies_d_une_reussite_ne_pesent_plus(self):
        """Une salle : chacun se trompe, puis réussit. L'adresse reste ouverte."""
        from apps.authentication.models import CustomUser
        for i in range(25):
            CustomUser.objects.create_user(username='etu%02d' % i, email='etu%02d@iss.mr' % i,
                                           password='Mdp-etu-%02d' % i, role='admin')
            MDP['etu%02d' % i] = 'Mdp-etu-%02d' % i
        for i in range(25):
            assert essai('etu%02d' % i, False).status_code == 401
            assert essai('etu%02d' % i, True).status_code == 200


# ── L'écran de déblocage ─────────────────────────────────────────────────────

class TestDeblocage:

    @pytest.fixture
    def admin(self):
        from apps.authentication.models import CustomUser
        c = APIClient()
        c.force_authenticate(CustomUser.objects.create_user(
            username='adm', email='adm@iss.mr', password='x', role='admin', is_superuser=True))
        return c

    def test_le_compte_bloque_apparait_tous_navigateurs_confondus(self, admin):
        codes('alice', False, 3, HTTP_USER_AGENT='Chrome')
        codes('alice', False, 2, HTTP_USER_AGENT='Mobile')
        assert essai('alice', True).status_code == 429
        lignes = admin.get('/api/v1/auth/locked-attempts/').json()
        assert [(l['username'], l['ip_address'], l['failures']) for l in lignes] == \
            [('alice', '41.0.0.1', 5)]
        assert 0 < lignes[0]['remaining_seconds'] <= 900

    def test_l_adresse_bloquee_apparait_sans_compte(self, admin):
        for i in range(20):
            essai('inconnu%02d' % i, False)
        lignes = admin.get('/api/v1/auth/locked-attempts/').json()
        assert [(l['username'], l['ip_address'], l['failures']) for l in lignes] == \
            [(None, '41.0.0.1', 20)]

    def test_debloquer_le_compte_rouvre_l_acces(self, admin):
        codes('alice', False, 5)
        assert admin.post('/api/v1/auth/users/unblock/', {'username': 'alice'},
                          format='json').status_code == 200
        assert essai('alice', True).status_code == 200

    def test_debloquer_l_adresse_rouvre_l_acces(self, admin):
        for i in range(20):
            essai('inconnu%02d' % i, False)
        assert admin.post('/api/v1/auth/users/unblock/', {'ip': '41.0.0.1'},
                          format='json').status_code == 200
        assert essai('bob', True).status_code == 200
