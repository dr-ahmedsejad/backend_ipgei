"""
SIGA — Système Intégré de Gestion Académique
Base settings (shared between dev and prod)
"""
from pathlib import Path
from datetime import timedelta
from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = config('SECRET_KEY', default='dev-secret-key-change-in-prod')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'django_filters',
    'axes',
    'drf_spectacular',

    # SIGA apps — existantes
    'apps.authentication',
    'apps.absence',
    'apps.avancement',
    'apps.banque',
    'apps.departement',
    'apps.em',
    'apps.emplois',
    'apps.parametres',
    'apps.prof',
    'apps.salle',
    'apps.suivi',
    'apps.vacation',
    # Core (audit, permissions, mixins)
    'core',
    # SIGA apps — nouvelles (scolarite LMD)
    'apps.modules',
    'apps.scolarite',
    'apps.inscriptions',
    'apps.evaluations',
    'apps.stages',
    'apps.documents',
    'apps.notifications',
    # Portail étudiant
    'apps.reclamations',
    'apps.portail',
    # Audit (journal lecture-seule)
    'apps.audit',
    # Sauvegardes BD (matrice + telechargement)
    'apps.backup',
    # Module « Cours » — fil pédagogique par élément de module. EN LIGNE
    # UNIQUEMENT : ses tables sont exclues de la synchronisation, si bien
    # qu'un cours publié depuis le portail n'est jamais écrasé par un push.
    'apps.cours',

    # IPGEI — moteur academique classes preparatoires (MPSI/MP).
    # Remplace la couche academique LMD ; n'altere aucun modele du socle.
    'apps.ipgei',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'axes.middleware.AxesMiddleware',                          # après AuthenticationMiddleware
    # Rôle d'instance : sur le VPS, refuse les écritures hors boîte de réception.
    # Après l'authentification — le refus doit pouvoir nommer l'utilisateur dans
    # les journaux — et avant l'audit, qui n'a pas à tracer une écriture rejetée.
    'core.mirror.MirrorReadOnlyMiddleware',
    'core.middleware.AuditMiddleware',                          # après auth + axes
    'core.cache_middleware.cache_control_lookup_middleware',    # F-5 : Cache-Control sur lookups
    # Rabat `Content-Disposition: attachment` sur `inline` pour les seules
    # requetes fetch/XHR : sans quoi les gestionnaires de telechargement (IDM)
    # detournent la reponse et tuent la requete du frontend. Voir le module.
    'core.download_middleware.attachment_inline_pour_fetch_middleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# ── Rôle de l'instance : autorité locale ou miroir en ligne ───────────────────
# LOCAL (autorité) : MIRROR_MODE=False, la valeur par défaut. VPS (miroir en
# lecture seule) : True. Voir core/mirror.py — un seul code, deux rôles.
MIRROR_MODE = config('MIRROR_MODE', default=False, cast=bool)

# Chemins où l'écriture EN LIGNE reste permise malgré le miroir. Toute nouvelle
# fonction « boîte de réception » doit ajouter le sien ici, faute de quoi elle
# répondra 403 sur le VPS — et seulement là, ce qui est le plus mauvais des
# défauts : invisible en développement.
MIRROR_WRITE_ALLOWLIST = (
    '/api/v1/auth/',                       # connexion, déconnexion, rafraîchissement
    '/admin/login',                        # connexion à l'admin Django
    '/api/v1/portail/reclamer-seance/',    # réclamation de séance (enseignant)
    '/api/v1/saisie-en-ligne/',            # saisie de notes en ligne (brouillon)
    '/api/v1/cours/',                      # fil pédagogique publié en ligne
)

# ── Synchronisation LOCAL → VPS (miroir) — voir core/sync.py ──────────────────
PG_DUMP_BIN  = config('PG_DUMP_BIN',  default='pg_dump')   # binaire ≥ version serveur
SYNC_WORKDIR = config('SYNC_WORKDIR', default='') or None  # défaut : dossier temporaire

