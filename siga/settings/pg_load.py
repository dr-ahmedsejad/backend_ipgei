"""
Settings pour l'ETL de migration MySQL -> PostgreSQL (dry-run, hors cutover).

Expose DEUX connexions simultanées :
  - 'default'      = PostgreSQL cible  (base jetable, ex. siga_pg)
  - 'mysql_source' = MySQL source      (snapshot, LECTURE SEULE — jamais écrit)

Hérite de base.py (TIME_ZONE=Africa/Nouakchott, USE_TZ=True, toutes les apps).
Utilisé par `manage.py migrate` (crée le schéma Django sur la cible PG) puis par
`manage.py migrate_data_from_mysql` (copie les données).

    DJANGO_SETTINGS_MODULE=siga.settings.pg_load python manage.py migrate
    DJANGO_SETTINGS_MODULE=siga.settings.pg_load python manage.py migrate_data_from_mysql

Tous les identifiants viennent de l'env (aucun mot de passe en dur) :
  PG cible    : PG_TARGET_DB / PG_TARGET_USER / PG_TARGET_PASSWORD / PG_TARGET_HOST / PG_TARGET_PORT
  MySQL source: SRC_DB / SRC_USER / SRC_PASSWORD / SRC_HOST / SRC_PORT
"""
from decouple import config

from .base import *  # noqa: F401,F403

DATABASES = {
    # Cible PostgreSQL (schéma créé par migrate, données par l'ETL).
    'default': {
        'ENGINE':   'django.db.backends.postgresql',
        'NAME':     config('PG_TARGET_DB',       default='IPGEI'),
        'USER':     config('PG_TARGET_USER',     default='postgres'),
        'PASSWORD': config('PG_TARGET_PASSWORD', default=''),
        'HOST':     config('PG_TARGET_HOST',     default='localhost'),
        'PORT':     config('PG_TARGET_PORT',     default='5432'),
    },
    # Source MySQL — LECTURE SEULE (l'ETL n'écrit jamais dessus).
    'mysql_source': {
        'ENGINE':   'django.db.backends.mysql',
        'NAME':     config('SRC_DB',       default='siga'),
        'USER':     config('SRC_USER',     default='root'),
        'PASSWORD': config('SRC_PASSWORD', default=''),
        'HOST':     config('SRC_HOST',     default='localhost'),
        'PORT':     config('SRC_PORT',     default='3306'),
        'OPTIONS':  {'init_command': "SET sql_mode='STRICT_TRANS_TABLES'"},
    },
}
