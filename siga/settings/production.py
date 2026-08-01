from .base import *
from decouple import config

DEBUG = False
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='').split(',')

SIMPLE_JWT['AUTH_COOKIE_SECURE'] = True
SIMPLE_JWT['AUTH_COOKIE_SAMESITE'] = 'Strict'

# ── HTTPS / HSTS ──────────────────────────────────────────────────────────────
SECURE_SSL_REDIRECT             = True
SESSION_COOKIE_SECURE           = True
CSRF_COOKIE_SECURE              = True
SECURE_HSTS_SECONDS             = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS  = True
SECURE_HSTS_PRELOAD             = True

# ── Anti-sniff / Anti-clickjacking / Referrer ─────────────────────────────────
SECURE_CONTENT_TYPE_NOSNIFF     = True
X_FRAME_OPTIONS                 = 'DENY'         # interdit l'iframe (clickjacking)
SECURE_REFERRER_POLICY          = 'same-origin'  # ne fuite pas l'URL au site externe

# ── Cookies extra ─────────────────────────────────────────────────────────────
SESSION_COOKIE_HTTPONLY         = True
CSRF_COOKIE_HTTPONLY            = True

# ── DRF en prod : seul JSONRenderer (pas de BrowsableAPI qui expose le schema)
# Hereditage de base.py : DEFAULT_RENDERER_CLASSES = [JSONRenderer] -> deja OK.
# Override defensif au cas ou un autre fichier de settings ajouterait BrowsableAPI :
REST_FRAMEWORK['DEFAULT_RENDERER_CLASSES'] = [
    'rest_framework.renderers.JSONRenderer',
]
