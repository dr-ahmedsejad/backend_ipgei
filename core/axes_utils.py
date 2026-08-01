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
