"""
Settings de tests pytest sur PostgreSQL (migration PG — avant cutover).

Hérite de test.py (MD5 hasher, Axes off, LocMemCache, logs coupés...) et ne
redéfinit QUE la base de données : PostgreSQL local au lieu de sqlite :memory:.

Usage :
    pytest tests/ --ds=siga.settings.test_pg --migrations --create-db

Les valeurs par défaut sont celles du poste de dev local (PostgreSQL 18) ;
surchargez via variables d'environnement PG_TEST_DB / PG_TEST_USER /
PG_TEST_PASSWORD / PG_TEST_HOST / PG_TEST_PORT si besoin.
pytest-django créera/écrasera la base « test_<PG_TEST_DB> ».
"""
from decouple import config

from .test import *  # noqa: F401,F403

# PostgreSQL local — ne touche JAMAIS gesafped26 (MySQL).
# Pas d'OPTIONS.init_command : c'était un réglage MySQL (sql_mode).
DATABASES = {
    'default': {
        'ENGINE':   'django.db.backends.postgresql',
        'NAME':     config('PG_TEST_DB',       default='IPGEI_test'),
        'USER':     config('PG_TEST_USER',     default='postgres'),
        'PASSWORD': config('PG_TEST_PASSWORD', default=''),
        'HOST':     config('PG_TEST_HOST',     default='localhost'),
        'PORT':     config('PG_TEST_PORT',     default='5432'),
    }
}
