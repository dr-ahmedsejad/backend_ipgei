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

from .views_helpers import (
    _build_institution_context, _render_pdf,
    _enrichir_lignes_anonymat, _grouper_lignes_par_groupe, _resolve_em,
)


class EmargementViewSet(viewsets.ViewSet):
    """
    GET /api/v1/evaluations/emargement/pdf/?filiere=X&niveau=N&semestre=SX&annee_univ=Y
    Génère la fiche d'émargement pour une filière / niveau / semestre / année.
    """
    permission_classes = [RBACPermission]
    required_module    = 'eval_emargement'

    @action(detail=False, methods=['get'], url_path='pdf')
    def pdf(self, request):
        from .services.note_reports import build_emargement_pdf
        return build_emargement_pdf(request)

    @action(detail=False, methods=['get'], url_path='excel')
    def excel(self, request):
        from .services.note_reports import build_emargement_excel
        return build_emargement_excel(request)

# ── Fiches de collecte de notes ────────────────────────────────────────────────


class CollecteNotesViewSet(viewsets.ViewSet):
    """
    Fiches de collecte de notes (session normale et rattrapage).
    GET /api/v1/evaluations/collecte-notes/pdf/     — session normale
    GET /api/v1/evaluations/collecte-rattrapage/pdf/ — session rattrapage
    """
    permission_classes = [RBACPermission]
    required_module    = 'eval_collecte'

    @action(detail=False, methods=['get'], url_path='pdf')
    def pdf_normale(self, request):
        from .services.note_reports import build_collecte_pdf
        return build_collecte_pdf(request, 'normale')

    @action(detail=False, methods=['get'], url_path='rattrapage-pdf')
    def pdf_rattrapage(self, request):
        from .services.note_reports import build_collecte_pdf
        return build_collecte_pdf(request, 'rattrapage')

    @action(detail=False, methods=['get'], url_path='tous-pdf')
    def pdf_tous(self, request):
        from .services.note_reports import build_collecte_pdf_tous
        return build_collecte_pdf_tous(request, 'normale')

    @action(detail=False, methods=['get'], url_path='tous-rattrapage-pdf')
    def pdf_tous_rattrapage(self, request):
        from .services.note_reports import build_collecte_pdf_tous
        return build_collecte_pdf_tous(request, 'rattrapage')

    # ── Variantes EXCEL (mêmes étudiants que les PDF) ──────────────────────────
    @action(detail=False, methods=['get'], url_path='excel')
    def excel_normale(self, request):
        from .services.note_reports import build_collecte_excel
        return build_collecte_excel(request, 'normale')

    @action(detail=False, methods=['get'], url_path='rattrapage-excel')
    def excel_rattrapage(self, request):
        from .services.note_reports import build_collecte_excel
        return build_collecte_excel(request, 'rattrapage')

    @action(detail=False, methods=['get'], url_path='tous-excel')
    def excel_tous(self, request):
        from .services.note_reports import build_collecte_excel_tous
        return build_collecte_excel_tous(request, 'normale')

    @action(detail=False, methods=['get'], url_path='tous-rattrapage-excel')
    def excel_tous_rattrapage(self, request):
        from .services.note_reports import build_collecte_excel_tous
        return build_collecte_excel_tous(request, 'rattrapage')

# ── Anonymat ──────────────────────────────────────────────────────────────────


class AnonymatSessionViewSet(viewsets.ViewSet):
    """
    Gestion des anonymats par (étudiant × session).
    POST /api/v1/evaluations/anonymats/generer/?session=X[&regenerer=1]
    GET  /api/v1/evaluations/anonymats/?session=X
    GET  /api/v1/evaluations/anonymats/levee/pdf/?session=X
    """
    permission_classes = [RBACPermission]
    required_module    = 'eval_anonymat'

    def list(self, request):
        from .services.note_reports import list_anonymats
        return list_anonymats(request)

    @action(detail=False, methods=['post'], url_path='generer')
    def generer(self, request):
        from .services.note_reports import do_generer_anonymat
        return do_generer_anonymat(request)

    @action(detail=False, methods=['get'], url_path='levee/pdf')
    def levee_pdf(self, request):
        from .services.note_reports import build_levee_pdf
        return build_levee_pdf(request)