# Tables dont seules les DONNÉES sont écartées : le schéma part, la table est
# recréée vide à la restauration. Journaux et jetons — leur contenu est propre à
# chaque instance, et l'écraser ne coûte rien.
SYNC_EXCLUDE_TABLE_DATA = [
    'core_audit_log', 'django_session', 'axes_accesslog',
    'token_blacklist_outstandingtoken', 'token_blacklist_blacklistedtoken',
]

# Tables ENTIÈREMENT écartées du dump — ni schéma, ni DROP, ni données : la
# BOÎTE DE RÉCEPTION. La restauration ne les touche pas, si bien que ce qui a été
# écrit en ligne y survit au push suivant.
#
# IMPÉRATIF : aucune de ces tables ne doit porter de ForeignKey vers une table
# miroir. Le dump restaure avec DROP ... CASCADE ; une FK entrante ferait échouer
# le DROP de la table visée, et la restauration entière avec lui. D'où le dessin
# « FK-free » : des *_id en IntegerField et un instantané du libellé.
SYNC_EXCLUDE_TABLE = [
    'reclamations_reclamation_seance',
    'saisie_note_en_ligne',
    # Le fil pédagogique vit EN LIGNE et nulle part ailleurs : il n'a pas
    # d'équivalent local à refléter. Les pièces jointes et les réponses portent
    # une FK vers `cours` — inbox vers inbox, ce que l'invariant autorise, la
    # table visée n'étant elle-même jamais droppée.
    'cours', 'cours_piece_jointe', 'cours_reponse',
]

# Cible SSH du VPS. Vide = mode local, la synchronisation s'arrête après le dump
# et se contente de le décrire — utile pour éprouver la chaîne sans VPS.
SYNC_SSH_HOST     = config('SYNC_SSH_HOST',     default='')
SYNC_SSH_USER     = config('SYNC_SSH_USER',     default='ipgei-deploy')
SYNC_SSH_KEY      = config('SYNC_SSH_KEY',      default='')
SYNC_RECEIVER_CMD = config('SYNC_RECEIVER_CMD', default='sync')
# Où atterrissent les fichiers /media sur le portail. La base ne porte que
# leurs chemins : sans ce transfert, le portail affiche des liens morts.
SYNC_MEDIA_DEST  = config('SYNC_MEDIA_DEST', default='/opt/ipgei/media/')

# Configuration de la rétention des logs d'audit (Option C : 90j HOT + 365j ARCHIVE)
AUDIT_RETENTION = {
    'HOT_DAYS':      90,
    'ARCHIVE_DAYS':  365,
    'EXPORT_PATH':   BASE_DIR / 'backups' / 'audit',
    'EXPORT_FORMAT': 'jsonl.gz',
}

ROOT_URLCONF = 'siga.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'siga.wsgi.application'

# ── Database ──────────────────────────────────────────────────────────────────
# Branche PostgreSQL : le moteur PAR DÉFAUT est PostgreSQL (la branche `main`
# garde MySQL par défaut). Reste pilotable par DB_ENGINE (postgresql|mysql) si
# besoin de rebasculer, mais AUCUNE variable n'est requise sur cette branche —
# un clone frais tourne sur PostgreSQL sans configuration.
DB_ENGINE = config('DB_ENGINE', default='postgresql').strip().lower()  # 'postgresql' | 'mysql'
if DB_ENGINE not in ('mysql', 'postgresql'):
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(
        f"DB_ENGINE invalide : {DB_ENGINE!r}. Valeurs acceptées : 'postgresql' ou 'mysql' — "
        "refus explicite (pas de retombée silencieuse)."
    )
_IS_POSTGRES = DB_ENGINE == 'postgresql'

# ── Garde-fou IPGEI : isolation totale de SIGA (plan §1) ─────────────────────
# IPGEI est un produit SEPARE forke du socle SIGA. Il ne doit JAMAIS ouvrir la
# base de SIGA : une seule migration lancee par erreur sur `siga_pg` altererait
# la production. On refuse donc explicitement au demarrage tout nom de base
# connu comme appartenant a SIGA, plutot que de laisser une faute de frappe
# passer silencieusement.
_DB_NAME = config('DB_NAME', default='IPGEI')
_BASES_SIGA = {'siga_pg', 'siga', 'gesafped', 'gesafped26'}
if _DB_NAME.strip().lower() in _BASES_SIGA:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(
        f"DB_NAME={_DB_NAME!r} appartient a SIGA. IPGEI doit utiliser sa propre "
        "base (DB_NAME=IPGEI). Refus de demarrer pour proteger SIGA."
    )

