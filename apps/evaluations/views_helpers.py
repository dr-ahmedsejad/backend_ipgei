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
from core.telechargement import entete_piece_jointe

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


def _build_institution_context():
    """Retourne (institution, logo_url) pour les templates PDF."""
    from apps.parametres.models import Institution
    institution = Institution.objects.filter(est_principale=True).first()
    institution_logo_url = None
    if institution and institution.logo:
        abs_logo = os.path.join(str(settings.MEDIA_ROOT), institution.logo.name)
        institution_logo_url = 'file:///' + abs_logo.replace('\\', '/')
    return institution, institution_logo_url


def _render_pdf(template_name: str, context: dict, filename: str) -> HttpResponse:
    """Rend un template HTML en PDF via pdfkit et retourne un HttpResponse."""
    try:
        import pdfkit
    except ImportError:
        from rest_framework.response import Response
        return Response({'detail': 'pdfkit non installé.'}, status=500)

    from core.pdf_renderer import render_pdf_bytes
    options = {
        'orientation':              'Portrait',
        'margin-top':               '0.50in',
        'margin-right':             '0.50in',
        'margin-bottom':            '0.75in',
        'margin-left':              '0.50in',
        'footer-center':            'Page [page] / [toPage]',
        'enable-local-file-access': '',
        'encoding':                 'UTF-8',
        'no-stop-slow-scripts':     '',
        'javascript-delay':         '500',
        'load-error-handling':      'ignore',
        'load-media-error-handling': 'ignore',
        'disable-smart-shrinking':  '',
    }
    try:
        pdf_bytes = render_pdf_bytes(template_name, context, options)
    except Exception as exc:
        logger.error('pdfkit error %s: %s', template_name, exc)
        return HttpResponse(f'Erreur PDF : {exc}', status=500)
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = entete_piece_jointe(filename)
    return response


def _enrichir_lignes_anonymat(lignes: list, anonymat_map: dict) -> list:
    """Injecte numero_anonymat dans chaque dict (ordre alphabétique conservé)."""
    for ligne in lignes:
        ligne['numero_anonymat'] = anonymat_map.get(ligne['inscription_admin'].id, '')
    return lignes


def _grouper_lignes_par_groupe(lignes: list) -> list:
    """Regroupe des lignes de collecte par GROUPE (département de l'étudiant).
    Retourne [{'nom': <groupe>, 'lignes': [...]}] trié par nom de groupe.
    Sert à imprimer une fiche par groupe."""
    from collections import OrderedDict
    gm = OrderedDict()
    for lg in lignes:
        etu = lg.get('etudiant')
        dep = getattr(etu, 'departement', None) if etu else None
        key = dep.id if dep else 0
        if key not in gm:
            gm[key] = {
                'nom':     dep.nom if dep else '',   # vide => ligne Groupe masquée
                'filiere': (dep.filiere.intitule_fr if dep and getattr(dep, 'filiere_id', None) else ''),
                'lignes':  [],
            }
        gm[key]['lignes'].append(lg)
    return sorted(gm.values(), key=lambda g: g['nom'])


# ── Fiche d'émargement ─────────────────────────────────────────────────────────


def _resolve_em(em_id):
    """Retourne l'objet EM de planification ou None."""
    from apps.em.models import EM
    try:
        return EM.objects.get(pk=em_id)
    except EM.DoesNotExist:
        return None
