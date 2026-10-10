from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from django_filters.rest_framework import DjangoFilterBackend

from .models import AppareilPush, Notification
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

    # ── Notifications push (app Groupe Polytechnique) — reprises de l'ISS ─────
    @action(detail=False, methods=['post', 'delete'], url_path='appareils')
    def appareils(self, request):
        """POST : inscrit le téléphone (jeton FCM) ; DELETE : le désinscrit
        (« Se déconnecter »). Corps : {"jeton", "plateforme", "langue"}."""
        jeton = str(request.data.get('jeton') or '').strip()
        if not jeton or len(jeton) > 512:
            return Response({'detail': 'Jeton requis.'}, status=status.HTTP_400_BAD_REQUEST)
        if request.method == 'DELETE':
            AppareilPush.objects.filter(jeton=jeton, user_id=request.user.pk).delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        plateforme = str(request.data.get('plateforme') or 'android')[:20]
        langue = 'ar' if str(request.data.get('langue') or '').startswith('ar') else 'fr'
        # Un jeton = un appareil : s'il passe à un autre compte, il le suit.
        _, cree = AppareilPush.objects.update_or_create(
            jeton=jeton, defaults={'user_id': request.user.pk, 'plateforme': plateforme,
                                   'langue': langue, 'projet': 'gp'})
        return Response({'detail': 'Appareil inscrit.'},
                        status=status.HTTP_201_CREATED if cree else status.HTTP_200_OK)

    # Sans session : l'app n'a qu'une session à la fois, mais reste inscrite
    # auprès de chaque établissement où l'on s'est connecté. Le jeton FCM fait
    # foi ; réponse 204 dans tous les cas (rien à apprendre en interrogeant).
    @action(detail=False, methods=['post'], url_path='appareils/oublier',
            permission_classes=[AllowAny], authentication_classes=[],
            throttle_classes=[AnonRateThrottle])
    def oublier_appareil(self, request):
        """Session expirée, « Ce n'est pas moi » : ce téléphone ne reçoit plus
        les notifications de l'IPGEI. Corps : {"jeton"}."""
        jeton = str(request.data.get('jeton') or '').strip()
        if not jeton or len(jeton) > 512:
            return Response({'detail': 'Jeton requis.'}, status=status.HTTP_400_BAD_REQUEST)
        AppareilPush.objects.filter(jeton=jeton).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['post'], url_path='appareils/remplacer',
            permission_classes=[AllowAny], authentication_classes=[],
            throttle_classes=[AnonRateThrottle])
    def remplacer_jeton(self, request):
        """Firebase a renouvelé le jeton du téléphone : l'inscription suit le
        nouveau, sans session. Corps : {"ancien", "nouveau"}."""
        ancien = str(request.data.get('ancien') or '').strip()
        nouveau = str(request.data.get('nouveau') or '').strip()
        if not ancien or not nouveau or len(ancien) > 512 or len(nouveau) > 512:
            return Response({'detail': 'Jetons requis.'}, status=status.HTTP_400_BAD_REQUEST)
        appareil = AppareilPush.objects.filter(jeton=ancien).first()
        if appareil is not None and ancien != nouveau:
            AppareilPush.objects.filter(jeton=nouveau).exclude(pk=appareil.pk).delete()
            appareil.jeton = nouveau
            appareil.save(update_fields=['jeton'])
        return Response(status=status.HTTP_204_NO_CONTENT)