DATABASES = {
    'default': {
        'ENGINE':   'django.db.backends.postgresql' if _IS_POSTGRES else 'django.db.backends.mysql',
        'NAME':     _DB_NAME,
        'USER':     config('DB_USER',     default='postgres' if _IS_POSTGRES else 'root'),
        'PASSWORD': config('DB_PASSWORD', default=''),
        'HOST':     config('DB_HOST',     default='localhost'),
        # Défaut du port conditionné par le moteur (5432 PG / 3306 MySQL) ;
        # surchargeable via DB_PORT comme avant.
        'PORT':     config('DB_PORT', default='5432' if _IS_POSTGRES else '3306'),
        # init_command est une syntaxe purement MySQL — aucun équivalent requis
        # côté PG (STRICT_TRANS_TABLES est le comportement natif de PostgreSQL).
        'OPTIONS':  {} if _IS_POSTGRES else {
            'init_command': "SET default_storage_engine=INNODB, sql_mode='STRICT_TRANS_TABLES'",
        },
        'CONN_MAX_AGE': 60,
    }
}

# ── Auth ──────────────────────────────────────────────────────────────────────
AUTH_USER_MODEL = 'authentication.CustomUser'

AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',   # doit être en premier
    'django.contrib.auth.backends.ModelBackend',
]

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ── Cache (Redis si REDIS_URL configuré, sinon mémoire locale) ───────────────
# En production : ajouter REDIS_URL=redis://localhost:6379/1 dans .env
# Sans Redis : LocMemCache fonctionne mais n'est pas partagé entre processus
_REDIS_URL = config('REDIS_URL', default='')
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': _REDIS_URL,
    } if _REDIS_URL else {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'siga-rbac',
    }
}

# ── DRF ───────────────────────────────────────────────────────────────────────
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'core.authentication.CookieJWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
        'rest_framework.parsers.MultiPartParser',
        'rest_framework.parsers.FormParser',
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_PAGINATION_CLASS': 'core.pagination.StandardPagination',
    'PAGE_SIZE': 10,
    'DEFAULT_THROTTLE_CLASSES': [],  # Pas de throttle global — chaque view sensible a ses propres throttle_classes
    'DEFAULT_THROTTLE_RATES': {
        'anon':               '20/minute',
        'user':               '200/minute',
        'login':              '5/15min',   # LoginView — 5 tentatives par 15 min par IP
        'sensitive_endpoint': '5/hour',    # ChangePasswordView
        'admin_action':       '60/minute', # UserToggleView, RoleToggleView
        'backup_download':    '10/hour',   # Anti-exfiltration de masse
        'backup_manual':      '5/hour',    # Generation manuelle (lourde)
        'verify':             '30/minute', # /verifier public — anti-balayage de tokens
    },
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'EXCEPTION_HANDLER': 'core.exceptions.custom_exception_handler',
}

# ── JWT ───────────────────────────────────────────────────────────────────────
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME':  timedelta(minutes=config('JWT_ACCESS_TOKEN_LIFETIME_MINUTES', default=60, cast=int)),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=config('JWT_REFRESH_TOKEN_LIFETIME_DAYS',    default=7,  cast=int)),
    'ROTATE_REFRESH_TOKENS':  True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'AUTH_COOKIE':         'access_token',
    'AUTH_COOKIE_REFRESH': 'refresh_token',
    'AUTH_COOKIE_SECURE':   False,
    'AUTH_COOKIE_HTTP_ONLY': True,
    'AUTH_COOKIE_SAMESITE':  'Lax',
}

# ── CORS ──────────────────────────────────────────────────────────────────────
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOWED_ORIGINS = config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:3001,http://localhost:3002'
).split(',')

