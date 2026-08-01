from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from core.permissions import RBACPermission
from core.mixins import AuditMixin, SelectAllMixin
from core.pagination import StandardPagination
from .models import Banque
from .serializers import BanqueSerializer

class BanqueViewSet(AuditMixin, SelectAllMixin, viewsets.ModelViewSet):
    queryset           = Banque.objects.all()
    serializer_class   = BanqueSerializer
    permission_classes = [RBACPermission]
    required_module    = 'banques'
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    search_fields      = ['nom', 'description']
    ordering_fields    = ['nom']
    pagination_class   = StandardPagination

    def get_permissions(self):
        # Lecture libre auth — UNIQUEMENT la liste (list/all) : consommee par
        # /profs/ajouter (select banque obligatoire). Detail (retrieve), creation,
        # modification, suppression restent gatees par RBAC 'banques'.
        if self.action in ('list', 'all'):
            return [IsAuthenticated()]
        return super().get_permissions()
