"""
Le volume de maquette applicable à une année — gel et lecture.

Deux fonctions, et la même règle que partout ailleurs dans ce dépôt : une
valeur PARTICULIÈRE si elle existe, sinon la valeur générale. C'est ce que fait
`ipgei.services.seuil_applicable` pour un seuil de jury et
`ipgei.services.ponderation_applicable` pour une pondération de classe.

Ici la valeur générale est l'EM lui-même — toujours à jour, donc toujours celle
d'aujourd'hui — et la valeur particulière est le volume gelé de l'année
regardée. Les deux exposent `CM`, `TD`, `TP` et `PR` sous les mêmes noms : un
appelant écrit `volumes_applicables(em, annee).CM` sans jamais avoir à savoir
lequel des deux il a en main.
"""
from .models import EM, VolumeAnnuelEM


def volumes_applicables(em, annee_universitaire: str):
    """
    Le volume de maquette qui vaut pour cette année : le gel, sinon l'EM.

    Le repli sur l'EM n'est pas une faiblesse mais le comportement d'avant,
    conservé tel quel : une année jamais gelée — le cursus SIGA d'origine, une
    année dont le suivi n'a pas encore été généré — continue de lire les
    volumes vivants. On ne rend pas un écran vide pour un gel manquant.
    """
    if em is None or not annee_universitaire:
        return em
    fige = VolumeAnnuelEM.objects.filter(
        em=em, annee_universitaire=annee_universitaire,
    ).first()
    return fige or em


def volumes_par_em(annee_universitaire: str) -> dict:
    """
    Tous les gels de l'année, indexés par `em_id` — pour une boucle.

    `volumes_applicables` fait une requête par EM ; sur l'écran d'avancement,
    qui parcourt les 26 matières, cela en ferait 26. Les appelants qui bouclent
    prennent ce dictionnaire une fois et se replient eux-mêmes sur l'EM.
    """
    if not annee_universitaire:
        return {}
    return {
        v.em_id: v
        for v in VolumeAnnuelEM.objects.filter(
            annee_universitaire=annee_universitaire)
    }


def figer_maquette(annee_universitaire: str, *, rafraichir: bool = False) -> int:
    """
    Gèle la maquette IPGEI pour cette année. Rend le nombre de lignes écrites.

    Appelé à la génération du suivi : c'est le moment où l'année commence à
    produire des heures à comparer à un volume, donc le dernier où ce volume est
    encore celui qu'on a voulu. Idempotent — regénérer une semaine ne regèle
    rien, et c'est bien le but : le premier gel est le bon.

    Seuls les EM MIROIRS d'une matière IPGEI sont gelés. Les EM du catalogue
    socle (CHIM, EPS…) n'appartiennent à aucune maquette de prépa ; les geler
    donnerait une histoire à des matières que l'IPGEI n'enseigne pas.

    `rafraichir=True` réécrit un gel existant. C'est un geste délibéré — « la
    maquette de CETTE année était fausse, corrige-la » —, jamais automatique :
    un rafraîchissement silencieux ramènerait exactement le défaut qu'on répare.
    """
    if not annee_universitaire:
        return 0

    ems = EM.objects.filter(matiere_ipgei__isnull=False)
    deja = set(
        VolumeAnnuelEM.objects
        .filter(annee_universitaire=annee_universitaire)
        .values_list('em_id', flat=True)
    )

    if rafraichir:
        for em in ems.filter(id__in=deja):
            VolumeAnnuelEM.objects.filter(
                annee_universitaire=annee_universitaire, em=em,
            ).update(CM=em.CM, TD=em.TD, TP=em.TP, PR=em.PR)

    nouveaux = [
        VolumeAnnuelEM(annee_universitaire=annee_universitaire, em=em,
                       CM=em.CM, TD=em.TD, TP=em.TP, PR=em.PR)
        for em in ems if em.id not in deja
    ]
    if nouveaux:
        VolumeAnnuelEM.objects.bulk_create(nouveaux, ignore_conflicts=True)
    return len(nouveaux)
