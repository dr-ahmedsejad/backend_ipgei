"""Custom exception handler DRF pour SIGA.

Objectifs sécurité :
1. JAMAIS retourner de stack trace, paths internes, noms de modèles ORM, ou détails BD
   à l'utilisateur final. Tout cela reste serveur-side (logs/Sentry).
2. Chaque erreur reçoit un `request_id` (UUID court) que l'utilisateur peut donner au
   support. Le support cherche ce request_id dans les logs serveur pour retrouver la
   stack trace complète.
3. Messages utilisateur traduits FR, courts, sans détails techniques.
4. Codes HTTP conservés (400/403/404/409/500…) pour compatibilité REST.

Cas spéciaux gérés :
- ProtectedError Django -> 409 "Impossible de supprimer : référencé par X"
- Throttled -> message FR avec délai
- ValidationError DRF (400) -> structure par champ préservée (sécurise car écrite par nous)
- Exceptions non gérées -> 500 avec message générique + request_id, stack trace logguée
"""
import math
import logging
import secrets

from django.db.models.deletion import ProtectedError
from rest_framework.views import exception_handler
from rest_framework.exceptions import Throttled
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger('siga')


def _get_or_create_request_id() -> str:
    """Recupere le request_id pose par AuditMiddleware (X-Request-Id uuid4).
    Si non disponible (cas rare : exception avant le middleware), genere un
    fallback court (8 hex)."""
    try:
        from core.audit_context import get_request_context
        rid = get_request_context().get('request_id', '')
        if rid:
            # Coupe l'uuid4 a 8 char pour la version "user-friendly" affichee
            return rid.split('-')[0] if '-' in rid else rid[:8]
    except Exception:
        pass
    return secrets.token_hex(4)


def _safe_user_message_for_5xx() -> str:
    """Message generique 5xx — JAMAIS de details techniques."""
    return 'Une erreur interne est survenue. Veuillez réessayer ou contacter le support.'


def custom_exception_handler(exc, context):
    """Handler central des exceptions DRF + Django.

    Renvoie systematiquement :
      {
        "status":     <code HTTP>,
        "error":      "<message FR>",
        "request_id": "<8 char hex>",      # toujours present pour traçabilité
        "errors":     { ... }              # uniquement pour 400 validation
      }
    """
    request_id = _get_or_create_request_id()
    view       = context.get('view')
    request    = context.get('request')
    user_id    = getattr(getattr(request, 'user', None), 'pk', None) if request else None

    # ── Cas special 1 : ProtectedError (suppression bloquee par FK) ───────────
    if isinstance(exc, ProtectedError):
        protected   = exc.protected_objects
        model_names = {obj.__class__.__name__ for obj in protected}
        # Pas de details internes - message generique
        logger.warning(
            'ProtectedError request_id=%s user=%s view=%s models=%s nb=%d',
            request_id, user_id, getattr(view, '__class__.__name__', '?'),
            ', '.join(model_names), len(protected),
        )
        return Response(
            {
                'status':     409,
                'error':      'Suppression impossible : cet enregistrement est référencé '
                              'par d\'autres données. Supprimez d\'abord les références.',
                'request_id': request_id,
            },
            status=status.HTTP_409_CONFLICT,
        )

    # ── Cas special 2 : Throttling (Axes / DRF rate limit) ───────────────────
    if isinstance(exc, Throttled):
        wait = exc.wait
        if wait is not None:
            minutes = math.ceil(wait / 60)
            if minutes >= 2:
                detail = f'Trop de tentatives. Réessayez dans {minutes} minutes.'
            else:
                seconds = math.ceil(wait)
                detail = f'Trop de tentatives. Réessayez dans {seconds} secondes.'
        else:
            detail = 'Trop de tentatives. Veuillez patienter avant de réessayer.'
        exc.detail = detail
        logger.info(
            'Throttled request_id=%s user=%s wait=%s',
            request_id, user_id, wait,
        )

    # ── Delegation au handler DRF standard ───────────────────────────────────
    response = exception_handler(exc, context)

    if response is not None:
        # Erreur "metier" connue de DRF (4xx structuree)
        code = response.status_code

        # Log de toute erreur 4xx (utile pour detecter abus, scan, brute force)
        logger.info(
            'API error %s request_id=%s user=%s view=%s',
            code, request_id, user_id,
            getattr(view, '__class__.__name__', '?') if view else '?',
        )

        if code == 400 and isinstance(response.data, dict):
            # Validation : preserver la structure par champ (les messages sont
            # ecrits par nous, donc surs - cf serializers.validate()).
            response.data = {
                'status':     400,
                'errors':     response.data,
                'request_id': request_id,
            }
        elif code >= 500:
            # 500 venant de DRF (rare, ex: NotImplementedError) - masquer
            logger.error(
                'API 5xx via DRF handler request_id=%s user=%s exc=%s',
                request_id, user_id, type(exc).__name__,
            )
            response.data = {
                'status':     code,
                'error':      _safe_user_message_for_5xx(),
                'request_id': request_id,
            }
        else:
            # 401, 403, 404, 405, 406, 409, 415, etc. - garder le message DRF
            # (ces messages sont generiques et OK pour l'utilisateur)
            msg = _flatten(response.data)
            response.data = {
                'status':     code,
                'error':      msg,
                'request_id': request_id,
            }
        return response

    # ── Exception non geree par DRF (= bug applicatif) -> 500 masque ─────────
    logger.exception(
        'Unhandled exception request_id=%s user=%s view=%s exc_type=%s',
        request_id, user_id,
        getattr(view, '__class__.__name__', '?') if view else '?',
        type(exc).__name__,
        exc_info=exc,
    )
    return Response(
        {
            'status':     500,
            'error':      _safe_user_message_for_5xx(),
            'request_id': request_id,
        },
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def _flatten(data):
    """Aplatit recursivement une structure DRF (dict/list) en une chaine.
    Utilise pour 401/403/404 où on a juste un message simple a renvoyer."""
    if isinstance(data, list):
        return ' '.join(str(i) for i in data)
    if isinstance(data, dict):
        msgs = []
        for v in data.values():
            msgs.append(_flatten(v))
        return ' '.join(msgs)
    return str(data)
