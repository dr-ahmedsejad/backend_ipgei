"""
API des sanctions pour fraude.

    GET  /api/v1/ipgei/fraudes/?inscription=&statut=&annee_universitaire=&search=
    GET  /api/v1/ipgei/fraudes/sanctionnables/?inscription=   notes et épreuves visables
    POST /api/v1/ipgei/fraudes/                               multipart, `elements` en JSON
    POST /api/v1/ipgei/fraudes/{id}/lever/                    {motif_levee}
    GET  /api/v1/ipgei/fraudes/{id}/pv/                       le PV joint

Pas de modification ni de suppression : une sanction se lève, et la fiche reste.
Le module RBAC est celui des jurys — prononcer une sanction change ce qu'un jury
a arrêté, ce n'est pas une saisie de notes ordinaire.
"""
import json
import mimetypes
import os

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import FileResponse
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.filters import SearchFilter
from rest_framework.mixins import CreateModelMixin, ListModelMixin, RetrieveModelMixin
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from core.mixins import InstitutionScopedMixin
from core.pagination import StandardPagination
from core.permissions import RBACPermission

from . import fraudes
from .models import ElementSanctionne, InscriptionIPGEI, Note, SanctionFraude


class ElementSanctionneSerializer(serializers.ModelSerializer):
    matiere_code     = serializers.CharField(source='note.matiere.code', read_only=True)
    matiere_intitule = serializers.CharField(source='note.matiere.intitule', read_only=True)
    semestre_code    = serializers.CharField(source='note.semestre.code', read_only=True)
    note_retenue     = serializers.DecimalField(source='note.note_retenue', max_digits=5,
                                                decimal_places=2, read_only=True)
    libelle          = serializers.CharField(read_only=True)

    class Meta:
        model  = ElementSanctionne
        fields = ['id', 'note', 'matiere_code', 'matiere_intitule', 'semestre_code',
                  'portee', 'composante', 'numero', 'libelle', 'valeurs_origine',
                  'note_retenue_avant', 'note_retenue']


class SanctionFraudeSerializer(serializers.ModelSerializer):
    etudiant_matricule = serializers.CharField(source='inscription.etudiant.matricule', read_only=True)
    etudiant_nom       = serializers.CharField(source='inscription.etudiant.nom_display', read_only=True)
    classe_nom         = serializers.CharField(source='inscription.classe.nom', read_only=True)
    annee_universitaire = serializers.CharField(source='inscription.annee_universitaire', read_only=True)
    nature_display     = serializers.CharField(source='get_nature_display', read_only=True)
    statut_display     = serializers.CharField(source='get_statut_display', read_only=True)
    cree_par_nom       = serializers.CharField(source='cree_par.name', read_only=True, default='')
    levee_par_nom      = serializers.CharField(source='levee_par.name', read_only=True, default='')
    elements           = ElementSanctionneSerializer(many=True, read_only=True)
    pv_nom             = serializers.SerializerMethodField()
    jurys_a_redeliberer = serializers.SerializerMethodField()

    class Meta:
        model  = SanctionFraude
        fields = ['id', 'inscription', 'etudiant_matricule', 'etudiant_nom', 'classe_nom',
                  'annee_universitaire', 'nature', 'nature_display', 'date_faits', 'motif',
                  'pv_nom', 'statut', 'statut_display', 'cree_par_nom', 'cree_le',
                  'motif_levee', 'levee_par_nom', 'levee_le', 'elements',
                  'jurys_a_redeliberer']

    def get_pv_nom(self, obj) -> str:
        return os.path.basename(obj.pv_deliberation.name) if obj.pv_deliberation else ''

    def get_jurys_a_redeliberer(self, obj) -> list:
        return [{'id': d.pk, 'libelle': str(d), 'date_validation': d.date_validation}
                for d in fraudes.jurys_a_redeliberer(obj)]


