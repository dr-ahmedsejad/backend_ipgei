"""
Fiche d'émargement d'une épreuve — la feuille que le surveillant fait signer.

Elle ne porte aucune note : le candidat émarge en entrant et en sortant, et
c'est tout. D'où ses deux colonnes vides, `Entrée` et `Sortie`, là où la fiche
de collecte porte `Note /20` et `Observations`.

La liste des candidats vient de `collecte.lignes` — MÊME ordre, MÊME filtre de
rattrapage. C'est délibéré : le surveillant appelle les noms pendant que la
scolarité tient la fiche de collecte, et deux ordres différents décaleraient
l'appel. En seconde session, la fiche ne porte donc que les étudiants qui
doivent la matière.

`Groupe d'examen` et `Salle` ne sont pas des données du modèle : l'IPGEI répartit
ses candidats à la main, salle par salle, le jour de l'épreuve. Ils sont donc
saisis au moment de l'édition et se contentent d'être imprimés — les modéliser
supposerait une répartition persistée qui n'existe pas.
"""
import random
from datetime import date

from django.core.exceptions import ValidationError

from core.pdf_renderer import render_pdf_bytes
from core.pdf_utils import get_institution_context

from . import collecte, services

TEMPLATE = 'ipgei/fiche_emargement.html'

#: Mêmes marges que la fiche de collecte : les deux feuilles circulent
#: ensemble, un cadrage différent se verrait.
OPTIONS_PDF = dict(collecte.OPTIONS_PDF)


def places_retenues(salle, demandee=None) -> int:
    """
    Les places qu'on ouvre dans cette salle : celle de la base, ou moins.

    La capacité du référentiel est la capacité PHYSIQUE de la salle. Le jour
    d'une épreuve on en ouvre parfois moins — un rang condamné, un espacement
    voulu entre candidats, une table réservée au surveillant. L'agent peut donc
    abaisser le nombre ; il ne peut pas le monter, sinon la fiche promettrait
    des sièges qui n'existent pas.
    """
    if not salle.capacite:
        raise ValidationError(
            f"La salle {salle.nom} n'a pas de capacité renseignée : "
            "impossible de lui affecter des candidats.")
    if demandee is None:
        return salle.capacite
    if demandee < 1:
        raise ValidationError(
            f'{salle.nom} : le nombre de places doit être au moins 1.')
    if demandee > salle.capacite:
        raise ValidationError(
            f'{salle.nom} ne compte que {salle.capacite} places : impossible '
            f"d'en ouvrir {demandee}. La capacité se corrige au référentiel "
            'des salles, pas ici.')
    return demandee


def repartir(lignes: list, retenues: list) -> list:
    """
    Répartit les candidats entre les salles retenues, au prorata des places.

    `retenues` : une liste de `{'salle': Salle, 'capacite': int}` — la capacité
    EFFECTIVEMENT ouverte, qui vaut celle du référentiel ou moins (voir
    `places_retenues`).

    Chaque salle cochée est une salle qu'on OUVRE : la remplir à ras avant de
    passer à la suivante mobiliserait un surveillant pour cinq candidats pendant
    que la première déborde. On répartit donc proportionnellement — la grande
    salle prend plus de monde que la petite, et personne n'est serré.

    La liste arrive déjà ordonnée et l'ordre est conservé : deux éditions de la
    même épreuve donnent la MÊME répartition. Sans cela, un candidat changerait
    de salle d'une impression à l'autre.

    Aucune salle ne dépasse jamais les places qu'on lui a ouvertes. Si elles
    manquent, on lève plutôt que de tronquer : une fiche amputée en silence
    laisserait des candidats sans siège le jour de l'épreuve, et rien ne le
    signalerait.
    """
    places = [r['capacite'] for r in retenues]
    total_places = sum(places)
    effectif = len(lignes)
    if effectif > total_places:
        manque = effectif - total_places
        raise ValidationError(
            f'{effectif} candidats pour {total_places} places : {manque} '
            f'candidat{"s" if manque > 1 else ""} sans siège. Ajoutez une salle '
            "ou rendez les places que vous avez retirées.")

    # Quotas au prorata : partie entière, puis les sièges restants aux plus
    # forts restes — sans quoi la somme des arrondis ne retomberait pas sur
    # l'effectif, et il manquerait ou surgirait une ligne.
    exacts = [effectif * p / total_places for p in places]
    quotas = [int(q) for q in exacts]
    restants = effectif - sum(quotas)
    ordre_restes = sorted(range(len(retenues)),
                          key=lambda i: (exacts[i] - quotas[i]), reverse=True)
    for i in ordre_restes[:restants]:
        quotas[i] += 1

    groupes, debut = [], 0
    for rang, (retenue, quota) in enumerate(zip(retenues, quotas), start=1):
        groupes.append({
            'salle':    retenue['salle'],
            'capacite': retenue['capacite'],
            'groupe':   f'G{rang}',
            'lignes':   lignes[debut:debut + quota],
        })
        debut += quota
    return groupes


