from django.utils import timezone
from django.db import models
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter

from core.permissions import RBACPermission, IsEtudiant
from .models import Reclamation, PeriodeReclamation
from .serializers import (
    ReclamationSerializer, ReclamationCreateSerializer, ReclamationTraiterSerializer,
    PeriodeReclamationSerializer,
)


class ReclamationAdminViewSet(viewsets.ModelViewSet):
    """
    Gestion des réclamations côté staff.
    Accessible aux rôles SCOLARITE : admin, DE, scolarite, responsable_filiere.
    """
    queryset = Reclamation.objects.select_related(
        'etudiant', 'traitee_par',
        'presence__suivi__em',
        'inscription_element__em',
        'session_evaluation',
    ).all()
    serializer_class   = ReclamationSerializer
    permission_classes = [RBACPermission]
    required_module    = 'reclamations'
    parser_classes     = [MultiPartParser, FormParser, JSONParser]
    filter_backends    = [DjangoFilterBackend, SearchFilter]
    filterset_fields   = ['statut', 'type_reclamation']
    search_fields      = ['etudiant__matricule', 'etudiant__nom', 'motif']

    def get_permissions(self):
        from rest_framework.permissions import IsAuthenticated
        if self.action in ('pour_enseignant', 'traiter'):
            return [IsAuthenticated()]
        return super().get_permissions()

    @action(detail=False, methods=['get'], url_path='pour-enseignant')
    def pour_enseignant(self, request):
        """
        GET /api/v1/reclamations/pour-enseignant/
        Retourne les réclamations des EMs enseignés par le prof connecté.
        """
        if getattr(request.user, 'role', None) != 'enseignant':
            return Response({'detail': 'Réservé aux enseignants.'}, status=403)
        try:
            prof = request.user.prof_profile
        except Exception:
            return Response({'detail': 'Profil enseignant introuvable.'}, status=404)

        annee = request.query_params.get('annee_universitaire')
        if not annee:
            try:
                annee = request.user.contexte.annee_universitaire
            except Exception:
                pass

        # EMs du prof (via SuiviePointage)
        from apps.suivi.models import SuiviePointage
        qs_suivi = SuiviePointage.objects.filter(prof=prof)
        if annee:
            qs_suivi = qs_suivi.filter(annee_universitaire=annee)
        em_ids = list(qs_suivi.values_list('em_id', flat=True).distinct())

        qs = Reclamation.objects.select_related(
            'etudiant', 'traitee_par',
            'presence__suivi__em',
            'inscription_element__em',
        ).filter(
            models.Q(inscription_element__em__in=em_ids) |
            models.Q(presence__suivi__em__in=em_ids)
        ).distinct().order_by('-date_soumission')

        statut = request.query_params.get('statut')
        if statut:
            qs = qs.filter(statut=statut)

        return Response(ReclamationSerializer(qs, many=True).data)

    @action(detail=True, methods=['post'], url_path='traiter')
    def traiter(self, request, pk=None):
        """POST /api/v1/reclamations/{id}/traiter/ — change statut + ajoute réponse."""
        reclamation = self.get_object()

        # Vérifier qu'un enseignant ne traite que ses propres EMs
        if getattr(request.user, 'role', None) == 'enseignant':
            try:
                prof = request.user.prof_profile
                from apps.suivi.models import SuiviePointage
                em_ids = list(SuiviePointage.objects.filter(prof=prof).values_list('em_id', flat=True).distinct())
                em_ok = (
                    (reclamation.inscription_element and reclamation.inscription_element.em_id in em_ids) or
                    (reclamation.presence and reclamation.presence.suivi.em_id in em_ids)
                )
                if not em_ok:
                    return Response({'detail': 'Accès refusé.'}, status=403)
            except Exception:
                return Response({'detail': 'Profil enseignant introuvable.'}, status=404)

        serializer = ReclamationTraiterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        reclamation.statut          = serializer.validated_data['statut']
        reclamation.reponse         = serializer.validated_data.get('reponse', '')
        reclamation.traitee_par     = request.user
        reclamation.date_traitement = timezone.now()
        reclamation.save()

        return Response(ReclamationSerializer(reclamation).data)


class PeriodeReclamationViewSet(viewsets.ModelViewSet):
    """
    Gestion des periodes de reclamation (cote SG).
    Permet a l administration d ouvrir/fermer manuellement des fenetres
    pendant lesquelles les etudiants peuvent reclamer leurs notes.
    """
    queryset = PeriodeReclamation.objects.select_related(
        'annee_univ', 'institution', 'filiere', 'cree_par',
    ).all()
    serializer_class   = PeriodeReclamationSerializer
    permission_classes = [RBACPermission]
    required_module    = 'reclamations'
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['annee_univ', 'type_session', 'type_semestre', 'institution', 'filiere', 'niveau', 'actif']

    def perform_create(self, serializer):
        serializer.save(cree_par=self.request.user)

    @action(detail=True, methods=['post'], url_path='fermer-maintenant')
    def fermer_maintenant(self, request, pk=None):
        """POST /api/v1/reclamations/periodes/{id}/fermer-maintenant/
        Force date_fermeture = now() pour fermer la periode immediatement.
        """
        periode = self.get_object()
        periode.date_fermeture = timezone.now()
        periode.save(update_fields=['date_fermeture', 'date_modification'])
        return Response(PeriodeReclamationSerializer(periode).data)

    @action(detail=True, methods=['post'], url_path='basculer-actif')
    def basculer_actif(self, request, pk=None):
        """POST /api/v1/reclamations/periodes/{id}/basculer-actif/
        Active ou desactive la periode (toggle).
        """
        periode = self.get_object()
        periode.actif = not periode.actif
        periode.save(update_fields=['actif', 'date_modification'])
        return Response(PeriodeReclamationSerializer(periode).data)
