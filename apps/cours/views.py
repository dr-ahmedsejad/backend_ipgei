"""ViewSet du module Cours (fil pédagogique par EM)."""
import json

from django.db import transaction
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from .models import Cours, CoursPieceJointe, CoursReponse
from .serializers import CoursSerializer, CoursReponseSerializer
from . import services

MAX_FICHIER_MO = 25
MAX_FICHIER_OCTETS = MAX_FICHIER_MO * 1024 * 1024


def _type_from_content(content_type):
    ct = (content_type or '').lower()
    if ct.startswith('image/'):
        return 'image'
    if ct.startswith('video/'):
        return 'video'
    return 'fichier'


def _type_from_url(url):
    u = (url or '').lower()
    if any(s in u for s in ('youtube.com', 'youtu.be', 'vimeo.com', 'dailymotion')):
        return 'video'
    return 'lien'


class CoursViewSet(viewsets.ModelViewSet):
    """Fil de cours par EM. Publication réservée aux enseignants de l'EM ;
    consultation/réponse ouvertes aux enseignants de l'EM et aux étudiants inscrits."""
    serializer_class   = CoursSerializer
    permission_classes = [IsAuthenticated]
    parser_classes     = [MultiPartParser, FormParser, JSONParser]

    def _annee(self):
        return (self.request.query_params.get('annee_universitaire')
                or self.request.data.get('annee_universitaire')
                or services.annee_courante(self.request.user))

    def _base_qs(self):
        # Table inbox FK-free : plus de select_related em/auteur (snapshots stockés).
        return Cours.objects.prefetch_related('pieces', 'reponses')

    def get_queryset(self):
        u = self.request.user
        qs = self._base_qs().all()
        annee = self._annee()
        # Admin : accès total. Sinon : uniquement les EMs visibles (enseignés/inscrits).
        if not services._is_admin(u):
            qs = qs.filter(em_id__in=services.ems_visibles_ids(u, annee))
        p = self.request.query_params
        if p.get('em'):
            qs = qs.filter(em_id=p['em'])
        if p.get('annee_universitaire'):
            qs = qs.filter(annee_universitaire=p['annee_universitaire'])
        return qs

    def get_object(self):
        """Opérations sur UN cours (retrieve/repondre/update/destroy) : on résout par pk
        puis on vérifie l'accès avec l'année DU COURS (pas celle, faillible, de la requête
        — contexte/année active pouvant différer de l'année du cours)."""
        from django.shortcuts import get_object_or_404
        from rest_framework.exceptions import PermissionDenied
        obj = get_object_or_404(self._base_qs(), pk=self.kwargs.get('pk'))
        if not services.peut_voir_em(self.request.user, obj.em_id, obj.annee_universitaire):
            raise PermissionDenied("Accès non autorisé à ce cours.")
        return obj

    def create(self, request, *args, **kwargs):
        from apps.parametres.models import Institution
        em_id = request.data.get('em')
        if not em_id:
            return Response({'detail': 'EM requis.'}, status=status.HTTP_400_BAD_REQUEST)
        annee = self._annee()
        if not services.peut_publier_em(request.user, em_id, annee):
            return Response(
                {'detail': "Vous ne pouvez publier que pour un EM que vous enseignez cette année."},
                status=status.HTTP_403_FORBIDDEN)

        titre   = (request.data.get('titre') or '').strip()
        contenu = (request.data.get('contenu') or '').strip()

        # Fichiers uploadés
        fichiers = request.FILES.getlist('fichiers')
        for f in fichiers:
            if f.size > MAX_FICHIER_OCTETS:
                return Response(
                    {'detail': f'Fichier « {f.name} » trop volumineux (max {MAX_FICHIER_MO} Mo).'},
                    status=status.HTTP_400_BAD_REQUEST)

        # Liens externes : JSON [{url, type?, libelle?}]
        liens = request.data.get('liens')
        if isinstance(liens, str) and liens.strip():
            try:
                liens = json.loads(liens)
            except json.JSONDecodeError:
                liens = []
        if not isinstance(liens, list):
            liens = []

        if not contenu and not fichiers and not liens:
            return Response({'detail': 'Un cours doit contenir du texte, un fichier ou un lien.'},
                            status=status.HTTP_400_BAD_REQUEST)

        inst = Institution.objects.filter(est_principale=True).first()
        em_code, em_intitule = services.snapshot_em(em_id)
        auteur_id, auteur_nom, auteur_role = services.snapshot_auteur(request.user)
        with transaction.atomic():
            cours = Cours.objects.create(
                em_id=em_id, em_code=em_code, em_intitule=em_intitule,
                annee_universitaire=annee, institution_id=(inst.id if inst else None),
                auteur_id=auteur_id, auteur_nom=auteur_nom, auteur_role=auteur_role,
                titre=titre, contenu=contenu,
            )
            for f in fichiers:
                CoursPieceJointe.objects.create(
                    cours=cours, type=_type_from_content(getattr(f, 'content_type', '')),
                    fichier=f, libelle=f.name[:255],
                )
            for lien in liens:
                url = (lien.get('url') or '').strip() if isinstance(lien, dict) else ''
                if not url:
                    continue
                CoursPieceJointe.objects.create(
                    cours=cours,
                    type=(lien.get('type') or _type_from_url(url)),
                    url=url, libelle=(lien.get('libelle') or '')[:255],
                )
        ser = self.get_serializer(cours)
        return Response(ser.data, status=status.HTTP_201_CREATED)

    def _est_auteur_ou_admin(self, cours):
        u = self.request.user
        return services._is_admin(u) or (cours.auteur_id == getattr(u, 'pk', None))

    def update(self, request, *args, **kwargs):
        cours = self.get_object()
        if not self._est_auteur_ou_admin(cours):
            return Response({'detail': "Seul l'auteur peut modifier ce cours."},
                            status=status.HTTP_403_FORBIDDEN)
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        cours = self.get_object()
        if not self._est_auteur_ou_admin(cours):
            return Response({'detail': "Seul l'auteur peut supprimer ce cours."},
                            status=status.HTTP_403_FORBIDDEN)
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def repondre(self, request, pk=None):
        cours = self.get_object()   # 403 si l'utilisateur ne peut pas voir ce cours
        contenu = (request.data.get('contenu') or '').strip()
        if not contenu:
            return Response({'detail': 'La réponse ne peut pas être vide.'},
                            status=status.HTTP_400_BAD_REQUEST)
        # Réponse ciblée : `parent` doit appartenir au même cours.
        parent = None
        parent_id = request.data.get('parent')
        if parent_id:
            parent = CoursReponse.objects.filter(pk=parent_id, cours=cours).first()
            if parent is None:
                return Response({'detail': 'Réponse ciblée introuvable pour ce cours.'},
                                status=status.HTTP_400_BAD_REQUEST)
        auteur_id, auteur_nom, auteur_role = services.snapshot_auteur(request.user)
        rep = CoursReponse.objects.create(
            cours=cours, parent=parent,
            auteur_id=auteur_id, auteur_nom=auteur_nom, auteur_role=auteur_role,
            contenu=contenu)
        return Response(CoursReponseSerializer(rep).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch', 'delete'], url_path=r'reponses/(?P<rep_id>\d+)')
    def modifier_reponse(self, request, pk=None, rep_id=None):
        """Modifier (PATCH) ou supprimer (DELETE) SA propre réponse (ou admin)."""
        from django.shortcuts import get_object_or_404
        cours = self.get_object()   # 403 si l'utilisateur ne peut pas voir ce cours
        rep = get_object_or_404(CoursReponse, pk=rep_id, cours=cours)
        if not (services._is_admin(request.user) or rep.auteur_id == getattr(request.user, 'pk', None)):
            return Response({'detail': "Vous ne pouvez modifier que vos propres réponses."},
                            status=status.HTTP_403_FORBIDDEN)
        if request.method == 'DELETE':
            rep.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        contenu = (request.data.get('contenu') or '').strip()
        if not contenu:
            return Response({'detail': 'La réponse ne peut pas être vide.'},
                            status=status.HTTP_400_BAD_REQUEST)
        from django.utils import timezone
        rep.contenu = contenu
        rep.date_modification = timezone.now()
        rep.save(update_fields=['contenu', 'date_modification'])
        return Response(CoursReponseSerializer(rep).data)

    @action(detail=False, methods=['get'], url_path='mes-ems')
    def mes_ems(self, request):
        """EMs de l'utilisateur (enseignés et/ou où il est inscrit) + droit de publier."""
        annee = self._annee()
        return Response(services.ems_utilisateur(request.user, annee))

    @action(detail=False, methods=['get'], url_path='apercu-lien')
    def apercu_lien(self, request):
        """Prévisualisation d'un lien externe (embed vidéo + métadonnées OpenGraph).
        Résultat mis en cache 6 h (les OG changent rarement, évite de refrapper la cible)."""
        from django.core.cache import cache
        from .link_preview import apercu_lien as _apercu
        url = (request.query_params.get('url') or '').strip()
        if not url:
            return Response({'detail': 'Paramètre url requis.'}, status=status.HTTP_400_BAD_REQUEST)
        key = f'cours:apercu:{url}'
        data = cache.get(key)
        if data is None:
            data = _apercu(url)
            cache.set(key, data, 6 * 3600)
        return Response(data)
