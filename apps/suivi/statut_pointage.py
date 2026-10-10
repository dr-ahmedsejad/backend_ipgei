"""
« Non fait » réel ou pointage pas encore fait ?

Le pointage d'une semaine est GÉNÉRÉ avec toutes les séances à « Non fait »
(valeur par défaut) ; le surveillant fait ensuite passer en « Fait » celles qui
ont eu lieu, sans toucher aux autres. Un « Non fait » ne dit donc rien tant que
personne n'a pointé.

Règle : on retient l'heure de chaque pointage (`SuiviePointage.pointe_le`). Une
séance encore « Non fait » est un VRAI « Non fait » si sa grille — même année,
même semaine, au moins un département en commun — a été pointée APRÈS la fin
de la séance : le surveillant l'a vue et l'a laissée « Non fait ». Sinon, elle
est « En attente » de pointage (ni payée ni contestable).
"""
import re
from collections import defaultdict
from datetime import datetime, time

from django.db.models import Max
from django.utils import timezone

FAIT = 'Fait'
NON_FAIT = 'Non fait'
REPORTE = 'Reporté'
EN_ATTENTE = 'En attente'

_HEURE = re.compile(r'(\d{1,2})\s*[:hH]\s*(\d{2})')


def fin_seance(sp):
    """Fin de la séance (date + heure de fin du créneau « 08:00-09:30 »).
    Sans heure lisible : fin de la journée. Sans date : None."""
    if not sp.date_suivie:
        return None
    heures = _HEURE.findall(sp.creneau_fk.creneau) if sp.creneau_fk_id and sp.creneau_fk else []
    if heures:
        h, m = (int(x) for x in heures[-1])
        fin = time(min(h, 23), min(m, 59))
    else:
        fin = time(23, 59, 59)
    return timezone.make_aware(datetime.combine(sp.date_suivie, fin))


def derniers_pointages(pointages):
    """Pour chaque pointage, l'heure du dernier pointage de SA grille (même
    année, même semaine, un département en commun) : {pk: datetime | None}.
    Les départements doivent être préchargés (`prefetch_related('departements')`)."""
    from .models import SuiviePointage

    pointages = list(pointages)
    if not pointages:
        return {}
    depts = {sp.pk: {d.pk for d in sp.departements.all()} for sp in pointages}
    cles = {(sp.annee_universitaire, sp.numero_semaine) for sp in pointages}
    tous_depts = set().union(*depts.values())

    # Dernier pointage par (année, semaine, département).
    par_grille = defaultdict(lambda: None)
    if tous_depts:
        lignes = (SuiviePointage.objects
                  .filter(annee_universitaire__in={a for a, _ in cles},
                          numero_semaine__in={s for _, s in cles},
                          departements__in=tous_depts,
                          pointe_le__isnull=False)
                  .values('annee_universitaire', 'numero_semaine', 'departements')
                  .annotate(dernier=Max('pointe_le')))
        for l in lignes:
            par_grille[(l['annee_universitaire'], l['numero_semaine'], l['departements'])] = l['dernier']

    resultat = {}
    for sp in pointages:
        candidats = [sp.pointe_le] + [
            par_grille[(sp.annee_universitaire, sp.numero_semaine, d)] for d in depts[sp.pk]
        ]
        candidats = [c for c in candidats if c]
        resultat[sp.pk] = max(candidats) if candidats else None
    return resultat


def statut_affiche(sp, dernier_pointage):
    """« Fait », « Reporté », « Non fait » (constaté au pointage) ou « En attente »."""
    commentaire = sp.commentaire or NON_FAIT
    if commentaire != NON_FAIT:
        return commentaire
    fin = fin_seance(sp)
    if fin is None:
        return NON_FAIT  # séance sans date : rien pour en juger, ancien comportement
    return NON_FAIT if dernier_pointage and dernier_pointage >= fin else EN_ATTENTE
