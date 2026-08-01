"""
AuditMiddleware — capture le contexte HTTP de chaque requete dans un
thread-local accessible par les signals d'audit.

Stocke :
  - request.user (None si anonyme)
  - request.META['REMOTE_ADDR'] / X-Forwarded-For
  - request.META['HTTP_USER_AGENT']
  - request.path + request.method
  - request_id (uuid4) — corrélation requête/audit logs
"""
import uuid
from core.audit_context import set_request_context, clear_request_context


class AuditMiddleware:
    """A placer apres AuthenticationMiddleware dans MIDDLEWARE."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = str(uuid.uuid4())
        # X-Forwarded-For pour reverse-proxy, fallback REMOTE_ADDR
        ip = (
            request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
            or request.META.get('REMOTE_ADDR', '')
        )
        user = getattr(request, 'user', None)
        if user is not None and not user.is_authenticated:
            user = None

        set_request_context(
            user=user,
            ip_address=ip or None,
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:1000],
            request_id=request_id,
            endpoint=request.path[:200],
            http_method=request.method,
        )
        try:
            response = self.get_response(request)
        finally:
            clear_request_context()

        # Permet a la reponse de retourner le request_id pour debugging
        response['X-Request-Id'] = request_id
        return response