class SanctionFraudeViewSet(InstitutionScopedMixin, ListModelMixin, RetrieveModelMixin,
                            CreateModelMixin, GenericViewSet):
    serializer_class   = SanctionFraudeSerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_deliberation'
    institution_filter_field = 'inscription__classe__institution'
    parser_classes     = [MultiPartParser, FormParser, JSONParser]
    pagination_class   = StandardPagination
    queryset           = (SanctionFraude.objects
                          .select_related('inscription__etudiant', 'inscription__classe',
                                          'cree_par', 'levee_par')
                          .prefetch_related('elements__note__matiere',
                                            'elements__note__semestre'))
    filter_backends    = [DjangoFilterBackend, SearchFilter]
    filterset_fields   = ['id', 'inscription', 'statut', 'nature', 'inscription__classe',
                          'inscription__annee_universitaire']
    search_fields      = ['inscription__etudiant__matricule', 'inscription__etudiant__nom',
                          'motif']

    @staticmethod
    def _utilisateur(request):
        user = getattr(request, 'user', None)
        return user if getattr(user, 'is_authenticated', False) else None

    @staticmethod
    def _traduire(exc: DjangoValidationError):
        if hasattr(exc, 'message_dict'):
            return ValidationError(exc.message_dict)
        return ValidationError(exc.messages)

    def create(self, request, *args, **kwargs):
        inscription = (InscriptionIPGEI.objects.select_related('classe')
                       .filter(pk=request.data.get('inscription')).first())
        if inscription is None:
            raise ValidationError({'inscription': 'Inscription introuvable.'})
        brut = request.data.get('elements')
        if isinstance(brut, str):
            try:
                brut = json.loads(brut)
            except ValueError:
                raise ValidationError({'elements': 'Liste des éléments illisible.'})
        if not isinstance(brut, list):
            raise ValidationError({'elements': 'Désignez au moins une matière ou une épreuve.'})

        champ_date = serializers.DateField()
        try:
            date_faits = champ_date.to_internal_value(request.data.get('date_faits'))
        except serializers.ValidationError:
            raise ValidationError({'date_faits': 'Date des faits invalide (AAAA-MM-JJ).'})

        pv = request.FILES.get('pv_deliberation')
        if pv is not None:
            from core.validators import validate_document
            try:
                validate_document(pv)
            except DjangoValidationError as exc:
                raise ValidationError({'pv_deliberation': exc.messages})

        try:
            sanction = fraudes.sanctionner(
                inscription=inscription, nature=request.data.get('nature'),
                date_faits=date_faits, motif=request.data.get('motif'),
                pv_deliberation=pv, elements=brut, utilisateur=self._utilisateur(request))
        except DjangoValidationError as exc:
            raise self._traduire(exc)
        return Response(self.get_serializer(sanction).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='lever')
    def lever(self, request, pk=None):
        try:
            resultat = fraudes.lever(self.get_object(),
                                     motif_levee=request.data.get('motif_levee'),
                                     utilisateur=self._utilisateur(request))
        except DjangoValidationError as exc:
            raise self._traduire(exc)
        sanction = self.get_queryset().get(pk=resultat['sanction'].pk)
        return Response({**self.get_serializer(sanction).data, 'ecarts': resultat['ecarts']})

    @action(detail=True, methods=['get'], url_path='pv')
    def pv(self, request, pk=None):
        sanction = self.get_object()
        try:
            fichier = sanction.pv_deliberation.open('rb')
        except (FileNotFoundError, OSError, ValueError):
            return Response({'error': 'PV introuvable sur le serveur.'},
                            status=status.HTTP_404_NOT_FOUND)
        nom = os.path.basename(sanction.pv_deliberation.name)
        reponse = FileResponse(fichier, as_attachment=True, filename=nom,
                               content_type=mimetypes.guess_type(nom)[0]
                               or 'application/octet-stream')
        reponse['Cache-Control'] = 'no-store'
        reponse['X-Content-Type-Options'] = 'nosniff'
        return reponse

    @action(detail=False, methods=['get'], url_path='sanctionnables')
    def sanctionnables(self, request):
        """Les notes de l'inscription, et pour chacune les épreuves qu'on peut viser."""
        inscription = (InscriptionIPGEI.objects.select_related('classe', 'etudiant')
                       .filter(pk=request.query_params.get('inscription')).first())
        if inscription is None:
            raise ValidationError({'inscription': 'Inscription introuvable.'})
        notes = list(Note.objects.filter(inscription=inscription)
                     .select_related('matiere__partage_avec', 'semestre', 'inscription__classe')
                     .prefetch_related('evaluations')
                     .order_by('semestre__code', 'matiere__ordre', 'matiere__code'))
        actifs = fraudes.elements_actifs([n.pk for n in notes])
        lignes = []
        for note in notes:
            deja = actifs.get(note.pk, [])
            lignes.append({
                'note': note.pk,
                'semestre_code': note.semestre.code,
                'matiere_code': note.matiere.code,
                'matiere_intitule': note.matiere.intitule,
                'note_retenue': note.note_retenue,
                'verrouillee': note.verrouillee,
                'porteuse_code': (note.matiere.partage_avec.code
                                  if note.matiere.partage_avec_id else None),
                'sanction_active': deja[0].sanction_id if deja else None,
                'epreuves': fraudes.epreuves_sanctionnables(note),
            })
        return Response({
            'inscription': inscription.pk,
            'etudiant_matricule': inscription.etudiant.matricule,
            'etudiant_nom': inscription.etudiant.nom_display,
            'classe_nom': inscription.classe.nom,
            'annee_universitaire': inscription.annee_universitaire,
            'notes': lignes,
        })
