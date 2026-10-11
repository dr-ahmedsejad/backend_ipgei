"""Annonces : un message aux élèves choisis par classe (MPSI A, toutes les MP…).

GET  /api/v1/annonces/grille/  les cases à cocher (niveaux × classes de l'année)
GET  /api/v1/annonces/         l'historique des envois
POST /api/v1/annonces/         envoyer — { titre, texte, cibles: ["c12", …], resume }

Droit RBAC « annonces » : voir (grille, historique), modifier (envoyer).
"""
from django.db import transaction
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.permissions import RBACPermission

from . import services
from .models import Annonce

# Le lien de la notification : la page des notifications du portail web, qui
# affiche le texte entier. L'app mobile, elle, reconnaît le type « annonce »
# et ouvre l'écran de lecture.
LIEN = '/dashboard/notifications'


class AnnonceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Annonce
        fields = ['id', 'titre', 'texte', 'cibles', 'resume', 'nb_destinataires',
                  'auteur_nom', 'cree_le']
        read_only_fields = ['nb_destinataires', 'auteur_nom', 'cree_le']

    def validate_titre(self, v):
        v = (v or '').strip()
        if not v:
            raise serializers.ValidationError('Le titre est obligatoire.')
        return v

    def validate_texte(self, v):
        v = (v or '').strip()
        if not v:
            raise serializers.ValidationError("Le texte de l'annonce est obligatoire.")
        if len(v) > 5000:
            raise serializers.ValidationError('5000 caractères au plus.')
        return v

    def validate_cibles(self, v):
        if not isinstance(v, list) or not v:
            raise serializers.ValidationError('Cochez au moins une case.')
        return [str(x) for x in v][:500]


class AnnonceViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    serializer_class = AnnonceSerializer
    permission_classes = [RBACPermission]
    required_module = 'annonces'
    pagination_class = None

    def get_queryset(self):
        return Annonce.objects.all()[:50]

    @action(detail=False, methods=['get'], url_path='grille')
    def grille(self, request):
        return Response(services.grille())

    def create(self, request, *args, **kwargs):
        from apps.authentication.models import CustomUser
        from apps.notifications.services import notifier

        s = self.get_serializer(data=request.data)
        s.is_valid(raise_exception=True)
        ids, cibles = services.destinataires(s.validated_data['cibles'])
        if not ids:
            return Response({'detail': "Aucun étudiant n'a de compte dans les cases cochées."},
                            status=status.HTTP_400_BAD_REQUEST)
        u = request.user
        with transaction.atomic():
            annonce = s.save(cibles=cibles, auteur=u,
                             auteur_nom=(getattr(u, 'name', '') or u.get_username())[:150],
                             resume=(s.validated_data.get('resume') or '')[:500])
            annonce.nb_destinataires = notifier(
                CustomUser.objects.filter(pk__in=ids), annonce.titre, annonce.texte,
                type='annonce', lien=LIEN, auteur=u)
            annonce.save(update_fields=['nb_destinataires'])
        return Response(self.get_serializer(annonce).data, status=status.HTTP_201_CREATED)