# ── Base URL publique des documents (QR de vérification) ──────────────────────
# Domaine FIXE de vérification des documents officiels : le QR code encode
# {DOCUMENTS_BASE_URL}/verifier/{token}. Scanné par un téléphone, il ouvre la page
# publique de vérification. Le domaine de vérification est FIXE — https://ent.ipgei.mr
# — et sert de défaut (indépendant de DOMAIN, qui gère hosts/CORS). Surchargeable via
# la variable d'env DOCUMENTS_BASE_URL (ex. en dev local pour pointer vers le
# frontend de test). DOIT rester une URL absolue avec hôte.
DOCUMENTS_BASE_URL = config('DOCUMENTS_BASE_URL', default='https://ent.ipgei.mr')

# ── Signature numérique des PDF officiels (PAdES — anti-falsification) ─────────
# Si activée ET le certificat PKCS#12 existe, chaque document officiel est signé
# numériquement (pyHanko) : toute modification d'octet devient détectable par un
# lecteur PDF (Adobe : « le document a été modifié depuis la signature »).
# Certificat AUTO-SIGNÉ pour l'instant (`manage.py generer_certificat_signature`),
# à remplacer par un certificat d'AC officielle quand disponible.
# NON BLOQUANT : si désactivé / certificat absent / lib indisponible → PDF non signé
# (la génération n'échoue jamais). Le .p12 est hors-git (voir .gitignore).
PDF_SIGNING_ENABLED      = config('PDF_SIGNING_ENABLED', default=True, cast=bool)
PDF_SIGN_PKCS12_PATH     = config('PDF_SIGN_PKCS12_PATH', default=str(BASE_DIR / 'secrets' / 'doc_signing.p12'))
PDF_SIGN_PKCS12_PASSWORD = config('PDF_SIGN_PKCS12_PASSWORD', default='')
PDF_SIGN_REASON          = config('PDF_SIGN_REASON', default="Document officiel — authenticité et intégrité")
PDF_SIGN_LOCATION        = config('PDF_SIGN_LOCATION', default='Nouakchott, Mauritanie')

# ── Suivi : delai de grace pour generer une semaine apres sa fin ──────────────
# Une fois la semaine ecoulee (today > date_fin), les non-admins ne peuvent
# plus generer son suivi. Grace period optionnelle pour couvrir les oublis
# (ex. responsable de retour de mission courte). Valeur 0 = strict.
# Bypass : role='admin' ou is_superuser=True peuvent toujours generer.
SUIVI_GRACE_DAYS_AFTER_WEEK_END = config(
    'SUIVI_GRACE_DAYS_AFTER_WEEK_END', default=0, cast=int,
)


# ── Module Sauvegardes BD ────────────────────────────────────────────────────
# Dossier racine ou les sauvegardes sont stockees sur disque. Sous-dossiers :
#   daily/   weekly/   monthly/   manual/
# Sous Linux prod : /home/backups. Sous Windows dev : ajuster via .env.
BACKUP_BASE_DIR = config(
    'BACKUP_BASE_DIR',
    default=str(BASE_DIR / 'local_backups'),  # fallback dev local
)
# Fichier credentials MySQL pour les scripts (chmod 600). Le user doit avoir
# au moins SELECT + LOCK TABLES + RELOAD + TRIGGER + EVENT + SHOW VIEW.
BACKUP_DB_CONFIG_PATH = config(
    'BACKUP_DB_CONFIG_PATH',
    default='/etc/siga-backup/db.cnf',
)
# Binaires utilises par le generator manuel (peuvent etre des chemins absolus
# si pas dans PATH, p.ex. WAMP sous Windows).
BACKUP_MYSQLDUMP_BIN = config('BACKUP_MYSQLDUMP_BIN', default='mysqldump')
# Équivalent PG (utilisé UNIQUEMENT quand connection.vendor == 'postgresql',
# donc inerte tant que DB_ENGINE reste 'mysql' — préparation cutover PG).
BACKUP_PGDUMP_BIN    = config('BACKUP_PGDUMP_BIN',    default='pg_dump')
BACKUP_OPENSSL_BIN   = config('BACKUP_OPENSSL_BIN',   default='openssl')
# Retention des backups manuels chiffres (jours). Le cleanup tourne via cron.
BACKUP_MANUAL_RETENTION_DAYS = config(
    'BACKUP_MANUAL_RETENTION_DAYS', default=7, cast=int,
)
# Mot de passe minimum pour le bouton manuel chiffre.
BACKUP_MANUAL_MIN_PASSWORD_LENGTH = config(
    'BACKUP_MANUAL_MIN_PASSWORD_LENGTH', default=16, cast=int,
)
# Inclure le dossier media (avatars, PDF officiels, justificatifs) dans
# le .7z du bouton manuel. Si False, seul siga.sql est inclus.
BACKUP_INCLUDE_MEDIA = config('BACKUP_INCLUDE_MEDIA', default=True, cast=bool)
# Plafond anti-DoS : si BD + media depasse cette taille en MB, la generation
# manuelle echoue avec un message clair (suggere les backups cron).
BACKUP_MANUAL_MAX_SIZE_MB = config(
    'BACKUP_MANUAL_MAX_SIZE_MB', default=5000, cast=int,
)
# Niveau de compression LZMA2 (1=rapide, 9=max). 5 = bon equilibre temps/taille.
BACKUP_LZMA2_PRESET = config('BACKUP_LZMA2_PRESET', default=5, cast=int)


