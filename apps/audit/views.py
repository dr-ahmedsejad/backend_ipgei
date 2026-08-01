"""
AuditLogViewSet — endpoints REST en lecture seule sur le journal d'audit.

Acces :
  - Admin / IT : acces global (toute l'entreprise)
  - Autres roles : restriction au by-entity (objet possede ou consulte)
                   et a leurs propres actions

Endpoints :
  GET /api/v1/audit/                  -> liste paginee + filtres
  GET /api/v1/audit/{id}/             -> detail complet
  GET /api/v1/audit/by-entity/        -> ?model=X&object_id=Y (timeline)
  GET /api/v1/audit/stats/            -> stats globales (counts by action/model)
  GET /api/v1/audit/export/           -> export CSV streame
"""
import csv
from datetime import timedelta

from django.db.models import Count
from django.http import StreamingHttpResponse
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import AuditLog
from core.permissions import IsAdminOrIT

from .filters import AuditLogFilter
from .serializers import AuditLogListSerializer, AuditLogSerializer


class _Echo:
    """Pseudo-fichier pour StreamingHttpResponse + csv.writer."""
    def write(self, value):
        return value


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Journal d'audit en lecture seule.
    L'append-only est garanti cote modele : aucune ecriture/destruction possible.
    """
    queryset = AuditLog.objects.select_related('user', 'institution').all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = AuditLogFilter

    def get_serializer_class(self):
        if self.action == 'list':
            return AuditLogListSerializer
        return AuditLogSerializer

    def _is_admin_or_it(self, user):
        return bool(user and user.is_authenticated
                    and (user.role in ('admin', 'IT') or user.is_superuser))

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user

        # Admin/IT : acces global
        if self._is_admin_or_it(user):
            return qs

        # Autres : limite a leurs propres actions OU by-entity (gere dans l'action)
        if self.action == 'by_entity':
            return qs  # restriction object-level appliquee separement
        return qs.filter(user=user)

    def get_permissions(self):
        # stats / export / by_entity reserves admin/IT : by_entity renvoie la
        # timeline complete d'une entite (model+object_id), sans restriction
        # object-level cote queryset → reserve aux admins pour eviter l'IDOR.
        if self.action in ('stats', 'export', 'by_entity'):
            return [IsAdminOrIT()]
        return super().get_permissions()

    # ── /api/v1/audit/by-entity/?model=X&object_id=Y ────────────────────────
    @action(detail=False, methods=['get'], url_path='by-entity')
    def by_entity(self, request):
        model = request.query_params.get('model', '').strip()
        object_id = request.query_params.get('object_id', '').strip()
        if not model or not object_id:
            return Response(
                {'detail': 'Parametres requis : model, object_id'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qs = (AuditLog.objects
              .select_related('user', 'institution')
              .filter(model_name=model, object_id=str(object_id))
              .order_by('-timestamp'))

        page = self.paginate_queryset(qs)
        if page is not None:
            ser = AuditLogListSerializer(page, many=True)
            return self.get_paginated_response(ser.data)
        ser = AuditLogListSerializer(qs, many=True)
        return Response(ser.data)

    # ── /api/v1/audit/stats/ ────────────────────────────────────────────────
    @action(detail=False, methods=['get'])
    def stats(self, request):
        days = int(request.query_params.get('days', '30'))
        days = max(1, min(days, 365))
        since = timezone.now() - timedelta(days=days)

        base = AuditLog.objects.filter(timestamp__gte=since)
        total = base.count()
        by_action = list(
            base.values('action')
                .annotate(n=Count('id'))
                .order_by('-n')
        )
        by_model = list(
            base.values('model_name')
                .annotate(n=Count('id'))
                .order_by('-n')[:20]
        )
        by_user = list(
            base.exclude(user=None)
                .values('user_id', 'user__username')
                .annotate(n=Count('id'))
                .order_by('-n')[:20]
        )
        return Response({
            'period_days': days,
            'total': total,
            'by_action': by_action,
            'by_model': by_model,
            'by_user': by_user,
        })

    # ── /api/v1/audit/export/ ───────────────────────────────────────────────
    @action(detail=False, methods=['get'])
    def export(self, request):
        """Export CSV streame (memoire constante)."""
        qs = self.filter_queryset(self.get_queryset())[:50000]

        cols = [
            'id', 'timestamp', 'user', 'role', 'action',
            'model', 'object_id', 'label',
            'institution', 'ip', 'endpoint', 'http_method',
        ]

        def rows():
            writer = csv.writer(_Echo())
            yield writer.writerow(cols)
            for log in qs.iterator(chunk_size=500):
                yield writer.writerow([
                    log.id,
                    log.timestamp.isoformat(),
                    getattr(log.user, 'username', '') if log.user_id else '',
                    getattr(log.user, 'role', '') if log.user_id else '',
                    log.action,
                    log.model_name,
                    log.object_id,
                    log.label,
                    str(log.institution) if log.institution_id else '',
                    log.ip_address or '',
                    log.endpoint,
                    log.http_method,
                ])

        resp = StreamingHttpResponse(rows(), content_type='text/csv')
        ts = timezone.now().strftime('%Y%m%d_%H%M%S')
        resp['Content-Disposition'] = f'attachment; filename="audit_{ts}.csv"'
        return resp
