"""
Réclamation de séance — la boîte de réception des enseignants.

Depuis le portail en ligne, un enseignant conteste une séance portée « Non
fait ». L'écriture va dans `ReclamationSeance`, table exclue de la
synchronisation : elle survit donc au push suivant, là où une écriture dans le
suivi lui-même serait écrasée par le dump du local.

Aucune clé étrangère vers le pointage. On recopie sa CLÉ NATURELLE et ses
libellés au moment de la réclamation, si bien qu'elle reste lisible même si la
séance a été régénérée ou supprimée en local entre-temps.
"""
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.suivi.models import SuiviePointage
from core.permissions import IsAdminOrIT, IsEnseignant

from .models import ReclamationSeance
from .serializers import ReclamationSeanceSerializer

#: Le seul état qui se conteste. Comparé en minuscules — le pointage est saisi
#: à la main côté local, et « Non Fait » s'y rencontre autant que « Non fait ».
COMMENTAIRE_CONTESTABLE = 'non fait'


class ReclamerSeanceView(APIView):
    """
    Côté enseignant, sur le portail :

      * `GET  /api/v1/portail/reclamer-seance/` — mes réclamations ;
      * `POST /api/v1/portail/reclamer-seance/` — `{pointage, motif}`.
    """
    permission_classes = [IsEnseignant]

    def _prof(self, request):
        return getattr(request.user, 'prof_profile', None)

    def get(self, request):
        prof = self._prof(request)
        if prof is None:
            return Response({'detail': 'Profil enseignant introuvable.'},
                            status=status.HTTP_403_FORBIDDEN)
        return Response(ReclamationSeanceSerializer(
            ReclamationSeance.objects.filter(prof_id=prof.pk), many=True).data)

    def post(self, request):
        prof = self._prof(request)
        if prof is None:
            return Response({'detail': 'Profil enseignant introuvable.'},
                            status=status.HTTP_403_FORBIDDEN)

        pointage_id = request.data.get('pointage')
        motif = (request.data.get('motif') or '').strip()
        if not pointage_id:
            return Response({'detail': 'La séance est obligatoire.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not motif:
            return Response({'detail': 'Le motif est obligatoire — c’est lui '
                                       'que la direction lira.'},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            pointage = (SuiviePointage.objects
                        .select_related('em', 'jour_fk', 'creneau_fk')
                        .get(pk=pointage_id))
        except SuiviePointage.DoesNotExist:
            return Response({'detail': 'Séance introuvable.'},
                            status=status.HTTP_404_NOT_FOUND)

        # On ne réclame que SES séances : sans ce contrôle, un identifiant deviné
        # ouvrirait le suivi de tout l'établissement.
        if pointage.prof_id != prof.pk:
            return Response({'detail': 'Cette séance ne vous appartient pas.'},
                            status=status.HTTP_403_FORBIDDEN)
        if (pointage.commentaire or '').strip().lower() != COMMENTAIRE_CONTESTABLE:
            return Response({'detail': 'Seule une séance marquée « Non fait » '
                                       'peut être réclamée.'},
                            status=status.HTTP_400_BAD_REQUEST)

        # Anti-doublon : une seule réclamation en attente par séance. Deux clics
        # sur un réseau lent ne doivent pas produire deux dossiers à traiter.
        if ReclamationSeance.objects.filter(
                prof_id=prof.pk, pointage_id=pointage.pk,
                statut='en_attente').exists():
            return Response({'detail': 'Une réclamation est déjà en attente '
                                       'sur cette séance.'},
                            status=status.HTTP_400_BAD_REQUEST)

        reclamation = ReclamationSeance.objects.create(
            prof_id=prof.pk,
            prof_nom=getattr(prof, 'nom', '') or '',
            user_id=request.user.pk,
            annee_universitaire=pointage.annee_universitaire or '',
            type_semestre=pointage.type_semestre or '',
            numero_semaine=pointage.numero_semaine or 0,
            em_id=pointage.em_id,
            jour=getattr(pointage.jour_fk, 'jour', '') or '',
            creneau=getattr(pointage.creneau_fk, 'creneau', '') or '',
            pointage_id=pointage.pk,
            institution_id=getattr(pointage, 'institution_id', None),
            em_code=getattr(pointage.em, 'code_em', '') or '',
            em_intitule=getattr(pointage.em, 'intitule', '') or '',
            date_seance=pointage.date_suivie,
            commentaire_origine=pointage.commentaire or 'Non fait',
            motif=motif,
        )
        return Response(ReclamationSeanceSerializer(reclamation).data,
                        status=status.HTTP_201_CREATED)


class ReclamationSeanceAdminViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Côté direction : lire les réclamations et les trancher.

      * `GET  /api/v1/reclamations/seances/?statut=en_attente`
      * `POST /api/v1/reclamations/seances/{id}/traiter/` — `{statut, reponse}`

    En lecture seule pour le reste : une réclamation ne se modifie pas, elle se
    traite. Ce que la direction accepte doit ensuite être corrigé dans le suivi,
    en local — le miroir ne décide pas d'un pointage.
    """
    serializer_class   = ReclamationSeanceSerializer
    permission_classes = [IsAdminOrIT]
    queryset           = ReclamationSeance.objects.all()

    def get_queryset(self):
        requete = super().get_queryset()
        statut = self.request.query_params.get('statut')
        if statut:
            requete = requete.filter(statut=statut)
        prof_id = self.request.query_params.get('prof_id')
        if prof_id:
            requete = requete.filter(prof_id=prof_id)
        return requete

    @action(detail=True, methods=['post'], url_path='traiter')
    def traiter(self, request, pk=None):
        reclamation = self.get_object()
        statut = request.data.get('statut')
        if statut not in ('acceptee', 'rejetee'):
            return Response({'detail': 'Le statut doit valoir « acceptee » ou '
                                       '« rejetee ».'},
                            status=status.HTTP_400_BAD_REQUEST)

        reclamation.statut          = statut
        reclamation.reponse         = (request.data.get('reponse') or '').strip()
        reclamation.traite_par_id   = request.user.pk
        reclamation.traite_par_nom  = (getattr(request.user, 'name', '')
                                       or request.user.get_username())
        reclamation.date_traitement = timezone.now()
        reclamation.save(update_fields=['statut', 'reponse', 'traite_par_id',
                                        'traite_par_nom', 'date_traitement'])
        return Response(ReclamationSeanceSerializer(reclamation).data)
