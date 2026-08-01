from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend

from .models import Notification
from .serializers import NotificationSerializer


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    # Pas de RBAC ici : le queryset isole strictement par destinataire,
    # donc tout user authentifié ne voit que ses propres notifications.
    serializer_class   = NotificationSerializer
    permission_classes = [IsAuthenticated]
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['lue', 'type']

    def get_queryset(self):
        return Notification.objects.filter(destinataire=self.request.user)

    @action(detail=False, methods=['get'], url_path='unread-count')
    def unread_count(self, request):
        count = self.get_queryset().filter(lue=False).count()
        return Response({'count': count})

    @action(detail=True, methods=['post'], url_path='lire')
    def lire(self, request, pk=None):
        notif = self.get_object()
        notif.lue = True
        notif.save(update_fields=['lue'])
        return Response(NotificationSerializer(notif).data)

    @action(detail=False, methods=['post'], url_path='tout-lire')
    def tout_lire(self, request):
        updated = self.get_queryset().filter(lue=False).update(lue=True)
        return Response({'updated': updated})
