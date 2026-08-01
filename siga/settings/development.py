from .base import *

DEBUG = True
ALLOWED_HOSTS = ['*']

# Show SQL queries in console
LOGGING['loggers']['django.db.backends'] = {
    'handlers': ['console'], 'level': 'DEBUG', 'propagate': False,
}

# Browsable API in dev
REST_FRAMEWORK['DEFAULT_RENDERER_CLASSES'] = [
    'rest_framework.renderers.JSONRenderer',
    'rest_framework.renderers.BrowsableAPIRenderer',
]
