"""
SIGA — URL racine
Toutes les routes API sont préfixées par /api/v1/
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView

from core.health import healthz
from core.media_auth import MediaAuthView
from core.mirror import instance_info
from core.sync import SyncHistoryView, SyncPushView

urlpatterns = [
    # Admin Django
    path('admin/', admin.site.urls),

    # Sonde de santé (application + base) pour la supervision.
    path('healthz/', healthz, name='healthz'),

    # Contrôle d'accès aux fichiers /media/ (sous-requête Nginx auth_request).
    path('internal/media-auth/', MediaAuthView.as_view(), name='media-auth'),

    # ── Rôle de l'instance et publication vers le miroir ─────────────────────
    # `instance` est PUBLIC : le frontend le lit avant même la connexion, pour
    # savoir s'il parle à l'autorité ou à son reflet.
    path('api/v1/instance/',     instance_info,             name='instance-info'),
    path('api/v1/sync/push/',    SyncPushView.as_view(),    name='sync-push'),
    path('api/v1/sync/history/', SyncHistoryView.as_view(), name='sync-history'),

    # ── API v1 ───────────────────────────────────────────────────────────────
    path('api/v1/saisie-en-ligne/', include('apps.reclamations.urls_saisie')),
    path('api/v1/cours/',           include('apps.cours.urls')),
    path('api/v1/auth/',        include('apps.authentication.urls')),
    path('api/v1/parametres/',  include('apps.parametres.urls')),
    path('api/v1/banques/',     include('apps.banque.urls')),
    path('api/v1/salles/',      include('apps.salle.urls')),
    path('api/v1/departements/',include('apps.departement.urls')),
    path('api/v1/ems/',         include('apps.em.urls')),
    path('api/v1/profs/',       include('apps.prof.urls')),
    path('api/v1/prof-type-history/', include('apps.prof.history_urls')),
    path('api/v1/emplois/',     include('apps.emplois.urls')),
    path('api/v1/suivi/',       include('apps.suivi.urls')),
    path('api/v1/absences/',    include('apps.absence.urls')),
    path('api/v1/vacations/',   include('apps.vacation.urls')),
    path('api/v1/avancement/',  include('apps.avancement.urls')),
    # ── Scolarite LMD ────────────────────────────────────────────────────────
    path('api/v1/scolarite/',    include('apps.scolarite.urls')),
    path('api/v1/',              include('apps.modules.urls')),
    path('api/v1/inscriptions/', include('apps.inscriptions.urls')),
    path('api/v1/evaluations/',  include('apps.evaluations.urls')),
    path('api/v1/stages/',       include('apps.stages.urls')),
    path('api/v1/documents/',    include('apps.documents.urls')),
    path('api/v1/notifications/', include('apps.notifications.urls')),
    path('api/v1/annonces/',      include('apps.annonces.urls')),
    # Portail étudiant
    path('api/v1/portail/',       include('apps.portail.urls')),
    path('api/v1/reclamations/',  include('apps.reclamations.urls')),
    # Journal d'audit (lecture seule)
    path('api/v1/audit/',         include('apps.audit.urls')),
    # Sauvegardes BD (liste, download, matrice grants, audit log)
    path('api/v1/backups/',       include('apps.backup.urls')),
    # ── IPGEI — moteur academique classes preparatoires ──────────────────────
    path('api/v1/ipgei/',         include('apps.ipgei.urls')),
]

# OpenAPI docs — exposées hors production uniquement (reconnaissance API en prod).
if settings.DEBUG:
    urlpatterns += [
        path('api/schema/', SpectacularAPIView.as_view(),                      name='schema'),
        path('api/docs/',   SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
        path('api/redoc/',  SpectacularRedocView.as_view(url_name='schema'),   name='redoc'),
    ]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
