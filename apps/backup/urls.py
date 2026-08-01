from rest_framework.routers import DefaultRouter

from .views import (
    BackupArtifactViewSet, BackupDownloadGrantViewSet, BackupDownloadLogViewSet,
)


router = DefaultRouter()
# /backups/                  -> liste, /backups/<id>/, /backups/<id>/download/,
#                              /backups/manual/, /backups/me/
router.register(r'grants', BackupDownloadGrantViewSet, basename='backup-grants')
router.register(r'logs',   BackupDownloadLogViewSet,   basename='backup-logs')
router.register(r'',       BackupArtifactViewSet,      basename='backups')

urlpatterns = router.urls
