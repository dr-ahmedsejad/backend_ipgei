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


class PVDeliberationViewSet(InstitutionScopedMixin, viewsets.ModelViewSet):
    queryset = PVDeliberation.objects.prefetch_related(
        'lignes', 'membres_jury',
    ).select_related(
        'session', 'session__annee_univ', 'annee_univ', 'filiere', 'president_jury',
    ).all()
    serializer_class   = PVDeliberationSerializer
    permission_classes = [RBACPermission]
    required_module    = 'delib_pv'
    filter_backends    = [DjangoFilterBackend]
    # Note : on retire 'annee_univ' du filterset_fields auto pour le gerer
    # nous-memes (couvre PV annuel via annee_univ ET PV semestriel via session.annee_univ)
    filterset_fields   = ['session', 'filiere', 'niveau', 'type_pv', 'est_clos']

    def get_queryset(self):
        from django.db.models import Q
        qs = super().get_queryset()
        # Filtre annee_univ etendu : matche les PV annuels (annee_univ direct)
        # ET les PV semestriels (via session.annee_univ)
        # Accepte un id (entier) OU un libelle '2023-2024' (string)
        annee = self.request.query_params.get('annee_univ') or self.request.query_params.get('annee')
        if annee:
            annee_str = str(annee).strip()
            try:
                annee_id = int(annee_str)
                qs = qs.filter(Q(annee_univ_id=annee_id) | Q(session__annee_univ_id=annee_id))
            except ValueError:
                # Pas un entier → traiter comme libelle '2023-2024'
                qs = qs.filter(
                    Q(annee_univ__annee=annee_str) | Q(session__annee_univ__annee=annee_str)
                )

        # Filtre parite semestre (contexte de session) : Impairs -> S1/S3/S5,
        # Pairs -> S2/S4/S6. Ne s'applique qu'aux PV semestriels ; les PV annuels
        # ne sont lies a aucune parite (delibres en fin d'annee) et restent
        # visibles quel que soit le semestre du contexte.
        parite = (self.request.query_params.get('semestre_type')
                  or self.request.query_params.get('type_semestre'))
        if parite in ('Impairs', 'Pairs'):
            impairs = ['S1', 'S3', 'S5', 'S7', 'S9']
            pairs   = ['S2', 'S4', 'S6', 'S8', 'S10']
            codes = impairs if parite == 'Impairs' else pairs
            qs = qs.filter(Q(type_pv='annuel') | Q(semestre_code__in=codes))

        return qs

    def _get_service(self, pv):
        if pv.type_pv == 'semestriel':
            return DeliberationSemestreService(pv)
        return get_deliberation_annuelle_service(pv)

    @action(detail=True, methods=['post'], url_path='clore')
    def clore(self, request, pk=None):
        """POST .../pvs/{id}/clore/ — délégué à services/pv_actions."""
        from .services.pv_actions import do_clore
        return do_clore(self.get_object())

    @action(detail=True, methods=['post'], url_path='rouvrir')
    def rouvrir(self, request, pk=None):
        """Réouverture d'un PV clos — réservée aux admins (délégué à services/pv_actions)."""
        user = request.user
        if not (user and user.is_authenticated and (user.role == 'admin' or user.is_superuser)):
            return Response(
                {'detail': 'Seul un administrateur peut réouvrir un PV clos.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        from .services.pv_actions import do_rouvrir
        return do_rouvrir(self.get_object())

    @action(detail=True, methods=['post'], url_path='attribuer-diplomes')
    def attribuer_diplomes(self, request, pk=None):
        """
        POST .../pvs/{id}/attribuer-diplomes/ — alimente le registre des diplômes
        (crée les RegistreDiplome des diplômés). Réservé au PV annuel de FIN DE
        CYCLE et CLÔTURÉ (PV de jury de délivrance — Art. 26 / Art. 30).
        Idempotent, append-only. Renvoie {'crees','deja','non_eligibles','ignores'}.
        """
        pv = self.get_object()
        if not pv.est_clos:
            return Response(
                {'detail': "Le PV doit être clôturé avant l'attribution des diplômes."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from apps.documents.services import attribuer_diplomes_pv
        return Response(attribuer_diplomes_pv(pv))

    def perform_destroy(self, instance):
        """
        Suppression d'un PV — RESERVEE aux administrateurs.
        - Refuse si l'utilisateur n'est pas admin/superuser.
        - Pour un PV CLOS : supprime quand meme (admin a deja le pouvoir de rouvrir).
        - RachatNote (PROTECT) : suppression manuelle prealable pour debloquer le CASCADE.
        - LigneDeliberation, MembreJury, ParametreJury, ObligationRattrapage : CASCADE auto.
        - AuditLog cree pour tracer l'operation.
        """
        from rest_framework.exceptions import PermissionDenied
        from django.db import transaction
        from core.models import AuditLog
        from apps.evaluations.models import RachatNote

        user = self.request.user
        is_admin = user and user.is_authenticated and (
            getattr(user, 'role', '') == 'admin' or user.is_superuser
        )
        if not is_admin:
            raise PermissionDenied('Seul un administrateur peut supprimer un PV de délibération.')

        pv_id = instance.id
        pv_label = (
            f"PV #{pv_id} type={instance.type_pv} "
            f"filiere={instance.filiere.code if instance.filiere else '?'} "
            f"L{instance.niveau} "
            f"{'session=' + instance.session.code if instance.session else 'annee=' + str(instance.annee_univ)}"
        )

        with transaction.atomic():
            # 1. Supprimer RachatNote (PROTECT) explicitement
            nb_rachats = RachatNote.objects.filter(pv=instance).count()
            if nb_rachats:
                RachatNote.objects.filter(pv=instance).delete()
            # 2. AuditLog
            AuditLog.objects.create(
                user=user,
                action='DELETE',   # core.models.ACTION_DELETE (constante module, pas attribut de classe)
                model_name='PVDeliberation',
                object_id=str(pv_id),
                changes={
                    'pv_label':           pv_label,
                    'est_clos':           instance.est_clos,
                    'nb_rachats_supprimes': nb_rachats,
                    'motif':              'Suppression admin via DELETE /pvs/<id>/',
                },
            )
            # 3. Cascade automatique pour le reste
            instance.delete()

    @action(detail=True, methods=['post'], url_path='peupler')
    def peupler(self, request, pk=None):
        """POST .../pvs/{id}/peupler/ — peuple le PV + recalcule (délégué à services/pv_actions)."""
        pv = self.get_object()
        from .services.pv_actions import do_peupler
        return do_peupler(pv, self._get_service(pv))

    @action(detail=True, methods=['post'], url_path='calculer-decisions')
    def calculer_decisions(self, request, pk=None):
        """Applique les décisions (Art. 15 ou Art. 20) sur toutes les lignes du PV."""
        pv = self.get_object()
        if pv.est_clos:
            return Response({'detail': 'PV déjà clos.'}, status=status.HTTP_400_BAD_REQUEST)
        count = self._get_service(pv).calculer_decisions()
        return Response({'lignes_traitees': count})

    @action(detail=True, methods=['post'], url_path='recalculer-tout')
    def recalculer_tout(self, request, pk=None):
        """POST .../pvs/{id}/recalculer-tout/ — délégué à services/pv_actions."""
        pv = self.get_object()
        from .services.pv_actions import do_recalculer_tout
        return do_recalculer_tout(pv, self._get_service(pv))

    @action(detail=True, methods=['get'], url_path='diagnostic-sessions')
    def diagnostic_sessions(self, request, pk=None):
        """GET .../pvs/{id}/diagnostic-sessions/ — délégué à services/pv_diagnostic."""
        from .services.pv_diagnostic import build_pv_diagnostic
        return build_pv_diagnostic(self.get_object())

    @action(detail=True, methods=['post'], url_path='generer-obligations')
    def generer_obligations(self, request, pk=None):
        """
        POST /api/v1/evaluations/pvs/{id}/generer-obligations/
        Génère les ObligationRattrapage Art. 17 (semestriel uniquement).
        """
        pv = self.get_object()
        if pv.type_pv != 'semestriel':
            return Response(
                {'detail': 'Cette action ne s\'applique qu\'aux PV semestriels.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Une session de rattrapage est terminale : pas de « rattrapage du rattrapage ».
        # Les obligations se décident à la session NORMALE.
        if not (pv.session and pv.session.type_session == 'normale'):
            return Response(
                {'detail': "Les obligations de rattrapage se génèrent sur le PV de la "
                           "session NORMALE, pas sur une session de rattrapage."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if pv.est_clos:
            return Response({'detail': 'PV déjà clos.'}, status=status.HTTP_400_BAD_REQUEST)
        count = DeliberationSemestreService(pv).generer_obligations()
        return Response({'obligations_creees': count})

    @action(detail=True, methods=['get'], url_path='pdf')
    def pdf(self, request, pk=None):
        """GET .../pvs/{id}/pdf/ — export délégué à services/pv_export.build_pv_pdf."""
        from .services.pv_export import build_pv_pdf
        return build_pv_pdf(self.get_object())

    @action(detail=True, methods=['get'], url_path='excel')
    def excel(self, request, pk=None):
        """GET .../pvs/{id}/excel/ — export délégué à services/pv_export.build_pv_excel."""
        from .services.pv_export import build_pv_excel
        return build_pv_excel(self.get_object())

    @action(detail=True, methods=['get'], url_path='rapport-progression')
    def rapport_progression(self, request, pk=None):
        """GET .../pvs/{id}/rapport-progression/ — délégué à services/pv_export."""
        from .services.pv_export import build_pv_rapport_progression
        return build_pv_rapport_progression(self.get_object())

    @action(detail=True, methods=['post'], url_path='signer')
    def signer(self, request, pk=None):
        """POST .../pvs/{id}/signer/ — délégué à services/pv_actions."""
        from .services.pv_actions import do_signer
        return do_signer(self.get_object(), request)

class LigneDeliberationViewSet(InstitutionScopedMixin, viewsets.ModelViewSet):
    institution_filter_field = 'pv__institution'
    queryset = LigneDeliberation.objects.select_related(
        'pv', 'inscription_admin',
        'inscription_admin__etudiant',
    ).all()
    serializer_class   = LigneDeliberationSerializer
    permission_classes = [RBACPermission]
    required_module    = 'delib_pv'
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['pv', 'decision']


class ResultatModuleViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ResultatModule.objects.select_related(
        'inscription_ped', 'module', 'session',
    ).all()
    serializer_class   = ResultatModuleSerializer
    permission_classes = [RBACPermission]
    required_module    = 'eval_saisie'
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['session', 'inscription_ped', 'est_valide', 'code_statut',
                          'inscription_ped__inscription_admin']


class MembreJuryViewSet(viewsets.ModelViewSet):
    queryset = MembreJury.objects.select_related('pv', 'user').all()
    serializer_class   = MembreJurySerializer
    permission_classes = [RBACPermission]
    required_module    = 'delib_jury'
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['pv', 'role']

    def perform_destroy(self, instance):
        if instance.pv.est_clos:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('PV clos — suppression interdite.')
        instance.delete()


class ObligationRattrapageViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ObligationRattrapage.objects.select_related(
        'ligne', 'inscription_element__element',
    ).all()
    serializer_class   = ObligationRattrapageSerializer
    permission_classes = [RBACPermission]
    required_module    = 'delib_rachat'
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['ligne', 'type_obligation', 'code_statut_initial']


class ParametreJuryViewSet(viewsets.ModelViewSet):
    """Paramètres de seuils pour un PV de délibération."""
    queryset           = ParametreJury.objects.select_related('pv').all()
    serializer_class   = ParametreJurySerializer
    permission_classes = [RBACPermission]
    required_module    = 'delib_jury'
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['pv']


class RachatNoteViewSet(viewsets.ModelViewSet):
    """
    Registre immuable des rachats de notes.
    - CREATE autorisé uniquement si le PV n'est pas encore clos.
    - UPDATE et DELETE bloqués au niveau du modèle (PermissionError).
    """
    queryset = RachatNote.objects.select_related(
        'pv', 'ligne',
        'ligne__inscription_admin__etudiant',
        'decidee_par',
    ).all()
    serializer_class   = RachatNoteSerializer
    permission_classes = [RBACPermission]
    required_module    = 'delib_rachat'
    filter_backends    = [DjangoFilterBackend, SearchFilter]
    filterset_fields   = ['pv', 'ligne']
    search_fields      = [
        'ligne__inscription_admin__etudiant__nom',
        'ligne__inscription_admin__etudiant__matricule',
        'motif',
    ]

    @action(detail=False, methods=['get'], url_path='modules-consolides')
    def modules_consolides(self, request):
        """Modules d'un étudiant pour un semestre, avec la moyenne CONSOLIDEE
        (moteur du relevé : tous les EM à travers les années, max SN/SR +
        compensation). SPECIFIQUE au rachat — n'affecte pas ResultatModuleViewSet.
        GET .../rachats/modules-consolides/?inscription_admin=<id>&semestre=<code>"""
        from apps.inscriptions.models import InscriptionAdministrative, InscriptionPedagogique
        from apps.documents.services import calculer_resultat_semestre_consolide

        ia_id    = request.query_params.get('inscription_admin')
        sem_code = request.query_params.get('semestre')
        if not ia_id or not sem_code:
            return Response([])
        ia = (InscriptionAdministrative.objects
              .select_related('etudiant', 'annee_univ').filter(pk=ia_id).first())
        if not ia:
            return Response([])
        ip = (InscriptionPedagogique.objects
              .filter(inscription_admin=ia, semestre__code_semestre=sem_code)
              .select_related('semestre').first())
        if not ip or not ip.semestre:
            return Response([])
        try:
            res = calculer_resultat_semestre_consolide(ia.etudiant, ip.semestre, ia.annee_univ)
        except Exception:
            return Response([])
        modules = [{
            'module_code':     m['code'],
            'module_intitule': m['intitule_fr'],
            'moyenne':         m['note_module'],                 # CONSOLIDEE (relevé)
            'est_valide':      m['decision'] == 'Validé',
            'code_statut':     'V' if m['decision'] == 'Validé' else 'NV',
        } for m in (res.get('modules') or [])]
        return Response(modules)

    def perform_create(self, serializer):
        pv   = serializer.validated_data['pv']
        ligne = serializer.validated_data['ligne']
        if pv.est_clos:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('Le PV est clos — aucun rachat ne peut être ajouté.')
        if ligne.decision not in ('ajourned', 'rachat'):
            from rest_framework.exceptions import ValidationError
            raise ValidationError('Seul un étudiant ajourné peut faire l\'objet d\'un rachat.')
        serializer.save()
        # Mettre à jour la décision de la ligne
        ligne.decision = 'rachat'
        ligne.save(update_fields=['decision'])

    def perform_update(self, serializer):
        raise status.HTTP_405_METHOD_NOT_ALLOWED

    def perform_destroy(self, instance):
        from rest_framework.exceptions import MethodNotAllowed
        raise MethodNotAllowed('DELETE', detail='RachatNote est immuable.')


# ── Helper PDF partagé ─────────────────────────────────────────────────────────
