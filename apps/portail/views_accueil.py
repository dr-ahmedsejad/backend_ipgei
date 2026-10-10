"""
GET /api/v1/portail/accueil/ — l'accueil de l'app mobile en UNE requête.

L'accueil demandait sept réponses à l'ouverture (notes, absences, fiche,
emploi du temps, non lues, dernière notification, périodes de réclamation) :
sept allers-retours sur le réseau mobile, sept passages par l'authentification
— multipliés par mille étudiants lors d'un pic. Ici, une seule requête (même
point d'accès qu'à l'ISS ; l'app s'en sert dès qu'il existe).

Chaque partie est EXACTEMENT la réponse de sa vue habituelle (mêmes vues
appelées, même format) : l'app la lit avec le même code. Une partie en échec
vaut `null` sans empêcher les autres (comme l'app le faisait déjà).

`?annee=` (année consultée) a le même sens que pour les notes et absences.
"""
import logging

from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsEtudiant

logger = logging.getLogger('siga')


def _partie(nom, calcul):
    try:
        r = calcul()
        if getattr(r, 'status_code', 200) != 200:
            return None
        return r.data if hasattr(r, 'data') else r
    except Exception:
        logger.warning('portail/accueil : partie « %s » indisponible', nom, exc_info=True)
        return None


def _notifications(request):
    """Non lues et dernière notification, avec les règles de la cloche : le
    compte est celui de /notifications/unread-count/, et « lue » veut dire
    « absente des non lues » (ce que fait l'app avec `?lue=false`)."""
    from apps.notifications.serializers import NotificationSerializer
    from apps.notifications.views import NotificationViewSet

    vue = NotificationViewSet()
    vue.request = request
    vue.format_kwarg = None
    if hasattr(vue, '_non_lues'):
        non_lues = vue._non_lues()                       # miroir : lecture en ligne comprise
    else:
        non_lues = vue.get_queryset().filter(lue=False)
    derniere = vue.get_queryset().order_by('-created_at').first()
    if derniere is None:
        return non_lues.count(), []
    donnees = dict(NotificationSerializer(derniere, context={'request': request}).data)
    donnees['lue'] = not non_lues.filter(pk=derniere.pk).exists()
    return non_lues.count(), [donnees]


class AccueilView(APIView):
    permission_classes = [IsEtudiant]

    def get(self, request):
        from .serializers import ProfilEtudiantSerializer
        from .views import (MesAbsencesView, MesNotesView, MonEmploiView,
                            PeriodesReclamationActivesView, _get_etudiant)

        etudiant = _get_etudiant(request)
        notifs = _partie('notifications', lambda: _notifications(request)) or (None, None)
        return Response({
            'notes':         _partie('notes', lambda: MesNotesView().get(request)),
            'absences':      _partie('absences', lambda: MesAbsencesView().get(request)),
            'profil':        _partie('profil', lambda: ProfilEtudiantSerializer(
                                 etudiant, context={'request': request}).data),
            'emploi':        _partie('emploi', lambda: MonEmploiView().get(request)),
            'periodes':      _partie('periodes', lambda: PeriodesReclamationActivesView().get(request)),
            'non_lues':      notifs[0],
            'notifications': notifs[1],
        })
