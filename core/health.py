"""
Sonde de santé — `GET /healthz/`.

Un gunicorn figé ne meurt pas : `restart: always` ne le relance donc jamais,
et la panne se découvrait par téléphone. Cette route donne à un superviseur
(Uptime Kuma, healthchecks.io, la sonde Docker) quelque chose à interroger :
200 si l'application répond ET que la base répond, 503 sinon.

Volontairement pauvre : ni version, ni compteurs, ni noms de tables — c'est
une route publique, elle ne doit rien apprendre à qui la lit.
"""
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@never_cache
@require_GET
def healthz(request):
    try:
        with connection.cursor() as curseur:
            curseur.execute('SELECT 1')
            curseur.fetchone()
    except Exception:                                       # noqa: BLE001
        return JsonResponse({'status': 'degraded', 'database': 'down'}, status=503)
    return JsonResponse({'status': 'ok'})
