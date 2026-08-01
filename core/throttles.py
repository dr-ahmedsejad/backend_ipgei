from rest_framework.throttling import UserRateThrottle, AnonRateThrottle


class LoginRateThrottle(AnonRateThrottle):
    """
    Couche 1 — DRF : 5 tentatives de login par IP toutes les 15 minutes.
    Bloque avant même d'interroger la base de données.
    parse_rate surchargé car DRF ne supporte pas les fenêtres multi-minutes.
    """
    scope = 'login'

    def parse_rate(self, rate):
        # Fenêtre fixe : 5 requêtes par 900 secondes (15 min), indépendant du settings
        return (5, 900)


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
