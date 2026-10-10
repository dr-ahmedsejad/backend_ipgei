"""Envoi des notifications push par Firebase Cloud Messaging (API HTTP v1).

Sans bibliothèque Google : le jeton d'accès OAuth2 s'obtient en signant un JWT
(RS256) avec la clé du compte de service — PyJWT et cryptography sont déjà là
(djangorestframework-simplejwt, pyHanko).

Configuration : `settings.FIREBASE_CREDENTIALS` = chemin du fichier JSON de la
clé (SECRET, hors git). Vide → `configure()` est faux et rien n'est envoyé.

L'app enseignant est un AUTRE projet Firebase : sa clé est dans
`settings.FIREBASE_CREDENTIALS_ENSEIGNANT`. Chaque fonction prend le chemin de
la clé à utiliser (`chemin`) ; sans lui, la clé de l'app étudiante.

Débit : `envoyer` réutilise UNE connexion HTTPS par fil (thread) vers FCM au
lieu d'en ouvrir une par message, et peut être appelé depuis plusieurs fils
(envoyer_push en lance 10). FCM répond « trop de requêtes » (429) ou
« indisponible » (5xx) : on attend et on réessaie (3 essais).
"""
import http.client
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import jwt
from django.conf import settings

logger = logging.getLogger(__name__)

_SCOPE = 'https://www.googleapis.com/auth/firebase.messaging'
_jetons_cache = {}  # chemin de la clé → {'valeur', 'expire'}
_verrou_jeton = threading.Lock()
_cles_cache = {}    # chemin → contenu de la clé (lue une fois par exécution)
_fil = threading.local()  # connexion HTTPS vers FCM, une par fil
_FCM = 'fcm.googleapis.com'
_ESSAIS = 3


class JetonInvalide(Exception):
    """Le jeton de l'appareil n'est plus valable (app désinstallée…) : à supprimer."""


def cle_etudiant():
    return getattr(settings, 'FIREBASE_CREDENTIALS', '')


def cle_enseignant():
    return getattr(settings, 'FIREBASE_CREDENTIALS_ENSEIGNANT', '')


def cle_gp():
    """App Groupe Polytechnique : la clé de cet établissement dans le projet commun."""
    return getattr(settings, 'FIREBASE_CREDENTIALS_GP', '')


def _cle(chemin=None):
    with open(chemin or cle_etudiant(), encoding='utf-8') as f:
        return json.load(f)


def _cle_gardee(chemin=None):
    chemin = chemin or cle_etudiant()
    if chemin not in _cles_cache:
        _cles_cache[chemin] = _cle(chemin)
    return _cles_cache[chemin]


def configure(chemin=None):
    chemin = chemin or cle_etudiant()
    if not chemin:
        return False
    try:
        cle = _cle(chemin)
        return bool(cle.get('project_id') and cle.get('private_key') and cle.get('client_email'))
    except (OSError, ValueError):
        logger.warning('FIREBASE_CREDENTIALS illisible : %s', chemin)
        return False


def _post(url, corps, entetes):
    req = urllib.request.Request(url, data=corps, headers=entetes, method='POST')
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode('utf-8') or '{}')


def _jeton_acces(chemin=None):
    """Jeton OAuth2 (1 h), mis en cache jusqu'à 5 min avant son expiration.
    Un seul fil le demande à la fois (les autres attendent et le réutilisent)."""
    chemin = chemin or cle_etudiant()
    with _verrou_jeton:
        return _jeton_acces_verrouille(chemin)


def _jeton_acces_verrouille(chemin):
    _jeton_cache = _jetons_cache.setdefault(chemin, {'valeur': None, 'expire': 0.0})
    if _jeton_cache['valeur'] and time.time() < _jeton_cache['expire'] - 300:
        return _jeton_cache['valeur']
    cle = _cle(chemin)
    maintenant = int(time.time())
    assertion = jwt.encode({
        'iss': cle['client_email'],
        'scope': _SCOPE,
        'aud': cle.get('token_uri', 'https://oauth2.googleapis.com/token'),
        'iat': maintenant,
        'exp': maintenant + 3600,
    }, cle['private_key'], algorithm='RS256')
    corps = urllib.parse.urlencode({
        'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        'assertion': assertion,
    }).encode()
    rep = _post(cle.get('token_uri', 'https://oauth2.googleapis.com/token'), corps,
                {'Content-Type': 'application/x-www-form-urlencoded'})
    _jeton_cache['valeur'] = rep['access_token']
    _jeton_cache['expire'] = maintenant + int(rep.get('expires_in', 3600))
    return _jeton_cache['valeur']


def envoyer(jeton_appareil, titre, corps, donnees=None, chemin=None):
    """Envoie une notification à UN appareil. Lève JetonInvalide si FCM répond
    que le jeton n'existe plus (404 / UNREGISTERED) ; les autres erreurs sont
    remontées telles quelles (réseau, quota…)."""
    projet = _cle_gardee(chemin)['project_id']
    message = {
        'message': {
            'token': jeton_appareil,
            'notification': {'title': titre[:200], 'body': corps[:1000]},
            # Valeurs en texte uniquement (exigence FCM) ; lues par l'app au toucher.
            'data': {k: str(v) for k, v in (donnees or {}).items()},
            'android': {'priority': 'high', 'notification': {'channel_id': 'siga_general'}},
        }
    }
    corps_json = json.dumps(message).encode('utf-8')
    chemin_url = f'/v1/projects/{projet}/messages:send'
    for essai in range(1, _ESSAIS + 1):
        code, detail, attente = _post_fcm(chemin_url, corps_json, {
            'Authorization': f'Bearer {_jeton_acces(chemin)}', 'Content-Type': 'application/json'})
        if code == 200:
            return json.loads(detail or '{}')
        if code == 404 or 'UNREGISTERED' in detail or ('INVALID_ARGUMENT' in detail and 'token' in detail):
            raise JetonInvalide(detail)
        if (code == 429 or code >= 500) and essai < _ESSAIS:
            time.sleep(attente if attente is not None else essai)   # 1 s, puis 2 s
            continue
        raise RuntimeError(f'FCM {code} : {detail[:300]}')


def _post_fcm(chemin_url, corps, entetes):
    """POST vers FCM sur la connexion de ce fil (rouverte si FCM l'a fermée).
    Retourne (code, corps, attente conseillée en s ou None)."""
    for tentative in range(2):
        conn = getattr(_fil, 'conn', None)
        if conn is None:
            conn = _fil.conn = http.client.HTTPSConnection(_FCM, timeout=15)
        try:
            conn.request('POST', chemin_url, body=corps, headers=entetes)
            r = conn.getresponse()
            texte = r.read().decode('utf-8', 'replace')
            attente = r.getheader('Retry-After')
            return r.status, texte, (float(attente) if attente and attente.isdigit() else None)
        except (http.client.HTTPException, OSError):
            # Connexion périmée (FCM ferme les connexions inactives) : une neuve.
            conn.close()
            _fil.conn = None
            if tentative:
                raise
    raise RuntimeError('FCM injoignable')

