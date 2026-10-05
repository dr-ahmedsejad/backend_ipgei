"""
Authentification JWT via cookie HttpOnly.
Lit le token dans le cookie `access_token` OU dans le header Authorization.
"""
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken
from django.conf import settings


def _sync_audit_user(user):
    """Met a jour le user dans le contexte d'audit thread-local.

    Necessaire car AuditMiddleware tourne AVANT DRF auth dans la chaine, donc
    capture request.user = AnonymousUser au demarrage de la requete. Sans cette
    synchronisation, tous les audits ecrits via signals (post_save etc.)
    avaient user_id=NULL dans core_audit_log.
    """
    try:
        from core.audit_context import _local
        _local.user = user
    except Exception:
        pass


# Premier accès : un étudiant qui n'a pas encore choisi son mot de passe
# personnel ne peut appeler que ce qui sert à le faire. L'app mobile et le
# portail web l'imposent à l'écran ; le serveur l'impose aussi, pour qu'un
# client qui passerait outre n'accède à rien d'autre.
_PREMIER_ACCES_AUTORISE = (
    '/api/v1/auth/',                     # moi, premier accès, mot de passe, déconnexion, refresh
    '/api/v1/notifications/appareils/',  # enregistrement du téléphone (notifications push)
)
_PREMIER_ACCES_LECTURE = (
    '/api/v1/portail/profil/',           # nom de l'écran de premier accès
)


def _verifier_premier_acces(request, user):
    """403 `premier_acces` si un étudiant au premier accès sort du parcours."""
    if getattr(user, 'role', None) != 'etudiant':
        return
    chemin = request.path
    if chemin.startswith(_PREMIER_ACCES_AUTORISE):
        return
    if chemin.startswith(_PREMIER_ACCES_LECTURE) and request.method in ('GET', 'HEAD', 'OPTIONS'):
        return
    # L'IPGEI n'a pas de mode miroir : l'indicateur du compte fait foi.
    if getattr(user, 'doit_changer_mdp', False):
        raise PermissionDenied(
            detail="Choisissez d'abord votre mot de passe personnel (premier accès).",
            code='premier_acces',
        )


class CookieJWTAuthentication(JWTAuthentication):

    def authenticate(self, request):
        # 1. Essaie le cookie HttpOnly
        raw_token = request.COOKIES.get(settings.SIMPLE_JWT.get('AUTH_COOKIE', 'access_token'))
        if raw_token:
            try:
                validated = self.get_validated_token(raw_token)
                user = self.get_user(validated)
                _sync_audit_user(user)
                _verifier_premier_acces(request, user)
                return user, validated
            except (InvalidToken, AuthenticationFailed):
                # Cookie stale (token invalide OU user_id supprime) → on traite la
                # requete comme anonyme. Sinon, /auth/login/ (AllowAny) est bloque
                # par 401 user_not_found avant meme d'atteindre la vue.
                pass

        # 2. Fallback : header Authorization: Bearer <token>
        try:
            result = super().authenticate(request)
            if result is not None:
                _sync_audit_user(result[0])
                _verifier_premier_acces(request, result[0])
            return result
        except AuthenticationFailed:
            return None
