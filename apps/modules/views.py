from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter

from core.permissions import RBACPermission
from core.pagination import StandardPagination
from .models import Module, ElementModule
from .serializers import ModuleSerializer, ModuleListSerializer, ElementModuleSerializer


class ModuleViewSet(viewsets.ModelViewSet):
    """
    CRUD Modules LMD.
    GET  /api/v1/modules/                  → liste paginée
    GET  /api/v1/modules/{id}/             → détail avec éléments
    POST /api/v1/modules/                  → créer
    PATCH/PUT /api/v1/modules/{id}/        → modifier
    DELETE    /api/v1/modules/{id}/        → supprimer
    GET  /api/v1/modules/{id}/elements/   → éléments du module
    """
    permission_classes = [RBACPermission]
    required_module    = 'scolarite'
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields   = {
        'filiere':     ['exact'],
        'semestre':    ['exact'],
        'actif':       ['exact'],
        'institution': ['exact'],
    }
    search_fields  = ['code', 'intitule_fr', 'intitule_ar']
    ordering_fields = ['code', 'intitule_fr', 'credits']
    ordering        = ['filiere', 'semestre', 'code']
    pagination_class = StandardPagination

    def get_queryset(self):
        return Module.objects.select_related(
            'filiere', 'semestre', 'institution',
        ).prefetch_related(
            'elements',
            'ems_planification',
            'ems_planification__departement',
        ).order_by('filiere', 'semestre', 'code')

    def get_serializer_class(self):
        if self.action == 'list':
            return ModuleListSerializer
        return ModuleSerializer

    @action(detail=True, methods=['get'], url_path='elements')
    def elements(self, request, pk=None):
        """Liste les éléments d'un module donné."""
        module = self.get_object()
        qs = module.elements.all().order_by('ordre', 'code')
        return Response(ElementModuleSerializer(qs, many=True).data)

    @action(detail=True, methods=['post'], url_path='recalculer-credits')
    def recalculer_credits(self, request, pk=None):
        """Met à jour les crédits du module selon la somme de ses EMs planification.
        Fallback sur les ElementModule LMD si aucun EM planification n'est rattache.
        Retourne le module complet (ModuleSerializer) avec ems_planification, pour
        que le frontend conserve la liste apres setQueryData."""
        module = self.get_object()
        # Somme des credits des EMs planification (modele legacy EM avec hours)
        total_ems = sum((e.credits or 0) for e in module.ems_planification.all())
        if total_ems > 0:
            total = total_ems
        else:
            # Fallback : ElementModule LMD (modules_element)
            total = sum(e.credits for e in module.elements.all())
        module.credits = total
        module.save(update_fields=['credits'])
        return Response(ModuleSerializer(module).data)


class ElementModuleViewSet(viewsets.ModelViewSet):
    """
    CRUD Éléments de module LMD.
    GET  /api/v1/elements/            → liste (filtres : ?module=, ?semestre=)
    POST /api/v1/elements/            → créer
    PATCH/PUT /api/v1/elements/{id}/  → modifier
    DELETE    /api/v1/elements/{id}/  → supprimer
    """
    serializer_class   = ElementModuleSerializer
    permission_classes = [RBACPermission]
    required_module    = 'scolarite'
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields   = {
        'module':          ['exact'],
        'module__filiere': ['exact'],
        'module__semestre': ['exact'],
    }
    search_fields  = ['code', 'intitule_fr', 'intitule_ar']
    ordering_fields = ['code', 'intitule_fr', 'ordre']
    ordering        = ['module', 'ordre', 'code']
    pagination_class = StandardPagination

    def get_queryset(self):
        return ElementModule.objects.select_related(
            'module', 'module__filiere', 'module__semestre',
        ).all()