CORS_ALLOW_HEADERS = [
    'accept', 'accept-encoding', 'authorization',
    'content-type', 'dnt', 'origin',
    'user-agent', 'x-csrftoken', 'x-requested-with',
]

# ── Axes (brute-force) ────────────────────────────────────────────────────────
AXES_FAILURE_LIMIT        = config('AXES_FAILURE_LIMIT',    default=5,  cast=int)
AXES_COOLOFF_TIME         = timedelta(minutes=config('AXES_COOLOFF_MINUTES', default=15, cast=int))
AXES_LOCK_OUT_AT_FAILURE  = True
AXES_RESET_ON_SUCCESS     = True
AXES_LOCKOUT_PARAMETERS   = ['ip_address']         # bloquer par IP uniquement
AXES_HTTP_RESPONSE_CODE   = 429
AXES_LOCKOUT_TEMPLATE     = None
AXES_ENABLE_ADMIN         = True
# Retourne du JSON 429 au lieu d'une page HTML quand l'IP est bloquée
AXES_LOCKOUT_CALLABLE     = 'core.axes_utils.axes_lockout_callback'

# ── OpenAPI / Spectacular ─────────────────────────────────────────────────────
SPECTACULAR_SETTINGS = {
    'TITLE': 'SIGA API',
    'DESCRIPTION': 'Système Intégré de Gestion Académique — API REST v1',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'COMPONENT_SPLIT_REQUEST': True,
    'SECURITY': [{'cookieAuth': []}],
}

# ── Internationalisation ──────────────────────────────────────────────────────
LANGUAGE_CODE = 'fr-fr'
TIME_ZONE     = 'Africa/Nouakchott'
USE_I18N      = True
USE_TZ        = True

# ── Static / Media ────────────────────────────────────────────────────────────
STATIC_URL  = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

MEDIA_URL  = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Garde-fou anti-DoS sur les uploads (complète les validators par champ).
# Les imports xlsx volumineux sont bornés en taille/lignes dans leurs vues.
DATA_UPLOAD_MAX_MEMORY_SIZE   = 5 * 1024 * 1024   # 5 Mo
FILE_UPLOAD_MAX_MEMORY_SIZE   = 5 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── Logging ───────────────────────────────────────────────────────────────────
import os
os.makedirs(BASE_DIR / 'logs', exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {'format': '{levelname} {asctime} {module} {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose'},
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': str(BASE_DIR / 'logs' / 'siga.log'),
            'maxBytes': 5 * 1024 * 1024,
            'backupCount': 5,
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'WARNING'},
        'siga':   {'handlers': ['console', 'file'], 'level': 'INFO', 'propagate': False},
    },
}
