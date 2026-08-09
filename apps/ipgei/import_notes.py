"""
Import des notes depuis un classeur Excel.

Ce module lit EXACTEMENT la fiche que `collecte.generer_excel` produit : même
en-tête, mêmes colonnes, une feuille par matière. L'enseignant télécharge sa
fiche, remplit la colonne « Note /20 », renvoie le fichier — et la scolarité le
verse. C'est le tour complet de la fiche de collecte, et non un format de plus
à documenter.

Trois principes commandent le reste :

  * **La matière se LIT dans la feuille**, dans le bloc d'identification, et
    n'est pas demandée à l'agent. Un classeur porte souvent six matières :
    la faire choisir six fois inviterait à se tromper, et une note versée dans
    la mauvaise matière ne se voit pas.
  * **Une ligne fautive n'interrompt pas les autres, mais son motif est rendu.**
    Un import silencieusement partiel est pire qu'un import refusé : on croit
    la classe saisie.
  * **Une cellule vide ne touche à rien.** L'enseignant qui n'a corrigé que la
    moitié de sa pile renvoie la feuille sans effacer le reste. Pour retirer
    une note, on passe par la grille — l'effacement se décide, il ne se déduit
    pas d'une case laissée blanche.

Repris en lecture du dispositif SIGA (`evaluations/services/note_saisie.py`) :
plafonds de taille, index des inscrits en une requête, écriture groupée puis
recalcul. Transposé aux objets IPGEI — `Matiere` au lieu d'`EM`, `Note` porteuse
de ses `Evaluation`, et le calcul confié au service du domaine.
"""
import logging
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction

from . import services
from .collecte import (
    LIBELLES_TYPE, LIGNES_ORDRE, TYPE_DS, TYPE_EXAM, TYPE_RATT, TYPE_TP,
    TYPES_NOTE, campagne_du_type,
)
from .models import AnonymatIPGEI, Evaluation, InscriptionIPGEI, Matiere, Note

logger = logging.getLogger('siga')

#: Plafonds — un classeur de collecte tient en quelques dizaines de kilo-octets.
#: Au-delà, ce n'est plus une fiche de collecte, et mieux vaut le dire que de
#: laisser le serveur peiner sur un fichier qui n'est pas celui qu'on croit.
TAILLE_MAX_MO = 5
LIGNES_MAX    = 5000

#: En-têtes de la colonne d'identification, selon le mode de la fiche.
COLONNE_MATRICULE = 'matricule'
COLONNE_ANONYMAT  = "n° anonymat"
#: Variantes tolérées : la fiche voyage, on la rouvre dans LibreOffice, on
#: recopie l'en-tête à la main. Le « n° » est ce qui se déforme le plus.
ALIAS_ANONYMAT = ('n° anonymat', 'n°anonymat', 'no anonymat', 'anonymat',
                  'numero anonymat', 'numéro anonymat')
ALIAS_NOTE = ('note /20', 'note', 'note/20', 'note sur 20')


def _texte(valeur) -> str:
    return '' if valeur is None else str(valeur).strip()


def _matricule_normalise(brut) -> str:
    """
    Matricule lu d'une cellule Excel.

    Excel range volontiers « 27001 » en nombre : il revient en `27001.0`, qui ne
    correspond à aucun étudiant. Le rattraper ici évite une liste d'erreurs
    entière pour une raison qui n'a rien de métier.
    """
    if brut is None:
        return ''
    if isinstance(brut, float) and brut.is_integer():
        return str(int(brut))
    texte = str(brut).strip()
    if texte.endswith('.0') and texte[:-2].isdigit():
        texte = texte[:-2]
    return texte


def _valeur_note(brut):
    """
    Note lue d'une cellule : `Decimal`, ou `None` si la case est vide.

    Lève `ValueError` si la cellule contient quelque chose qui n'est pas une
    note — on ne devine pas, on le dit. La virgule décimale est admise : c'est
    ainsi qu'un clavier français la produit.
    """
    if brut is None or (isinstance(brut, str) and not brut.strip()):
        return None
    try:
        valeur = Decimal(str(brut).strip().replace(',', '.'))
    except (InvalidOperation, ValueError):
        raise ValueError(f'« {brut} » n\'est pas une note.')
    if not (Decimal('0') <= valeur <= Decimal('20')):
        raise ValueError(f'note {valeur} hors de [0 - 20].')
    return valeur


