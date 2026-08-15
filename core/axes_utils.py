"""
Callback axes pour retourner une réponse JSON 429 au lieu d'une page HTML.
Configuré via AXES_LOCKOUT_CALLABLE dans settings.
"""
import json
from django.http import HttpResponse


def axes_lockout_callback(request, credentials=None, *args, **kwargs):
    """
    Appelé par AxesMiddleware quand une IP est bloquée.
    Retourne du JSON 429 pour que le frontend puisse afficher le bon message.
    """
    data = json.dumps({
        'error': 'Trop de tentatives échouées. Accès bloqué pendant 15 minutes.',
        'blocked': True,
    }, ensure_ascii=False)
    return HttpResponse(data, content_type='application/json', status=429)


def axes_client_ip(request):
    """
    IP RÉELLE du client derrière Nginx — branchée par `AXES_CLIENT_IP_CALLABLE`.

    `django-ipware` n'est pas installé : sans ce callable, axes retombe sur
    `REMOTE_ADDR`, qui derrière un reverse proxy vaut l'IP du conteneur Nginx —
    la MÊME pour tout le monde. Cinq échecs d'un étudiant verrouillaient alors
    l'établissement entier, administrateur compris, lequel ne pouvait plus se
    connecter pour débloquer quoi que ce soit.

    Nginx pose `X-Real-IP` ; on la lit d'abord, puis la première adresse de
    `X-Forwarded-For`, puis `REMOTE_ADDR` en dernier recours. Ces en-têtes ne
    sont dignes de foi que parce que le backend n'est joignable QUE par Nginx :
    exposé en direct, ils seraient falsifiables et cette fonction deviendrait
    une faille.
    """
    reelle = request.META.get('HTTP_X_REAL_IP')
    if reelle:
        return reelle.strip()
    transmise = request.META.get('HTTP_X_FORWARDED_FOR')
    if transmise:
        return transmise.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')