def candidats(semestre, classes: list, matiere, type_note: str) -> tuple:
    """
    Les candidats d'une épreuve, sur une classe ou sur plusieurs.

    UNE classe : l'ordre de la fiche de collecte, à l'identique — le surveillant
    appelle les noms pendant que la scolarité tient la collecte.

    PLUSIEURS classes : à l'examen, la scolarité mélange les classes pour que
    des camarades ne composent pas côte à côte. Les listes sont alors BRASSÉES,
    et non concaténées : mises bout à bout, elles auraient rendu les blocs par
    classe, ce que le mélange cherche justement à défaire.

    Le brassage est reproductible. Une fiche se réimprime — la veille, puis le
    matin parce qu'une salle a changé —, et un tirage refait à chaque édition
    ferait passer le même candidat d'une salle à l'autre entre deux impressions.
    La graine est donc tirée de l'ÉPREUVE (semestre, matière, type, classes) et
    d'elle seule : elle ne dépend ni des salles retenues, ni de la date, si bien
    que rééditer après avoir changé de salle ne rebrasse rien.

    Rend `(lignes, seuil, melange)`. `melange` dit si la colonne « Classe » a
    lieu d'être : sur une classe unique elle répéterait le cartouche.
    """
    lignes, seuil = [], None
    for classe in classes:
        contenu, seuil_classe = collecte.lignes(
            semestre, classe.pk, matiere, type_note, anonymat=False)
        # Le seuil de rattrapage est celui du paramétrage, commun aux classes
        # d'un même niveau : le retenir une fois suffit.
        seuil = seuil if seuil is not None else seuil_classe
        for ligne in contenu:
            lignes.append({**ligne, 'classe': classe.nom})

    melange = len(classes) > 1
    if melange:
        graine = '|'.join([
            str(semestre.pk), str(matiere.pk), type_note,
            ','.join(str(c.pk) for c in classes),
        ])
        random.Random(graine).shuffle(lignes)
    return lignes, seuil, melange


def _fiches(semestre, classes: list, matiere, type_note: str,
            retenues: list, groupe: str, salle_libre: str) -> list:
    """
    Les fiches d'une matière : une par salle retenue, sinon une seule.

    Anonymat toujours faux : une fiche d'émargement se signe sous son nom, c'est
    justement la pièce qui rattache un candidat présent à sa copie.
    """
    contenu, seuil, melange = candidats(semestre, classes, matiere, type_note)

    commun = {'matiere': matiere, 'seuil': seuil, 'melange': melange,
              'effectif_matiere': len(contenu)}

    if not retenues:
        # Sans salles choisies, le groupe et la salle restent des mentions
        # libres — celles que l'utilisateur tape, ou un blanc à remplir au stylo.
        return [{**commun, 'lignes': contenu, 'effectif': len(contenu),
                 'groupe': groupe, 'salle_nom': salle_libre, 'salle': None}]

    # Une matière sans candidat ne justifie pas d'ouvrir des salles : une seule
    # fiche portant la mention « aucun étudiant » suffit, là où trois feuilles
    # vides feraient croire à trois salles réservées pour personne.
    if not contenu:
        return [{**commun, 'lignes': [], 'effectif': 0,
                 'groupe': '', 'salle_nom': '', 'salle': None}]

    return [
        {**commun,
         'lignes':    part['lignes'],
         'effectif':  len(part['lignes']),
         'groupe':    part['groupe'],
         'salle':     part['salle'],
         'salle_nom': part['salle'].nom,
         # Les places OUVERTES, qui valent celles du referentiel ou moins. La
         # fiche imprime celles-ci : c'est le nombre de sieges que le
         # surveillant trouvera dans la salle.
         'capacite':  part['capacite'],
         'capacite_salle': part['salle'].capacite}
        for part in repartir(contenu, retenues)
    ]


def contexte(semestre, classes: list, matieres: list, type_note: str,
             groupe: str = '', salle: str = '', retenues: list = None) -> dict:
    """
    Contexte de rendu — une fiche par matière, et par salle quand on en retient.

    Les matières gardent l'ordre de la maquette, les salles celui du choix.
    """
    retenues = list(retenues or [])
    classes = list(classes)
    fiches = []
    for matiere in matieres:
        fiches.extend(_fiches(semestre, classes, matiere, type_note,
                              retenues, groupe, salle))
    donnees = get_institution_context()
    donnees.update({
        'fiches':        fiches,
        'classe':        classes[0],
        'classes':       classes,
        # Une seule classe : son libelle complet, comme avant. Plusieurs : leurs
        # noms courts, le cursus etant commun puisqu'elles partagent le niveau.
        'classe_label':  (services.libelle_classe(classes[0]) if len(classes) == 1
                          else ', '.join(c.nom for c in classes)),
        'semestre':      semestre,
        'annee_universitaire': semestre.annee_universitaire,
        'type_note':     type_note,
        'type_note_label': collecte.libelle_type_note(type_note),
        'rattrapage':    type_note == collecte.TYPE_RATT,
        'groupe':        (groupe or '').strip(),
        'salle':         (salle or '').strip(),
        'date_edition':  date.today().strftime('%d/%m/%Y'),
    })
    return donnees


def generer_pdf(semestre, classes: list, matieres: list, type_note: str,
                groupe: str = '', salle: str = '', retenues: list = None) -> bytes:
    return render_pdf_bytes(
        TEMPLATE,
        contexte(semestre, classes, matieres, type_note, groupe, salle, retenues),
        dict(OPTIONS_PDF))
