"""
Le libellé d'affichage d'un département — filière, niveau, nom.

UN SEUL ENDROIT où cette règle est écrite. Elle vivait en deux copies
littérales, dans `apps/suivi/views.py` et `apps/avancement/views.py`, et elles
ont divergé au premier changement : les fiches de suivi disaient « MPSI A »
quand la charge des permanents disait encore « IPGEI - MPSI - MPSI A ». Même
classe, même règle voulue, deux libellés.

Le dépôt a déjà payé cette leçon côté écran : `lib/format-depts.ts` raconte que
dix pages portaient chacune leur copie avant d'être réunies.

Le pendant TypeScript de ce module est `frontend_ipgei/lib/format-depts.ts` :
les deux appliquent la même règle, et doivent bouger ensemble.

La règle
--------
Format complet : « FILIÈRE - NIVEAU - NOM ». Les départements partageant même
filière ET même niveau se regroupent en « FIL - NIV (G1 / G2) » — cinq classes
d'un même niveau tiennent alors sur une ligne au lieu de cinq.

Deux allègements, commandés par les DONNÉES et non par un choix figé : un
établissement qui se diversifierait les verrait disparaître d'eux-mêmes.

  * le CODE DE FILIÈRE tombe quand l'établissement n'en a qu'une. Il ne
    distingue alors rien et se répète sur chaque ligne ;
  * le NIVEAU tombe quand le nom de la classe le porte déjà. À l'IPGEI les
    classes se nomment d'après leur niveau — « MPSI - MPSI A » le disait deux
    fois.
"""
from collections import defaultdict


def filiere_distingue() -> bool:
    """
    Le code de filière apporte-t-il une information ?

    Non s'il n'y en a qu'une dans tout le référentiel : il est alors constant,
    et une constante répétée à chaque ligne n'apprend rien. C'est le cas de
    l'IPGEI, qui n'a que la sienne.

    Le calcul porte sur le RÉFÉRENTIEL entier, pas sur les lignes affichées —
    sinon le préfixe apparaîtrait et disparaîtrait d'un écran à l'autre selon
    ce que chacun montre.

    Une requête. L'appelant qui boucle sur un lot l'appelle UNE fois et passe
    le résultat à `format_depts_compact` : la question ne dépend pas de la
    ligne, et la poser à chacune ferait une requête par séance.
    """
    from apps.scolarite.models import Filiere

    return (Filiere.objects
            .exclude(code='').exclude(code__isnull=True)
            .values('code').distinct().count() > 1)


def nom_porte_le_niveau(nom: str, niveau: str) -> bool:
    """Le nom de la classe redit-il déjà son niveau ? (« MPSI A » pour MPSI)"""
    if not niveau or not nom:
        return False
    n, v = nom.strip().lower(), niveau.strip().lower()
    return n == v or n.startswith(f'{v} ') or n.startswith(f'{v}-')


def format_depts_compact(porteur, filiere_utile=None) -> str:
    """
    Le libellé des départements d'un `SuiviePointage` ou d'une `Vacation`.

    `porteur` doit exposer une relation `departements`. Elle doit être
    préchargée avec ses filières et niveaux — `prefetch_related(
    'departements__filiere', 'departements__niveau')` — faute de quoi chaque
    ligne coûte deux requêtes de plus.

    `filiere_utile` évite de recompter les filières à chaque ligne ; laissé à
    `None`, il est calculé ici.
    """
    depts = list(porteur.departements.all())
    if not depts:
        return ''
    if filiere_utile is None:
        filiere_utile = filiere_distingue()

    buckets = defaultdict(list)
    for d in depts:
        fil = ((d.filiere.code if d.filiere_id and d.filiere else '')
               if filiere_utile else '')
        niv = d.niveau.niveau if d.niveau_id and d.niveau else ''
        buckets[(fil, niv)].append(d)

    parts = []
    for (fil, niv), grp in buckets.items():
        prefix = ' - '.join(x for x in (fil, niv) if x)
        if len(grp) == 1:
            d = grp[0]
            nom = d.nom or ''
            # Le niveau tombe si le nom le redit déjà.
            niveau_affiche = '' if nom_porte_le_niveau(nom, niv) else niv
            full = ' - '.join(x for x in (fil, niveau_affiche, nom) if x)
            parts.append(full or nom)
        else:
            # Regroupés, les départements ne sont plus nommés que par leur
            # groupe : le préfixe commun porte déjà le reste.
            inner = ' / '.join((d.groupe or d.nom or '') for d in grp)
            parts.append(f'{prefix} ({inner})' if prefix else inner)
    return ' / '.join(parts)
