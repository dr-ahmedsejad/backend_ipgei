"""Mixins réutilisables pour les ViewSets SIGA."""
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import Q
import logging

logger = logging.getLogger('siga')


class AuditMixin:
    """Log les créations/modifications/suppressions."""

    def perform_create(self, serializer):
        obj = serializer.save()
        logger.info('[%s] CREATE %s#%s by user=%s',
                    self.__class__.__name__, obj.__class__.__name__,
                    obj.pk, self.request.user.username)

    def perform_update(self, serializer):
        obj = serializer.save()
        logger.info('[%s] UPDATE %s#%s by user=%s',
                    self.__class__.__name__, obj.__class__.__name__,
                    obj.pk, self.request.user.username)

    def perform_destroy(self, instance):
        logger.info('[%s] DELETE %s#%s by user=%s',
                    self.__class__.__name__, instance.__class__.__name__,
                    instance.pk, self.request.user.username)
        instance.delete()


class SelectAllMixin:
    """
    Ajoute GET /resource/all/ → liste complète sans pagination
    (utile pour les <select> dans le frontend).
    """

    @action(detail=False, methods=['get'], url_path='all', pagination_class=None)
    def all(self, request):
        qs = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data)


class InstitutionScopedMixin:
    """
    Restreint le queryset à l'institution principale (mono-institution).

    Section 1bis institution_V1 — phase 1 : tous les utilisateurs voient l'institution
    principale uniquement. Phase 2 (future) : multi-institution via request.user.institution.

    Usage simple — modèle avec FK directe `institution` :
        class MyViewSet(InstitutionScopedMixin, viewsets.ModelViewSet):
            queryset = MyModel.objects.all()

    Usage chaîne FK — pour les modèles du Groupe 3 (dérivation) :
        class LigneViewSet(InstitutionScopedMixin, viewsets.ModelViewSet):
            queryset = Ligne.objects.all()
            institution_filter_field = 'pv__institution'
    """
    institution_filter_field = 'institution'

    def get_queryset(self):
        from apps.parametres.models import Institution
        qs = super().get_queryset()
        principale = Institution.objects.filter(est_principale=True).first()
        if not principale:
            return qs.none()
        return qs.filter(**{self.institution_filter_field: principale})


class DepartementScopedMixin:
    """
    Restreint le queryset aux departements presents dans
    `request.user.managed_departements`. Bypass admin/superuser.

    Usage simple — modele avec FK directe `departement` :
        class EmploisViewSet(DepartementScopedMixin, viewsets.ModelViewSet):
            queryset = Emplois.objects.all()

    Usage M2M (Vacation) :
        class VacationViewSet(DepartementScopedMixin, viewsets.ModelViewSet):
            queryset = Vacation.objects.all()
            departement_filter_field = 'departements'   # M2M
            departement_filter_lookup = 'in'            # plutot que exact

    Notes :
      - Si le user n'a aucun dept attribue, le queryset retourne `.none()` :
        il ne voit RIEN. Cohrent avec l'approche "remplace" (M2M est l'unique gate).
      - GET list/retrieve sont scopes via get_queryset.
      - GET/POST sur actions custom doivent appeler `self.user_dept_ids` (helper)
        et filtrer manuellement.
    """
    departement_filter_field  = 'departement'
    departement_filter_lookup = 'in'
    # Si True, les lignes SANS departement (FK null / M2M vide) restent visibles
    # pour les utilisateurs scopes. Necessaire pour le travail transversal non
    # rattache a un groupe (ex. Encadrement : une vacation d'encadrement n'a pas
    # de departement). Sinon `departements__in=[...]` ne matche jamais une ligne
    # a zero departement, et elle devient invisible pour tout non-superuser.
    departement_scope_include_null = False

    def user_dept_ids(self):
        """Renvoie les ids des departements geres par le user courant.
        Retourne None pour superuser uniquement (filet de securite en cas
        de matrice mal configuree). L'admin role est traite comme tout
        utilisateur : il doit avoir des managed_departements explicites,
        configures via /parametres/permissions-edt."""
        u = self.request.user
        if not u.is_authenticated:
            return []
        if u.is_superuser:
            return None   # sentinelle : pas de scoping (cas critique uniquement)
        return list(u.managed_departements.values_list('id', flat=True))

    def get_queryset(self):
        qs = super().get_queryset()
        ids = self.user_dept_ids()
        if ids is None:
            return qs   # superuser fallback
        if not ids:
            return qs.none()
        lookup = f'{self.departement_filter_field}__{self.departement_filter_lookup}'
        cond = Q(**{lookup: ids})
        if self.departement_scope_include_null:
            # Inclure aussi les lignes sans departement rattache
            cond |= Q(**{f'{self.departement_filter_field}__isnull': True})
        return qs.filter(cond).distinct()
