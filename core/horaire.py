"""
L'horaire réel d'une ligne de suivi, quand il déborde de son créneau.

UN SEUL ENDROIT où ce calcul est écrit. Il servait déjà dans le sérialiseur du
pointage ; les fiches de suivi en avaient besoin à leur tour, et les recopier
aurait refait exactement ce qui vient d'être défait pour le libellé d'un
département — deux copies, puis deux libellés pour la même séance.

Le besoin
---------
Une épreuve dure 2h, 3h ou 4h, et sa ligne de suivi est ancrée sur le PREMIER
créneau qu'elle recouvre : le reste de son intervalle n'existe nulle part. Le
créneau seul annonçait donc « 08h00-09h30 » pour un examen qui court jusqu'à
midi — ce que lisaient le surveillant comme le correcteur.

La durée réelle, elle, est portée par `duree_creneau`, que
`ipgei.miroir.corriger_durees_epreuves` rétablit après chaque génération.
"""
from apps.ipgei.models import _bornes_creneau


def horaire_reel(creneau_libelle: str, duree_heures) -> str:
    """
    « 08h00 - 12h00 », ou la chaîne VIDE quand la durée colle au créneau.

    Vide plutôt qu'une répétition : réécrire « 08h00 - 09h30 » sur chaque
    séance ordinaire alourdirait la colonne sans rien apprendre. L'appelant
    retombe alors sur le libellé du créneau.

    Rend vide aussi quand le libellé ne se laisse pas lire : la table `creneau`
    est un référentiel libre partagé avec le socle, et mieux vaut le créneau tel
    quel qu'un horaire inventé.
    """
    if not creneau_libelle or not duree_heures:
        return ''
    bornes = _bornes_creneau(creneau_libelle)
    if bornes is None:
        return ''

    debut, fin_creneau = bornes
    minutes = int(round(float(duree_heures) * 60))
    # Une tolérance d'une minute : `duree_creneau` est un flottant, et 1.5 h ne
    # retombe pas toujours exactement sur 90 minutes.
    if abs((fin_creneau - debut) - minutes) < 1:
        return ''

    fin = min(debut + minutes, 23 * 60 + 59)
    return (f'{debut // 60:02d}h{debut % 60:02d} - '
            f'{fin // 60:02d}h{fin % 60:02d}')


def horaire_affiche(creneau_libelle: str, duree_heures) -> str:
    """Ce qu'une colonne « Créneau » doit montrer : l'horaire réel, ou le créneau."""
    return horaire_reel(creneau_libelle, duree_heures) or (creneau_libelle or '')
