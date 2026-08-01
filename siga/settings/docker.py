"""
SIGA — Settings pour deploiement Docker
========================================
Hérite de base.py mais sans imposer HTTPS (la terminaison TLS sera faite par
Nginx/Caddy en amont). DEBUG, ALLOWED_HOSTS et CORS sont configurables via
variables d'environnement passees par docker-compose.

Pour activer la securite stricte HTTPS quand Nginx aura un certificat SSL :
mettre DJANGO_SSL_REDIRECT=True dans le .env du compose.
"""
from .base import *  # noqa: F401, F403
from decouple import config, Csv

DEBUG = config('DEBUG', default=False, cast=bool)

ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='*', cast=Csv())

# Derriere Nginx : Django doit faire confiance au header X-Forwarded-Proto
# pour savoir si la connexion d'origine etait HTTPS.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Securite HTTPS — desactivee par defaut (HTTP local), activable par env var.
SECURE_SSL_REDIRECT = config('DJANGO_SSL_REDIRECT', default=False, cast=bool)
SESSION_COOKIE_SECURE = config('DJANGO_COOKIE_SECURE', default=False, cast=bool)
CSRF_COOKIE_SECURE = config('DJANGO_COOKIE_SECURE', default=False, cast=bool)

# JWT cookie : non-secure tant qu'on est en HTTP.
SIMPLE_JWT['AUTH_COOKIE_SECURE'] = config('DJANGO_COOKIE_SECURE', default=False, cast=bool)
SIMPLE_JWT['AUTH_COOKIE_SAMESITE'] = 'Lax'

# HSTS : uniquement si SSL active.
if SECURE_SSL_REDIRECT:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS             = 'DENY'         # anti-clickjacking
SECURE_REFERRER_POLICY      = 'same-origin'  # ne fuite pas l'URL au site externe

# Cookies extra (en plus de SESSION/CSRF_COOKIE_SECURE deja gatees par DJANGO_COOKIE_SECURE)
SESSION_COOKIE_HTTPONLY     = True
CSRF_COOKIE_HTTPONLY        = True

# DRF prod : seul JSONRenderer (pas de BrowsableAPIRenderer qui expose la page bleue)
# Override defensif au cas ou un autre fichier ajouterait BrowsableAPI :
REST_FRAMEWORK['DEFAULT_RENDERER_CLASSES'] = [
    'rest_framework.renderers.JSONRenderer',
]

# CORS — origines autorisees passees via env var (CORS_ALLOWED_ORIGINS dans le compose).
# La conf de base.py utilise deja decouple, on n'a rien a redefinir ici.
