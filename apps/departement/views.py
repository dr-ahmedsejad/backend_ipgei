from django.core.exceptions import ObjectDoesNotExist
from rest_framework import status, viewsets
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from core.permissions import RBACPermission
from core.mixins import AuditMixin, SelectAllMixin
from core.pagination import StandardPagination
from .models import Departement
from .serializers import DepartementSerializer

class DepartementViewSet(AuditMixin, SelectAllMixin, viewsets.ModelViewSet):
    queryset           = Departement.objects.select_related('niveau', 'filiere', 'institution').all()
    serializer_class   = DepartementSerializer
    permission_classes = [RBACPermission]
    required_module    = 'departements'
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields   = ['annee_universitaire', 'niveau', 'filiere', 'institution', 'is_container']
    search_fields      = ['nom', 'code']
    ordering_fields    = ['nom']
    pagination_class   = StandardPagination

    def _refus_si_miroir(self, instance):
        """
        Interdit de modifier ici un departement qui EST une classe IPGEI.

        `ipgei.Classe` et `ipgei.SousGroupeTP` heritent de ce modele : c'est la
        meme ligne, mais plusieurs champs (`nom`, `code`, `groupe`, `niveau`,
        `filiere`) y sont derives a chaque enregistrement cote IPGEI. Une
        retouche faite ici serait donc ecrasee, et une suppression emporterait
        la classe et ses seances. Mieux vaut refuser franchement et indiquer ou
        agir.
        """
        for attribut in ('classe_ipgei', 'sous_groupe_ipgei'):
            try:
                enfant = getattr(instance, attribut, None)
            except ObjectDoesNotExist:
                enfant = None      # MTI : l'acces leve si l'enfant n'existe pas
            if enfant is None:
                continue
            return Response(
                {'error': (
                    f"« {instance.nom} » est une classe IPGEI et se gere depuis "
                    "« Classes & sous-groupes ». Les champs de planification y "
                    "sont calcules : une modification faite ici serait ecrasee."
                )},
                status=status.HTTP_409_CONFLICT,
            )
        return None

    def update(self, request, *args, **kwargs):
        refus = self._refus_si_miroir(self.get_object())
        return refus or super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        refus = self._refus_si_miroir(self.get_object())
        return refus or super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        refus = self._refus_si_miroir(self.get_object())
        return refus or super().destroy(request, *args, **kwargs)

    def get_permissions(self):
        # Lecture libre pour tout authentifie : Departement = donnee de reference
        # consommee par de nombreux ecrans (rapports absences, vacations, suivi...).
        # Ecritures (create/update/destroy) restent gatees par RBAC departements.
        if self.action in ('list', 'retrieve', 'all'):
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_queryset(self):
        """Filtre optionnel EDT-scope : si ?edt_scope=1, ne renvoie que les
        groupes que ce user peut gerer (managed_departements). Admin bypass.

        Cible : les selects/chips des pages EDT (emplois, suivi, vacation)
        qui ne doivent montrer que les groupes attribues. Les pages
        non-EDT (admissions, statistiques, parametres) n'envoient pas
        ce parametre et conservent la liste complete.
        """
        qs = super().get_queryset()
        if self.request.query_params.get('edt_scope') in ('1', 'true', 'True'):
            u = self.request.user
            # Superuser fallback : voit tout sans configurer la matrice.
            # Tout autre user (admin role inclus) : filtre sur managed_departements.
            if not (u.is_authenticated and u.is_superuser):
                managed = u.managed_departements.values_list('id', flat=True)
                qs = qs.filter(pk__in=list(managed))
        return qs
