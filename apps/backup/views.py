"""
Endpoints DRF du module backup.

  GET    /api/v1/backups/                     liste des artifacts dispo
  GET    /api/v1/backups/me/                  est-ce que j'ai le droit ?
  GET    /api/v1/backups/{id}/download/       stream download + audit
  POST   /api/v1/backups/manual/              genere un backup chiffre
  GET    /api/v1/backups/grants/              liste users autorises (admin)
  POST   /api/v1/backups/grants/              accorder a un user (admin)
  DELETE /api/v1/backups/grants/{id}/         retirer (admin)
  GET    /api/v1/backups/logs/                audit log (admin)
"""
import logging

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.mixins import (
    CreateModelMixin, DestroyModelMixin, ListModelMixin,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet, ReadOnlyModelViewSet

from .models import BackupArtifact, BackupDownloadGrant, BackupDownloadLog
from .permissions import (
    CanDownloadBackup, IsBackupGrantManager, user_can_download,
)
from .serializers import (
    BackupArtifactSerializer, BackupDownloadGrantSerializer,
    BackupDownloadLogSerializer, ManualBackupRequestSerializer,
)
from .services.generator import (
    BackupGenerationError, generate_manual_encrypted_backup,
)
from .services.scanner import scan_backup_directories
from .services.streaming import stream_artifact_download
from .throttles import BackupDownloadThrottle, ManualBackupThrottle


logger = logging.getLogger(__name__)


class BackupArtifactViewSet(ReadOnlyModelViewSet):
    """
    Liste + telechargement des sauvegardes presentes sur disque.

    Filtrable par type ?type=daily_2h,manual...
    Tri par defaut : plus recent d'abord.
    """
    serializer_class    = BackupArtifactSerializer
    permission_classes  = [IsAuthenticated, CanDownloadBackup]
    queryset            = BackupArtifact.objects.all()

    def get_permissions(self):
        # `me` doit etre accessible a tout user authentifie (pour que le
        # frontend sache s'il faut afficher l'item de menu ou pas). Override
        # explicite ici pour ne PAS dependre du mecanisme @action(...) qui
        # peut etre court-circuite selon le mode de dispatch.
        if self.action == 'me':
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_queryset(self):
        qs = BackupArtifact.objects.all()
        types = self.request.query_params.get('type')
        if types:
            qs = qs.filter(type__in=[t.strip() for t in types.split(',') if t.strip()])
        # Ne montrer que les disponibles par defaut (sauf ?include_missing=1)
        if self.request.query_params.get('include_missing') != '1':
            qs = qs.filter(disk_available=True)
        return qs.order_by('-created_at')

    def list(self, request, *args, **kwargs):
        # Rafraichissement opportuniste : detecte les nouveaux fichiers sans
        # attendre le cron. Tres rapide (lecture metadonnees seulement).
        try:
            scan_backup_directories()
        except Exception:
            logger.exception('Scan opportuniste en debut de listing : erreur')
        return super().list(request, *args, **kwargs)

    @action(
        detail=False, methods=['get'], url_path='me',
        permission_classes=[IsAuthenticated],   # tout authentifie peut savoir
    )
    def me(self, request):
        """Retourne {can_download: bool}. Sert l'UI a savoir s'il faut afficher le menu."""
        return Response({'can_download': user_can_download(request.user)})

    @action(
        detail=True, methods=['get'], url_path='download',
        throttle_classes=[BackupDownloadThrottle],
    )
    def download(self, request, pk=None):
        """Stream le fichier + INSERT BackupDownloadLog (audit immuable)."""
        artifact = self.get_object()  # applique CanDownloadBackup deja
        return stream_artifact_download(
            user=request.user, artifact=artifact, request=request,
        )

    @action(
        detail=False, methods=['post'], url_path='manual',
        throttle_classes=[ManualBackupThrottle],
    )
    def manual(self, request):
        """
        Genere un backup chiffre AES-256 et retourne le metadata.
        Le frontend peut ensuite appeler /backups/<id>/download/ pour recuperer
        le fichier (ou faire un POST + redirect immediat).
        """
        ser = ManualBackupRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            artifact = generate_manual_encrypted_backup(
                user=request.user,
                password=ser.validated_data['password'],
                notes=ser.validated_data.get('notes', ''),
            )
        except BackupGenerationError as e:
            raise ValidationError({'detail': str(e)})

        try:
            from core.audit_helpers import write_audit
            write_audit(
                action='CREATE', model_name='BackupArtifact', object_id=str(artifact.pk),
                changes={'type': 'sauvegarde manuelle chiffrée'},
                label=f'Génération sauvegarde manuelle #{artifact.pk}', keep_forever=True,
            )
        except Exception:
            import logging
            logging.getLogger('siga').warning('Audit génération backup échoué', exc_info=True)

        return Response(
            BackupArtifactSerializer(artifact).data,
            status=status.HTTP_201_CREATED,
        )


class BackupDownloadGrantViewSet(ListModelMixin, CreateModelMixin,
                                  DestroyModelMixin, GenericViewSet):
    """
    Matrice (= liste blanche) des users autorises a telecharger.
    Strictement admin/superuser.

    POST /api/v1/backups/grants/   { "user": 42, "notes": "..." }
    DELETE /api/v1/backups/grants/<id>/
    """
    serializer_class    = BackupDownloadGrantSerializer
    permission_classes  = [IsAuthenticated, IsBackupGrantManager]
    queryset            = BackupDownloadGrant.objects.select_related(
        'user', 'granted_by',
    ).all()

    def perform_create(self, serializer):
        # Empeche un double-grant pour le meme user (OneToOne -> integrite DB
        # mais on prefere une 400 plutot qu'une 500)
        if BackupDownloadGrant.objects.filter(
            user_id=serializer.validated_data['user'].pk,
        ).exists():
            raise ValidationError({'user': 'Cet utilisateur est deja autorise.'})
        # Empeche s'auto-grant (deja admin de toute facon, mais soyons explicites)
        if serializer.validated_data['user'].pk == self.request.user.pk:
            raise ValidationError({'user': 'Vous ne pouvez pas vous accorder le droit a vous-meme.'})
        serializer.save(granted_by=self.request.user)
        try:
            from core.audit_helpers import write_audit
            g = serializer.instance
            write_audit(
                action='CREATE', model_name='BackupDownloadGrant', object_id=str(g.pk),
                changes={'user': str(g.user)},
                label=f'Autorisation téléchargement sauvegarde → {g.user}', keep_forever=True,
            )
        except Exception:
            import logging
            logging.getLogger('siga').warning('Audit octroi droit backup échoué', exc_info=True)

    def perform_destroy(self, instance):
        user_repr = str(getattr(instance, 'user', '?'))
        gid = instance.pk
        instance.delete()
        try:
            from core.audit_helpers import write_audit
            write_audit(
                action='DELETE', model_name='BackupDownloadGrant', object_id=str(gid),
                changes={'user': user_repr},
                label=f'Révocation droit téléchargement sauvegarde → {user_repr}', keep_forever=True,
            )
        except Exception:
            import logging
            logging.getLogger('siga').warning('Audit révocation droit backup échoué', exc_info=True)


class BackupDownloadLogViewSet(ReadOnlyModelViewSet):
    """
    Audit log : strictement lecture. Admin uniquement.

    Filtres : ?user=<id> ?artifact=<id> ?success=true/false
    """
    serializer_class    = BackupDownloadLogSerializer
    permission_classes  = [IsAuthenticated, IsBackupGrantManager]

    def get_queryset(self):
        qs = BackupDownloadLog.objects.select_related('user', 'artifact').all()
        params = self.request.query_params
        if 'user' in params:
            qs = qs.filter(user_id=params['user'])
        if 'artifact' in params:
            qs = qs.filter(artifact_id=params['artifact'])
        if 'success' in params:
            qs = qs.filter(success=params['success'].lower() == 'true')
        return qs.order_by('-downloaded_at')
