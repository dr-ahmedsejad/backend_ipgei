from rest_framework.throttling import UserRateThrottle, AnonRateThrottle

from core.ip_client import adresse_client


class LoginRateThrottle(AnonRateThrottle):
    """
    5 requêtes par 15 minutes : renouvellement de jeton et premier accès. La
    CONNEXION n'en a plus — elle compte ses échecs, pas ses requêtes
    (apps/authentication/tentatives.py).
    parse_rate surchargé car DRF ne supporte pas les fenêtres multi-minutes.

    Limite par IDENTITÉ tentée (username), PAS par IP : derrière le Wi-Fi d'un
    campus, des centaines d'étudiants partagent UNE adresse publique — par IP,
    dès le 6e étudiant connecté dans le quart d'heure, tous les suivants étaient
    refusés (connexion ET renouvellement de session). Repli sur le jeton de
    renouvellement (pour /token/refresh, sans username) puis l'IP en dernier
    recours. Même règle que le SIGA privé (CDD).
    """
    scope = 'login'

    def parse_rate(self, rate):
        # Fenêtre fixe : 5 requêtes par 900 secondes (15 min), indépendant du settings
        return (5, 900)

    def get_cache_key(self, request, view):
        ident = ''
        try:
            if request.method == 'POST':
                ident = (request.data.get('username') or '').strip().lower()
        except Exception:
            ident = ''
        if not ident:
            ident = request.COOKIES.get('refresh_token') or self.get_ident(request)
        return self.cache_format % {'scope': self.scope, 'ident': (ident or '')[:150]}

    def get_ident(self, request):
        # DRF lirait X-Forwarded-For tel quel : le client y écrit ce qu'il veut.
        return adresse_client(request) or super().get_ident(request)


class SensitiveEndpointThrottle(UserRateThrottle):
    """
    5 tentatives par heure pour les endpoints authentifiés sensibles.
    Ex : changement de mot de passe — limite les attaques par force brute
    sur l'ancien mot de passe.
    Configurer dans settings : DEFAULT_THROTTLE_RATES['sensitive_endpoint']
    """
    scope = 'sensitive_endpoint'


class AdminActionThrottle(UserRateThrottle):
    """
    60 actions par minute pour les actions admin (toggle permissions).
    Empêche le flood des endpoints RBAC qui écrivent en base à chaque appel.
    Configurer dans settings : DEFAULT_THROTTLE_RATES['admin_action']
    """
    scope = 'admin_action'
