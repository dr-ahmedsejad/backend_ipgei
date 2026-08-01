# import logging
# from rest_framework import viewsets, status
# from rest_framework.decorators import action
# from rest_framework.response import Response
# from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
# from django_filters.rest_framework import DjangoFilterBackend
# from rest_framework.filters import SearchFilter, OrderingFilter
# from core.permissions import RBACPermission
# from core.mixins import AuditMixin, SelectAllMixin
# from core.pagination import StandardPagination
# from .models import Prof
# from .serializers import ProfSerializer, ProfListSerializer, ProfStatsSerializer
#
# logger = logging.getLogger('siga')
#
#
# class ProfViewSet(AuditMixin, SelectAllMixin, viewsets.ModelViewSet):
#     queryset           = Prof.objects.select_related('banque').all()
#     permission_classes = [RBACPermission]
#     required_module    = 'profs'
#     parser_classes     = [MultiPartParser, FormParser, JSONParser]
#     filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
#     filterset_fields   = ['type', 'genre', 'grade', 'niveau_de_diplome', 'banque']
#     search_fields      = ['nom', 'email', 'NNI']
#     ordering_fields    = ['nom', 'type', 'grade']
#     pagination_class   = StandardPagination
#
#     def get_serializer_class(self):
#         if self.action in ('list', 'all'):
#             return ProfListSerializer
#         return ProfSerializer
#
#     @action(detail=False, methods=['get'], url_path='stats')
#     def stats(self, request):
#         qs = self.get_queryset()
#         data = {
#             'total':        qs.count(),
#             'vacataires':   qs.filter(type='vacataire').count(),
#             'permanents':   qs.filter(type='permanent').count(),
#             'contractuels': qs.filter(type='contractuel').count(),
#             'hommes':       qs.filter(genre='M').count(),
#             'femmes':       qs.filter(genre='F').count(),
#         }
#         return Response(data)

import logging
from django.db.models import Count
from django.contrib.auth import get_user_model
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter

from core.permissions import RBACPermission
from core.mixins import AuditMixin, SelectAllMixin
from core.pagination import StandardPagination
from .models import Prof, ProfTypeHistory
from .serializers import ProfSerializer, ProfListSerializer, ProfTypeHistorySerializer

User = get_user_model()

logger = logging.getLogger('siga')