def _lire_feuille(feuille) -> dict:
    """
    Repère, dans une feuille de collecte, la matière et la ligne d'en-tête.

    L'en-tête du tableau n'est PAS en première ligne : la fiche porte au-dessus
    le bandeau de l'établissement, son titre et un bloc d'identification. On
    cherche donc la ligne dont la première cellule annonce la colonne des
    étudiants, et l'on relève au passage l'intitulé de la matière.

    Renvoie `{intitule, ligne_entete, colonnes, anonymat}`, ou `None` si la
    feuille n'est pas une fiche de collecte — un onglet de brouillon ajouté par
    l'enseignant ne doit pas faire échouer le classeur entier.
    """
    intitule = ''
    for numero, ligne in enumerate(
        feuille.iter_rows(min_row=1, max_row=60, values_only=True), start=1,
    ):
        cellules = [_texte(c) for c in ligne]
        if not cellules:
            continue

        premiere = cellules[0].lower()
        if premiere.rstrip(' :').lower() == 'matière' and len(cellules) > 1:
            intitule = cellules[1]
            continue

        if premiere in ALIAS_ANONYMAT or premiere == COLONNE_MATRICULE:
            colonnes = {texte.lower(): index
                        for index, texte in enumerate(cellules) if texte}
            note = next((c for c in ALIAS_NOTE if c in colonnes), None)
            if note is None:
                return None
            return {
                'intitule':     intitule,
                'ligne_entete': numero,
                'colonne_cle':  0,
                'colonne_note': colonnes[note],
                'anonymat':     premiere != COLONNE_MATRICULE,
            }
    return None


def _index_inscriptions(classe, semestre) -> dict:
    """matricule → inscription, en une requête."""
    return {
        i.etudiant.matricule: i
        for i in InscriptionIPGEI.objects
        .filter(classe=classe, actif=True,
                annee_universitaire=semestre.annee_universitaire)
        .select_related('etudiant')
        .order_by(*LIGNES_ORDRE)
    }


def _index_anonymats(session) -> dict:
    """
    Numéro d'anonymat → inscription, pour toute la campagne.

    Le numéro vaut pour la SESSION et non par matière : un numéro par épreuve
    obligerait à redistribuer les copies à chaque correction. L'index se
    construit donc une fois, quelles que soient les matières du classeur.
    """
    return {
        str(a.numero): a.inscription
        for a in AnonymatIPGEI.objects
        .filter(session=session)
        .select_related('inscription__etudiant')
    }


