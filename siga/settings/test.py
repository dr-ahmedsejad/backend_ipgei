"""
Settings dedies aux tests pytest.
- BD sqlite IN-MEMORY (jamais touchee la BD MySQL prod gesafped26)
- Migrations desactivees (--no-migrations en CI) pour vitesse
- Hashing rapide MD5 (10x plus rapide que bcrypt)
- Axes desactive (sinon login en boucle dans les tests)
"""
from .base import *  # noqa: F401,F403

# BD ISOLEE EN RAM — ne touche JAMAIS gesafped26
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME':   ':memory:',
    }
}

# Hashing rapide pour login dans les tests
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]

# Desactive Axes : sinon les tests qui appellent /login/ en boucle se font bloquer
AXES_ENABLED = False

# Pas de cache externe en test
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
}

# Logs minimaux pendant les tests
import logging
logging.disable(logging.CRITICAL)

DEBUG = False
SECRET_KEY = 'test-secret-key-do-not-use-in-prod'

# En mode --migrations (verification du schema vendor-neutre), les data
# migrations de seed RBAC (authentication 0004/0007/0008/0009) ne doivent PAS
# peupler la base : la suite de tests a ete ecrite sur une baseline VIDE
# (--no-migrations). Ce flag les court-circuite uniquement sous settings de
# test ; les deploiements reels (MySQL prod, futur PostgreSQL) les executent.
SEEDS_DESACTIVES_POUR_TESTS = True
