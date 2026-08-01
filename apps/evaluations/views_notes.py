import logging
import os
from datetime import date

from django.conf import settings
from django.http import HttpResponse
from django.template.loader import get_template
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter

logger = logging.getLogger('siga')

from rest_framework.permissions import IsAuthenticated
from core.permissions import RBACPermission
from core.mixins import InstitutionScopedMixin
from .models import (
    SessionEvaluation, Note, ResultatElement,
    ResultatSemestre, PVDeliberation, LigneDeliberation,
    ParametreJury, RachatNote, ResultatModule, MembreJury, ObligationRattrapage,
)
from .serializers import (
    SessionEvaluationSerializer, NoteSerializer, ResultatElementSerializer,
    ResultatSemestreSerializer, PVDeliberationSerializer, LigneDeliberationSerializer,
    ParametreJurySerializer, RachatNoteSerializer,
    ResultatModuleSerializer, MembreJurySerializer, ObligationRattrapageSerializer,
)
from .services.calcul_notes import NoteCalculService
from .services.deliberation_semestre import DeliberationSemestreService
from .services.deliberation_annuelle import DeliberationAnnuelleService, get_deliberation_annuelle_service

from .views_helpers import _resolve_em


class NoteViewSet(InstitutionScopedMixin, viewsets.ModelViewSet):
    institution_filter_field = 'inscription_element__inscription_ped__inscription_admin__institution'
    queryset = Note.objects.select_related(
        'inscription_element', 'session', 'saisie_par',
    ).all()
    serializer_class   = NoteSerializer
    permission_classes = [RBACPermission]
    required_module    = 'eval_saisie'
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['session', 'type_note', 'inscription_element']

    def get_permissions(self):
        # Portail enseignant : la consultation de la feuille et la saisie en
        # masse sont ouvertes aux authentifies, MAIS scopees a l'interieur de
        # l'action (_peut_acceder_em) — un enseignant ne touche QUE ses EMs.
        if self.action in ('feuille', 'saisir_bulk'):
            return [IsAuthenticated()]
        return super().get_permissions()

    @staticmethod
    def _peut_acceder_em(user, em_id) -> bool:
        """Délègue au helper partagé services/note_access (conservé car les actions
        de saisie appellent self._peut_acceder_em)."""
        from .services.note_access import peut_acceder_em
        return peut_acceder_em(user, em_id)

    def create(self, request, *args, **kwargs):
        """
        Upsert : si une note (inscription_element, session, type_note) existe déjà,
        met à jour la valeur au lieu de rejeter avec un 400 unique_together.
        """
        ie_id     = request.data.get('inscription_element')
        sess_id   = request.data.get('session')
        type_note = request.data.get('type_note')
        valeur    = request.data.get('valeur')

        note, created = Note.objects.update_or_create(
            inscription_element_id=ie_id,
            session_id=sess_id,
            type_note=type_note,
            defaults={'valeur': valeur, 'saisie_par': request.user},
        )
        serializer = self.get_serializer(note)
        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def perform_update(self, serializer):
        serializer.save(saisie_par=self.request.user)

    @action(detail=False, methods=['post'], url_path='saisir-anonymat')
    def saisir_anonymat(self, request):
        """Délégué à services/note_saisie."""
        from .services.note_saisie import do_saisir_anonymat
        return do_saisir_anonymat(request)

    @action(detail=False, methods=['post'], url_path='importer',
            parser_classes=[MultiPartParser, FormParser])
    def importer(self, request):
        """Délégué à services/note_saisie."""
        from .services.note_saisie import do_importer
        return do_importer(request)

    @action(detail=False, methods=['get'], url_path='agrege')
    def agrege(self, request):
        """Délégué à services/note_lecture."""
        from .services.note_lecture import build_agrege
        return build_agrege(request)

    @action(detail=False, methods=['get'], url_path='feuille')
    def feuille(self, request):
        """GET ?session=X&em=Y — feuille de saisie (délégué à services/note_lecture)."""
        from .services.note_lecture import build_feuille
        return build_feuille(request)

    @action(detail=False, methods=['post'], url_path='saisir-bulk')
    def saisir_bulk(self, request):
        """Délégué à services/note_saisie."""
        from .services.note_saisie import do_saisir_bulk
        return do_saisir_bulk(request)

class ResultatElementViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ResultatElement.objects.select_related(
        'inscription_element', 'session',
    ).all()
    serializer_class   = ResultatElementSerializer
    permission_classes = [RBACPermission]
    required_module    = 'eval_saisie'
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['session', 'est_valide', 'est_eliminatoire']


class ResultatSemestreViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ResultatSemestre.objects.select_related(
        'inscription_ped', 'session',
    ).all()
    serializer_class   = ResultatSemestreSerializer
    permission_classes = [RBACPermission]
    required_module    = 'eval_saisie'
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['session', 'est_admis']