class ProfViewSet(AuditMixin, SelectAllMixin, viewsets.ModelViewSet):
    queryset = Prof.objects.select_related('banque').all()
    permission_classes = [RBACPermission]
    required_module = 'profs'
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = {
        'type':              ['exact', 'in'],
        'genre':             ['exact'],
        'grade':             ['exact'],
        'niveau_de_diplome': ['exact'],
        'banque':            ['exact'],
        'actif':             ['exact'],
    }
    # `NNI` est un entier : `icontains` ne s'y applique pas, une recherche par
    # NNI ne remontait donc rien alors que le champ de recherche l'annonce. On
    # expose une projection texte du NNI et on cherche dessus.
    search_fields = ['nom', 'email', 'nni_texte']
    ordering_fields = ['nom', 'type', 'grade']
    pagination_class = StandardPagination

    def get_queryset(self):
        # Projection texte du NNI, indispensable pour que SearchFilter (qui
        # applique `icontains`) puisse porter dessus.
        from django.db.models import CharField
        from django.db.models.functions import Cast
        return super().get_queryset().annotate(
            nni_texte=Cast('NNI', output_field=CharField()),
        )

    def get_serializer_class(self):
        if self.action in ('list', 'all'):
            return ProfListSerializer
        return ProfSerializer

    def destroy(self, request, *args, **kwargs):
        """
        Suppression protégée : un prof référencé par des vacations / surveillances
        / charges (données de paie en PROTECT) ne peut pas être supprimé. On
        renvoie un 409 explicite invitant à ARCHIVER (actif=False) plutôt qu'à
        forcer la suppression — l'historique de paie doit être conservé.
        """
        from django.db.models import ProtectedError
        instance = self.get_object()
        try:
            self.perform_destroy(instance)
        except ProtectedError as exc:
            nb = len(exc.protected_objects)
            return Response(
                {
                    'status': 409,
                    'error': (
                        f"Suppression impossible : ce professeur est référencé par "
                        f"{nb} enregistrement(s) (vacations, surveillances ou charges). "
                        f"Ces données de paie doivent être conservées — "
                        f"archivez le professeur (le rendre inactif) au lieu de le supprimer."
                    ),
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'], url_path='archiver')
    def archiver(self, request, pk=None):
        """Archive un professeur (actif=False) — alternative à la suppression."""
        prof = self.get_object()
        prof.actif = False
        prof.save(update_fields=['actif'])
        return Response({'detail': f'{prof.nom} archivé.', 'actif': prof.actif})

    @action(detail=True, methods=['post'], url_path='restaurer')
    def restaurer(self, request, pk=None):
        """Réactive un professeur archivé (actif=True)."""
        prof = self.get_object()
        prof.actif = True
        prof.save(update_fields=['actif'])
        return Response({'detail': f'{prof.nom} réactivé.', 'actif': prof.actif})

    # La création automatique du compte CustomUser à l'ajout d'un Prof est désormais
    # gérée par le signal post_save dans apps/prof/signals.py — couvre aussi les
    # créations hors REST (shell, fixtures, scripts). Les endpoints ci-dessous
    # restent disponibles pour rattraper les imports en masse (bulk_create ne
    # déclenche pas les signaux Django) et les cas de silent fail.

    @action(detail=False, methods=['post'], url_path='generer-comptes')
    def generer_comptes(self, request):
        """Crée les comptes enseignants pour tous les profs sans user."""
        from .services import creer_compte_pour_prof
        profs = Prof.objects.filter(user__isnull=True)
        created = sum(1 for p in profs if creer_compte_pour_prof(p))
        return Response({'created': created, 'detail': f'{created} compte(s) créé(s).'})

    @action(detail=True, methods=['post'], url_path='generer-compte')
    def generer_compte(self, request, pk=None):
        """Crée le compte enseignant pour un prof spécifique."""
        from .services import creer_compte_pour_prof
        prof = self.get_object()
        if prof.user_id:
            return Response(
                {'detail': 'Ce professeur a déjà un compte.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not prof.telephone:
            return Response(
                {'detail': 'Impossible de créer le compte : numéro de téléphone manquant.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        created = creer_compte_pour_prof(prof)
        if created:
            return Response({'detail': 'Compte créé avec succès.'})
        return Response(
            {'detail': 'Username déjà utilisé (doublon de téléphone).'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    @action(detail=False, methods=['get'], url_path='stats')
    def stats(self, request):
        """Stats professeurs.

        Par defaut : snapshot du statut courant (`Prof.type`).

        Mode historique : passer `?at_date=YYYY-MM-DD` (rapport retroactif)
        OU activer le flag global `USE_PROF_TYPE_HISTORY=true` dans `.env`
        (utilise alors la date du jour comme reference).
        Dans ces cas, les compteurs vacataires/permanents/contractuels viennent
        de prof_type_history a la date specifiee.
        """
        from datetime import datetime as _dt
        from django.utils import timezone
        from apps.prof.services import use_prof_type_history, prof_ids_with_type_at

        qs = self.get_queryset()

        # 1. Grouper les professeurs par niveau_de_diplome et les compter (inchange)
        diplomes_qs = qs.values('niveau_de_diplome').annotate(count=Count('id'))
        diplomes_labels = [
            d['niveau_de_diplome'] if d.get('niveau_de_diplome') else 'Non renseigné'
            for d in diplomes_qs
        ]
        diplomes_data = [d['count'] for d in diplomes_qs]

        # 2. Compteurs par type — historique ou snapshot ?
        at_date_param = request.query_params.get('at_date')
        if at_date_param:
            try:
                at_date = _dt.strptime(at_date_param, '%Y-%m-%d').date()
            except ValueError:
                return Response({'error': 'at_date invalide (attendu YYYY-MM-DD)'}, status=400)
            mode = 'history'
        elif use_prof_type_history():
            at_date = timezone.now().date()
            mode = 'history'
        else:
            at_date = None
            mode = 'current'

        if mode == 'history':
            n_vac  = len(prof_ids_with_type_at('vacataire',   at_date))
            n_per  = len(prof_ids_with_type_at('permanent',   at_date))
            n_con  = len(prof_ids_with_type_at('contractuel', at_date))
        else:
            n_vac = qs.filter(type='vacataire').count()
            n_per = qs.filter(type='permanent').count()
            n_con = qs.filter(type='contractuel').count()

        data = {
            'total': qs.count(),
            'vacataires': n_vac,
            'permanents': n_per,
            'contractuels': n_con,
            'hommes': qs.filter(genre='M').count(),
            'femmes': qs.filter(genre='F').count(),
            'diplomes_labels': diplomes_labels,
            'diplomes_data': diplomes_data,
            # Meta utile pour le front
            'mode': mode,
            'at_date': str(at_date) if at_date else None,
        }
        return Response(data)


class ProfTypeHistoryViewSet(AuditMixin, viewsets.ModelViewSet):
    """CRUD sur l'historique des statuts d'un prof. Consommé par
    `app/dashboard/profs/historique-statut/page.tsx` côté front.

    Le front filtre généralement par ?prof=<id>. Auto-fill `cree_par` au create.
    """
    queryset           = ProfTypeHistory.objects.select_related('prof').all()
    serializer_class   = ProfTypeHistorySerializer
    permission_classes = [RBACPermission]
    required_module    = 'profs'
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['prof', 'type']
    ordering_fields    = ['date_debut', 'prof_id']
    ordering           = ['prof_id', '-date_debut']
    pagination_class   = StandardPagination

    def perform_create(self, serializer):
        # Auto-fill cree_par avec le username du user authentifie
        user = self.request.user
        username = getattr(user, 'username', '') if user and user.is_authenticated else ''
        serializer.save(cree_par=username)

    @action(detail=False, methods=['get'], url_path='feature-status',
            permission_classes=[IsAuthenticated])
    def feature_status(self, request):
        """Retourne l'etat du flag USE_PROF_TYPE_HISTORY (lecture seule).

        Permet au frontend d'afficher un indicateur visuel "mode historique actif"
        sur les pages concernees (statistiques, payement, etc.).
        Endpoint public (pas de RBAC) car juste un booleen meta.
        """
        from apps.prof.services import use_prof_type_history
        return Response({
            'enabled': use_prof_type_history(),
            'description': (
                "Mode historique actif : les rapports de vacation utilisent "
                "prof_type_history pour reconstituer le statut de chaque prof "
                "AU MOIS calcule (au lieu du statut courant)."
            ) if use_prof_type_history() else (
                "Mode legacy : les rapports utilisent le statut courant Prof.type."
            ),
        })