def importer_classeur(fichier, *, semestre, classe, type_note: str, numero: int = 1,
                      matiere=None, utilisateur=None) -> dict:
    """
    Verse les notes d'un classeur de collecte.

    `type_note` et `numero` disent QUELLE épreuve on remplit : ils viennent de
    l'écran, pas du fichier. Une fiche de devoir surveillé rouverte pour saisir
    l'examen resterait sinon versée dans le devoir, sans que rien ne l'annonce.

    `matiere` restreint la fournée à un seul onglet. Laissée vide — le cas
    courant —, toutes les fiches du classeur sont versées : c'est ainsi qu'on
    reporte un semestre entier d'un fichier. La renseigner sert à ne verser
    qu'une matière d'un classeur qui en porte six, sans le découper.

    Renvoie un bilan : `{matieres, saisies, ignorees, erreurs}`. `ignorees`
    compte les cases laissées vides — c'est le compte qui dit à l'agent qu'une
    pile n'est corrigée qu'à moitié.
    """
    import openpyxl

    if type_note not in TYPES_NOTE:
        raise ValidationError({'type_note': f'Type d\'épreuve inconnu : {type_note}.'})
    if fichier.size > TAILLE_MAX_MO * 1024 * 1024:
        raise ValidationError(
            {'fichier': f'Fichier trop volumineux (maximum {TAILLE_MAX_MO} Mo).'})

    rattrapage = type_note == TYPE_RATT
    session = services.controler_saisie(semestre, rattrapage=rattrapage)
    plafond = services.plafond_effectif(semestre)

    try:
        classeur = openpyxl.load_workbook(fichier, read_only=True, data_only=True)
    except Exception as exc:                                   # noqa: BLE001
        raise ValidationError({'fichier': f'Fichier Excel invalide : {exc}'})

    inscriptions = _index_inscriptions(classe, semestre)
    if not inscriptions:
        raise ValidationError(
            f"{classe.nom} n'a aucun inscrit actif en {semestre.annee_universitaire}.")

    # L'épreuve visée. Le TP n'est pas une évaluation datée mais une note portée
    # sur la ligne : il n'a ni type d'évaluation ni numéro.
    type_evaluation = None
    if type_note in (TYPE_EXAM, TYPE_RATT):
        type_evaluation = Evaluation.TYPE_EXAM
    elif type_note == TYPE_DS:
        type_evaluation = Evaluation.TYPE_DS

    # L'index des numéros se construit une fois : ils valent pour la session,
    # pas par matière.
    anonymats = _index_anonymats(session)

    bilan = {'matieres': [], 'saisies': 0, 'ignorees': 0, 'erreurs': []}
    touchees = []

    for feuille in classeur.worksheets:
        plan = _lire_feuille(feuille)
        if plan is None:
            continue
        if feuille.max_row and feuille.max_row > LIGNES_MAX:
            bilan['erreurs'].append({
                'feuille': feuille.title, 'ligne': 0,
                'motif': f'Trop de lignes (maximum {LIGNES_MAX}).'})
            continue

        trouvee = Matiere.objects.filter(
            intitule=plan['intitule'], code_semestre=semestre.code,
            niveau_ref__code=classe.niveau_code, actif=True,
        ).first()
        # Matière imposée : les autres onglets du classeur ne sont pas des
        # erreurs, ils ne sont simplement pas demandés. Les signaler noierait
        # le bilan sous cinq lignes pour un import qui s'est bien passé.
        #
        # `trouvee` et non `matiere` dans tout ce qui suit : écraser le
        # paramètre revenait à imposer, dès le deuxième onglet, la matière du
        # premier — un classeur de six matières n'en versait qu'une.
        if matiere is not None and (trouvee is None or trouvee.pk != matiere.pk):
            continue
        if trouvee is None:
            bilan['erreurs'].append({
                'feuille': feuille.title, 'ligne': plan['ligne_entete'],
                'motif': (f'Matière « {plan["intitule"] or "?"} » introuvable au '
                          f'programme de {classe.niveau_code} en {semestre.code}.')})
            continue

        if plan['anonymat'] and not anonymats:
            bilan['erreurs'].append({
                'feuille': feuille.title, 'ligne': plan['ligne_entete'],
                'motif': "Fiche anonyme, mais aucun numéro n'a été tiré pour "
                         f'{session.get_type_session_display().lower()} de '
                         f'{semestre.code} : le tirage se fait avant la correction.'})
            continue

        saisies = ignorees = 0
        with transaction.atomic():
            for numero_ligne, ligne in enumerate(
                feuille.iter_rows(min_row=plan['ligne_entete'] + 1, values_only=True),
                start=plan['ligne_entete'] + 1,
            ):
                cle = _texte(ligne[plan['colonne_cle']]) if ligne else ''
                if not cle:
                    # Le tableau s'arrête à la première ligne vide. Ce qui suit
                    # n'est pas de la donnée : la fiche porte en dessous la note
                    # de seuil et le bloc de signature, que « continuer » lisait
                    # comme des étudiants — « Matricule "Signature de
                    # l'enseignant" inconnu » figurait dans le bilan de CHAQUE
                    # import.
                    break
                cle = _matricule_normalise(ligne[plan['colonne_cle']])

                inscription = (anonymats.get(cle) if plan['anonymat']
                               else inscriptions.get(cle))
                if inscription is None:
                    quoi = 'Numéro d\'anonymat' if plan['anonymat'] else 'Matricule'
                    bilan['erreurs'].append({
                        'feuille': feuille.title, 'ligne': numero_ligne,
                        'motif': f'{quoi} « {cle} » inconnu dans {classe.nom}.'})
                    continue

                brut = (ligne[plan['colonne_note']]
                        if plan['colonne_note'] < len(ligne) else None)
                try:
                    valeur = _valeur_note(brut)
                except ValueError as exc:
                    bilan['erreurs'].append({
                        'feuille': feuille.title, 'ligne': numero_ligne,
                        'motif': f'{cle} : {exc}'})
                    continue
                if valeur is None:
                    # Case vide : on ne touche pas à ce qui est déjà là.
                    ignorees += 1
                    continue

                note, _ = Note.objects.get_or_create(
                    inscription=inscription, matiere=trouvee, semestre=semestre)
                if note.verrouillee:
                    bilan['erreurs'].append({
                        'feuille': feuille.title, 'ligne': numero_ligne,
                        'motif': f'{cle} : note verrouillée par une délibération validée.'})
                    continue

                if type_note == TYPE_TP:
                    note.note_tp = valeur
                    note.save(update_fields=['note_tp'])
                else:
                    Evaluation.objects.update_or_create(
                        note=note, type_evaluation=type_evaluation, numero=numero,
                        defaults={'valeur': valeur, 'saisie_par': utilisateur},
                    )
                services.calculer_note(note, plafond_rattrapage=plafond)
                saisies += 1

        bilan['matieres'].append({
            'feuille':  feuille.title,
            'matiere':  trouvee.intitule,
            'saisies':  saisies,
            'ignorees': ignorees,
        })
        bilan['saisies']  += saisies
        bilan['ignorees'] += ignorees
        touchees.append(trouvee.intitule)

    if not bilan['matieres'] and not bilan['erreurs'] and matiere is not None:
        raise ValidationError(
            {'fichier': f'Ce classeur ne porte aucune fiche de « {matiere.intitule} ». '
                        'Vérifiez la matière choisie, ou laissez « Toutes les '
                        'matières » pour verser le classeur entier.'})
    if not bilan['matieres'] and not bilan['erreurs']:
        raise ValidationError(
            {'fichier': 'Aucune fiche de collecte reconnue dans ce classeur. '
                        'Attendu : une colonne « Matricule » (ou « N° anonymat ») '
                        'et une colonne « Note /20 », comme sur la fiche éditée '
                        'depuis Notes → Fiches de collecte.'})

    logger.info(
        'Import notes IPGEI : %s note(s) sur %s, %s matière(s), %s erreur(s) — %s',
        bilan['saisies'], LIBELLES_TYPE.get(type_note, type_note),
        len(bilan['matieres']), len(bilan['erreurs']), ', '.join(touchees))
    return bilan
