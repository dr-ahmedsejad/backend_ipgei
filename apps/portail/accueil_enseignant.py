"""
GET /api/v1/portail/enseignant/accueil/ — l'accueil de l'app enseignant en UNE
requête au lieu de six : semaines de l'enseignant, sa grille de la semaine en
cours et de la suivante (la prochaine séance, le samedi), réclamations des
étudiants, notifications non lues.

Chaque partie est EXACTEMENT la réponse de son adresse habituelle : la même vue
est appelée (sous-requête, mêmes droits, mêmes paramètres) ; l'app la lit avec
le même code. Une partie en échec vaut null sans empêcher les autres. Les
feuilles de notes ouvertes (règles propres à l'app) restent à part.

Paramètres : ceux de l'emploi du temps de l'enseignant (`annee_universitaire`,
`type_semestre`, `prof`) — l'année et les semestres EN COURS.
"""
import copy
import logging
from datetime import datetime

from django.http import QueryDict
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsEnseignant

logger = logging.getLogger('siga')


def _sous_requete(request, vue, params):
    """Appelle `vue` comme si l'app l'avait demandée avec ces paramètres
    (même utilisateur, mêmes cookies) ; retourne la Response, ou None."""
    brute = copy.copy(request._request)
    q = QueryDict(mutable=True)
    for k, v in params.items():
        if v not in (None, ''):
            q[k] = str(v)
    brute.GET = q
    try:
        r = vue(brute)
        return r if r.status_code == 200 else None
    except Exception:
        logger.warning('portail/enseignant/accueil : sous-requête en échec', exc_info=True)
        return None


def _lire_date(texte):
    try:
        return datetime.strptime(texte or '', '%d/%m/%Y').date()
    except ValueError:
        return None


def semaine_du_jour(semaines, dates, aujourdhui=None):
    """La semaine qui contient aujourd'hui, sinon la dernière commencée, sinon
    la dernière — la règle de l'app (ApiEnseignant.semaineDuJour)."""
    if not semaines:
        return None
    jour = aujourdhui or timezone.localdate()
    commencee = None
    for n in sorted(semaines):
        d = dates.get(n) or dates.get(str(n)) or {}
        debut, fin = _lire_date(d.get('debut')), _lire_date(d.get('fin'))
        if debut and fin and debut <= jour <= fin:
            return n
        if debut and debut <= jour:
            commencee = n
    return commencee if commencee is not None else sorted(semaines)[-1]


class AccueilEnseignantView(APIView):
    permission_classes = [IsEnseignant]

    def get(self, request):
        from apps.notifications.views import NotificationViewSet
        from apps.reclamations.views import ReclamationAdminViewSet
        from apps.suivi.views import SuiviePointageViewSet

        ctx = {k: request.query_params.get(k) for k in ('annee_universitaire', 'type_semestre', 'prof')}
        semaines_vue = SuiviePointageViewSet.as_view({'get': 'semaines'})
        grille_vue = SuiviePointageViewSet.as_view({'get': 'grille'})

        r = _sous_requete(request, semaines_vue, ctx)
        semaines = r.data if r is not None else None
        numero = suivant = grille = grille_suivante = None
        if semaines:
            liste = sorted(semaines.get('semaines') or [])
            numero = semaine_du_jour(liste, semaines.get('semaines_dates') or {})
            if numero is not None:
                r = _sous_requete(request, grille_vue, {**ctx, 'numero_semaine': numero})
                grille = r.data if r is not None else None
                suivant = next((s for s in liste if s > numero), None)
                if suivant is not None:
                    r = _sous_requete(request, grille_vue, {**ctx, 'numero_semaine': suivant})
                    grille_suivante = r.data if r is not None else None

        r = _sous_requete(request, ReclamationAdminViewSet.as_view({'get': 'pour_enseignant'}),
                          {'annee_universitaire': ctx['annee_universitaire']})
        reclamations = r.data if r is not None else None
        r = _sous_requete(request, NotificationViewSet.as_view({'get': 'unread_count'}), {})
        non_lues = r.data.get('count') if r is not None else None

        return Response({
            'semaines': semaines,
            'numero': numero,
            'grille': grille,
            'numero_suivant': suivant,
            'grille_suivante': grille_suivante,
            'reclamations': reclamations,
            'non_lues': non_lues,
        })
