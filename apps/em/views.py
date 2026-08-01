from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from core.permissions import RBACPermission
from core.mixins import AuditMixin, SelectAllMixin
from core.pagination import StandardPagination
from .models import EM
from .serializers import EMSerializer


def _scope_groupe_q(filiere_id, niveau_id):
    """
    Appartenance DÉRIVÉE d'un EM à un groupe (filiere_id, niveau_id) — jamais
    stockée. Un EM appartient au groupe SI sa filière (stable ou via module LMD)
    = celle du groupe ET le niveau de son semestre = celui du groupe.
    """
    return (Q(filiere_id=filiere_id) | Q(module_lmd__filiere_id=filiere_id)) \
        & Q(semestre__niveau_semestre_id=niveau_id)


class EMViewSet(AuditMixin, SelectAllMixin, viewsets.ModelViewSet):
    queryset = EM.objects.select_related(
        'filiere',
        'departement', 'departement__filiere',
        'semestre', 'semestre__niveau_semestre',
        'module_lmd', 'module_lmd__filiere', 'institution',
    ).distinct()
    serializer_class   = EMSerializer
    permission_classes = [RBACPermission]
    required_module    = 'em'

    def get_permissions(self):
        # Lecture libre pour tout authentifie : EM = catalogue de matieres
        # consomme par les notes, deliberations, suivi, emplois... Sans cela,
        # un user qui a 'eval_saisie:voir' ne peut meme pas afficher la liste
        # des EMs pour saisir/consulter des notes.
        # Ecritures (create/update/destroy) restent gatees par RBAC em.
        if self.action in ('list', 'retrieve', 'all'):
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_queryset(self):
        """Filtre optionnel ?edt_scope=1 : ne renvoie que les EMs rattaches
        a un departement present dans `request.user.managed_departements`.

        Cible : selects/autocompletes des pages EDT (vacations, suivi)
        ou seuls les EMs des groupes geres doivent etre proposes.
        Superuser bypass (filet de securite uniquement). Les pages non-EDT
        (notes, deliberations) n'envoient pas ce parametre et conservent
        l'acces au catalogue complet.
        """
        qs = super().get_queryset()
        if self.request.query_params.get('edt_scope') in ('1', 'true', 'True'):
            u = self.request.user
            if not (u.is_authenticated and u.is_superuser):
                # Appartenance DÉRIVÉE (filière + niveau) des groupes gérés, et non
                # plus par FK departement (désormais vestigial/NULL). Ainsi un EM
                # stable est proposé pour les groupes gérés de sa filière/niveau,
                # toutes années confondues, sans re-liaison annuelle.
                pairs = u.managed_departements.values_list('filiere_id', 'niveau_id')
                scope = Q()
                any_valid = False
                for fil_id, niv_id in pairs:
                    if fil_id and niv_id:
                        scope |= _scope_groupe_q(fil_id, niv_id)
                        any_valid = True
                qs = qs.filter(scope) if any_valid else qs.none()

        # Filtre "EMs ayant des inscrits pour (filiere, annee)" : on remplace la
        # double jointure DjangoFilter (lente + sémantiquement lâche : 2 joins
        # distincts sur inscriptions_elements) par UNE sous-requête sur le MÊME
        # InscriptionElement (rapide, pas de duplication, filiere ET annee
        # portées par la même inscription).
        p = self.request.query_params
        fil = p.get('inscriptions_elements__inscription_ped__inscription_admin__filiere')
        ann = p.get('inscriptions_elements__inscription_ped__inscription_admin__annee_univ')
        if fil or ann:
            from apps.inscriptions.models import InscriptionElement
            ie = InscriptionElement.objects.all()
            if fil:
                ie = ie.filter(inscription_ped__inscription_admin__filiere_id=fil)
            if ann:
                ie = ie.filter(inscription_ped__inscription_admin__annee_univ_id=ann)
            qs = qs.filter(id__in=ie.values('em_id'))
        return qs
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields   = {
        # Identité STABLE : filtrer les EM par filière (partagés par tous les groupes/années).
        'filiere':                            ['exact'],
        'departement':                        ['exact'],
        'departement__filiere':               ['exact'],
        'departement__annee_universitaire':   ['exact'],
        'semestre':                           ['exact'],
        'semestre__type_semestre':            ['exact'],
        # Niveau via le semestre STABLE de l'EM (dérivation groupe = filière + ce niveau).
        'semestre__niveau_semestre':          ['exact'],
        # (filtre inscriptions_elements par filiere+annee géré en sous-requête
        #  dans get_queryset — plus rapide que la double jointure DjangoFilter)
        'module_lmd':                         ['exact', 'isnull'],
        # Section 1ter institution_V1 : filtre par filiere via module_lmd (chaine stable d'annee en annee)
        'module_lmd__filiere':                ['exact'],
        # Phase EM↔Module : filtre par niveau via module_lmd → semestre → niveau_semestre
        'module_lmd__semestre__niveau_semestre': ['exact'],
        # Scoping multi-institution
        'institution':                        ['exact'],
    }
    search_fields      = ['code_em', 'intitule']
    ordering_fields    = ['code_em', 'intitule']
    pagination_class   = StandardPagination

    # ── Override SelectAllMixin.all() pour accepter ?filiere&niveau alias ────
    # Le frontend /emplois/gerer appelle GET /api/v1/ems/all/?filiere=X&niveau=Y
    # qui retourne les EMs dont le Module est dans la filière X au niveau Y.
    # Permet aussi le fallback sur ?departement=N (legacy, EMs pas encore lies a un module).
    @action(detail=False, methods=['get'], url_path='all', pagination_class=None)
    def all(self, request):
        qs = self.get_queryset()

        filiere = request.query_params.get('filiere')
        niveau  = request.query_params.get('niveau')
        if filiere and niveau:
            # Dérivation STABLE : EM de la filière (stable OU via module) au niveau du
            # semestre — indépendante du groupe/année. Un EM créé une fois apparaît
            # pour tous les groupes de cette filière/niveau, toutes années.
            qs = qs.filter(_scope_groupe_q(filiere, niveau))

        # Continue d'appliquer filterset_fields (departement, semestre, etc.)
        qs = self.filter_queryset(qs)
        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data)
