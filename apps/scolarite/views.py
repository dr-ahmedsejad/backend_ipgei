from rest_framework import status
from rest_framework.viewsets import ModelViewSet, GenericViewSet
from rest_framework.mixins import RetrieveModelMixin, UpdateModelMixin
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter

from core.permissions import RBACPermission
from core.pagination import StandardPagination
from .models import DepartementAcademique, Filiere, ParametresPonderation
from .serializers import (
    DepartementAcademiqueSerializer,
    FiliereSerializer,
    FiliereListSerializer,
    ParametresPonderationSerializer,
)


class DepartementAcademiqueViewSet(ModelViewSet):
    """
    CRUD Départements académiques (INFO, GEST, MATH…).
    Ne pas confondre avec 'apps/departement' qui représente les classes pédagogiques.
    GET /api/v1/scolarite/departements-academiques/          → liste
    GET /api/v1/scolarite/departements-academiques/{id}/    → détail + filières rattachées
    POST/PUT/PATCH/DELETE                                   → gestion (scolarite, admin)
    """
    serializer_class   = DepartementAcademiqueSerializer
    permission_classes = [RBACPermission]
    required_module    = 'scolarite'
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields   = ['actif']
    search_fields      = ['code', 'intitule_fr', 'intitule_ar']
    ordering_fields    = ['code', 'intitule_fr']
    ordering           = ['code']
    pagination_class   = StandardPagination

    def get_queryset(self):
        return DepartementAcademique.objects.prefetch_related('filieres').all()

    @action(detail=True, methods=['get'], url_path='filieres')
    def filieres(self, request, pk=None):
        """Liste les filières rattachées à ce département académique."""
        dept = self.get_object()
        qs = dept.filieres.all().order_by('code')
        return Response(FiliereListSerializer(qs, many=True).data)


class FiliereViewSet(ModelViewSet):
    """
    CRUD filières académiques.
    GET /api/v1/scolarite/filieres/           → liste paginée (public — AllowAny pour dropdown preinscription)
    GET /api/v1/scolarite/filieres/select/    → liste légère (id, code, intitule) — public
    GET /api/v1/scolarite/filieres/{id}/      → détail
    POST/PUT/PATCH/DELETE                     → gestion (scolarite, admin)
    """
    serializer_class = FiliereSerializer
    permission_classes = [RBACPermission]
    required_module  = 'scolarite'
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['est_active', 'type_diplome', 'institution', 'departement_academique']
    search_fields    = ['code', 'intitule_fr', 'intitule_ar']
    ordering_fields  = ['code', 'intitule_fr']
    ordering         = ['code']
    pagination_class = StandardPagination

    def get_queryset(self):
        return Filiere.objects.select_related(
            'responsable', 'institution', 'departement_academique',
        ).all()

    def get_permissions(self):
        if self.action in ('list', 'retrieve', 'select'):
            return [AllowAny()]
        return [RBACPermission()]

    @action(detail=False, methods=['get'], url_path='select')
    def select(self, request):
        """Liste légère pour les dropdowns : filtre optionnel ?est_active=true."""
        qs = self.get_queryset()
        est_active = request.query_params.get('est_active')
        if est_active is not None:
            qs = qs.filter(est_active=est_active.lower() == 'true')
        return Response(FiliereListSerializer(qs, many=True).data)


class ParametresPonderationViewSet(RetrieveModelMixin, UpdateModelMixin, GenericViewSet):
    """
    Singleton des paramètres de pondération institutionnels.
    GET  /api/v1/scolarite/parametres-ponderation/1/   → lecture
    PUT  /api/v1/scolarite/parametres-ponderation/1/   → mise à jour complète
    PATCH /api/v1/scolarite/parametres-ponderation/1/  → mise à jour partielle
    """
    serializer_class   = ParametresPonderationSerializer
    permission_classes = [RBACPermission]
    required_module    = 'scolarite'

    def get_object(self):
        obj = ParametresPonderation.get()
        self.check_object_permissions(self.request, obj)
        return obj

    def perform_update(self, serializer):
        # Capture les anciens coefficients AVANT save (la pondération change la
        # formule de note pour tous les étudiants → action sensible à tracer).
        inst = serializer.instance
        old = {'coeff_cc': inst.coeff_cc, 'coeff_exam': inst.coeff_exam, 'coeff_tp': inst.coeff_tp}
        serializer.save()
        new = serializer.instance
        try:
            from core.audit_helpers import write_audit
            write_audit(
                action='UPDATE', model_name='ParametresPonderation', object_id=str(new.pk),
                changes={
                    'coeff_cc':   {'old': old['coeff_cc'],   'new': new.coeff_cc},
                    'coeff_exam': {'old': old['coeff_exam'], 'new': new.coeff_exam},
                    'coeff_tp':   {'old': old['coeff_tp'],   'new': new.coeff_tp},
                },
                label='Modification de la pondération des notes (CC/EXAM/TP)',
                keep_forever=True,
            )
        except Exception:
            import logging
            logging.getLogger('siga').warning('Audit pondération échoué', exc_info=True)

    @action(detail=False, methods=['get'], url_path='current')
    def current(self, request):
        """GET /api/v1/scolarite/parametres-ponderation/current/ — raccourci sans connaître l'id."""
        obj = ParametresPonderation.get()
        return Response(ParametresPonderationSerializer(obj).data)
