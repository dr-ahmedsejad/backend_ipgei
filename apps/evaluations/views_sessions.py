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


class SessionEvaluationViewSet(InstitutionScopedMixin, viewsets.ModelViewSet):
    queryset = SessionEvaluation.objects.select_related(
        'annee_univ', 'cloturee_par',
    ).all()
    serializer_class   = SessionEvaluationSerializer
    permission_classes = [RBACPermission]
    required_module    = 'eval_saisie'
    filter_backends    = [DjangoFilterBackend, SearchFilter]
    # 'annee_univ' est gere manuellement dans get_queryset (accepte ID ou libelle "2023-2024")
    filterset_fields   = ['type_session', 'type_semestre', 'est_ouverte', 'est_close']

    def get_permissions(self):
        # Le portail enseignant doit pouvoir LISTER les sessions ouvertes pour
        # saisir ses notes. Lecture seule de metadonnees (pas sensible). Toute
        # ecriture (create/update/cloturer...) reste protegee par eval_saisie.
        if self.action == 'list':
            return [IsAuthenticated()]
        return super().get_permissions()
    search_fields      = ['code', 'intitule']

    def get_queryset(self):
        qs = super().get_queryset()
        annee = self.request.query_params.get('annee_univ') or self.request.query_params.get('annee')
        if annee:
            annee_str = str(annee).strip()
            try:
                annee_id = int(annee_str)
                qs = qs.filter(annee_univ_id=annee_id)
            except ValueError:
                qs = qs.filter(annee_univ__annee=annee_str)
        return qs

    def perform_create(self, serializer):
        """Auto-résout annee_univ si absent, et FIGE le plafond rattrapage depuis
        le défaut institutionnel (snapshot) — ainsi un changement ultérieur du
        défaut global n'impacte jamais cette session."""
        extra = {}
        if not serializer.validated_data.get('annee_univ'):
            from apps.parametres.models import Year
            try:
                annee_str = self.request.user.contexte.annee_universitaire
                extra['annee_univ'] = Year.objects.filter(annee=annee_str).first()
            except Exception:
                extra['annee_univ'] = Year.objects.order_by('-annee').first()
        # Snapshot du plafond rattrapage depuis le défaut global (ParametresPonderation).
        from apps.scolarite.models import ParametresPonderation
        pp = ParametresPonderation.get()
        extra['rattrapage_plafond_actif'] = pp.rattrapage_plafond_actif
        extra['rattrapage_plafond'] = pp.rattrapage_plafond
        serializer.save(**extra)
        # SessionEvaluation hors TRACKED_MODELS → audit explicite (création).
        try:
            from core.audit_helpers import write_audit
            s = serializer.instance
            write_audit(
                action='CREATE', model_name='SessionEvaluation', object_id=str(s.pk),
                changes={'code': s.code, 'type_session': s.type_session,
                         'type_semestre': s.type_semestre, 'annee': str(s.annee_univ)},
                label=f'Création session {s}', keep_forever=True,
            )
        except Exception:
            logger.warning('Audit création session échoué', exc_info=True)

    def perform_destroy(self, instance):
        snap = {'code': instance.code, 'type_session': instance.type_session,
                'type_semestre': instance.type_semestre, 'annee': str(instance.annee_univ)}
        pk = instance.pk
        label = f'Suppression session {instance}'
        instance.delete()
        try:
            from core.audit_helpers import write_audit
            write_audit(
                action='DELETE', model_name='SessionEvaluation', object_id=str(pk),
                changes={k: {'old': v, 'new': None} for k, v in snap.items()},
                label=label, keep_forever=True,
            )
        except Exception:
            logger.warning('Audit suppression session échoué', exc_info=True)

    @action(detail=True, methods=['post'], url_path='ouvrir')
    def ouvrir(self, request, pk=None):
        """Ouvre la saisie des notes pour cette session."""
        session = self.get_object()
        if session.est_close:
            return Response({'detail': 'Session déjà clôturée.'}, status=status.HTTP_400_BAD_REQUEST)
        session.est_ouverte = True
        session.save(update_fields=['est_ouverte'])
        return Response(SessionEvaluationSerializer(session).data)

    @action(detail=True, methods=['post'], url_path='cloturer')
    def cloturer(self, request, pk=None):
        """Clôture la saisie des notes pour cette session."""
        session = self.get_object()
        if session.est_close:
            return Response({'detail': 'Session déjà clôturée.'}, status=status.HTTP_400_BAD_REQUEST)
        session.est_close    = True
        session.est_ouverte  = False
        session.cloturee_par = request.user
        session.save(update_fields=['est_close', 'est_ouverte', 'cloturee_par'])
        # SessionEvaluation n'est PAS dans TRACKED_MODELS (core/signals.py) → audit explicite.
        try:
            from core.audit_helpers import write_audit
            write_audit(
                action='UPDATE', model_name='SessionEvaluation', object_id=str(session.pk),
                changes={'est_close': {'old': False, 'new': True}},
                label=f'Clôture saisie notes — session {session}', keep_forever=True,
            )
        except Exception:
            logger.warning('Audit clôture session %s échoué', session.pk, exc_info=True)
        return Response(SessionEvaluationSerializer(session).data)

    @action(detail=True, methods=['post'], url_path='rouvrir')
    def rouvrir(self, request, pk=None):
        """Réouverture d'une session clôturée — réservée admin."""
        user = request.user
        if not (user and user.is_authenticated and (
            getattr(user, 'role', '') == 'admin' or user.is_superuser
        )):
            return Response(
                {'detail': 'Seul un administrateur peut réouvrir une session clôturée.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        session = self.get_object()
        if not session.est_close:
            return Response({'detail': 'Session déjà ouverte.'}, status=status.HTTP_400_BAD_REQUEST)
        session.est_close    = False
        session.est_ouverte  = True
        session.cloturee_par = None
        session.save(update_fields=['est_close', 'est_ouverte', 'cloturee_par'])
        try:
            from core.audit_helpers import write_audit
            write_audit(
                action='UPDATE', model_name='SessionEvaluation', object_id=str(session.pk),
                changes={'est_close': {'old': True, 'new': False}},
                label=f'Réouverture session {session} (admin)', keep_forever=True,
            )
        except Exception:
            logger.warning('Audit réouverture session %s échoué', session.pk, exc_info=True)
        return Response(SessionEvaluationSerializer(session).data)

    @action(detail=True, methods=['post'], url_path='activer-plafond')
    def activer_plafond(self, request, pk=None):
        """Active / met à jour le plafond rattrapage SUR CETTE session (snapshot),
        puis recalcule la chaîne : éléments plafonnés → modules → semestres.

        Permet d'appliquer la règle (conseil scientifique) à une session DÉJÀ créée
        — le snapshot n'étant posé qu'à la création. Body optionnel {"valeur": 10} ;
        sinon = défaut institutionnel (ParametresPonderation). Bloqué si clôturée."""
        from decimal import Decimal, InvalidOperation
        from apps.scolarite.models import ParametresPonderation
        session = self.get_object()
        if session.est_close:
            return Response(
                {'detail': 'Session clôturée — réouvrez-la avant d\'activer le plafond.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        ancienne = session.rattrapage_plafond if session.rattrapage_plafond_actif else None
        valeur_raw = request.data.get('valeur')
        if valeur_raw is not None:
            try:
                valeur = Decimal(str(valeur_raw))
            except (InvalidOperation, ValueError):
                return Response({'detail': 'Valeur de plafond invalide.'},
                                status=status.HTTP_400_BAD_REQUEST)
        else:
            valeur = ParametresPonderation.get().rattrapage_plafond

        session.rattrapage_plafond_actif = True
        session.rattrapage_plafond = valeur
        session.save(update_fields=['rattrapage_plafond_actif', 'rattrapage_plafond'])

        # Recalcul de la chaîne : ME (plafonnées) -> modules -> semestres.
        svc = NoteCalculService(session)
        n_el  = len(svc.calculer_tous_elements_session())
        n_mod = len(svc.calculer_tous_modules_session())
        n_sem = len(svc.calculer_tous_semestres_session())
        try:
            from core.audit_helpers import write_audit
            write_audit(
                action='UPDATE', model_name='SessionEvaluation', object_id=str(session.pk),
                changes={'rattrapage_plafond': {'old': str(ancienne), 'new': str(valeur)}},
                label=f'Activation plafond rattrapage {valeur} + recalcul — session {session}',
                keep_forever=True,
            )
        except Exception:
            logger.warning('Audit activation plafond session %s échoué', session.pk, exc_info=True)

        data = SessionEvaluationSerializer(session).data
        data['recalcul'] = {'elements': n_el, 'modules': n_mod, 'semestres': n_sem}
        return Response(data)

    @action(detail=True, methods=['post'], url_path='calculer')
    def calculer(self, request, pk=None):
        """Déclenche le calcul de notes pour toute la session."""
        session = self.get_object()
        if session.est_close:
            return Response(
                {'detail': 'Session clôturée — recalcul interdit.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        service = NoteCalculService(session)
        resultats = service.calculer_tous_elements_session()
        return Response({'resultats_calcules': len(resultats)})

    @action(detail=True, methods=['post'], url_path='calculer-modules')
    def calculer_modules(self, request, pk=None):
        """
        POST /api/v1/evaluations/sessions/{id}/calculer-modules/
        Calcule les ResultatModule pour tous les étudiants (Art. 13).
        Doit être appelé après /calculer/ (calcul des éléments).
        """
        session = self.get_object()
        if session.est_close:
            return Response(
                {'detail': 'Session clôturée — recalcul interdit.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        service = NoteCalculService(session)
        resultats = service.calculer_tous_modules_session()
        return Response({'modules_calcules': len(resultats)})

    @action(detail=True, methods=['post'], url_path='calculer-semestres')
    def calculer_semestres(self, request, pk=None):
        """
        POST /api/v1/evaluations/sessions/{id}/calculer-semestres/
        Calcule les ResultatSemestre pour tous les étudiants (Art. 14-15).
        Doit être appelé après /calculer-modules/.
        """
        session = self.get_object()
        if session.est_close:
            return Response(
                {'detail': 'Session clôturée — recalcul interdit.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        service = NoteCalculService(session)
        resultats = service.calculer_tous_semestres_session()
        return Response({'semestres_calcules': len(resultats)})
