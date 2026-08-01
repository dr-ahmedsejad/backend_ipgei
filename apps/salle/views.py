from rest_framework import viewsets
from rest_framework.filters import SearchFilter, OrderingFilter
from core.permissions import RBACPermission
from core.mixins import AuditMixin, SelectAllMixin
from core.pagination import StandardPagination
from .models import Salle
from .serializers import SalleSerializer

class SalleViewSet(AuditMixin, SelectAllMixin, viewsets.ModelViewSet):
    queryset           = Salle.objects.all()
    serializer_class   = SalleSerializer
    permission_classes = [RBACPermission]
    required_module    = 'salles'
    filter_backends    = [SearchFilter, OrderingFilter]
    search_fields      = ['nom']
    ordering_fields    = ['nom', 'capacite']
    pagination_class   = StandardPagination
