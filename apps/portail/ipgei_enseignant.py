"""
La saisie de notes en ligne, branchée sur le moteur IPGEI.

CE QUE CE MODULE CORRIGE. `apps/reclamations/views_saisie.py` construisait sa
grille avec `evaluations.services.note_lecture.build_feuille`, qui lit
`InscriptionElement`, `SessionEvaluation` et `evaluations.Note` — la couche LMD
que l'IPGEI a remplacée. Ces tables portent **zéro ligne**, face à 5 580
`ipgei.Note`. Un enseignant qui ouvrait l'écran voyait une grille vide, sans la
moindre erreur pour l'en avertir. C'est le défaut corrigé la veille dans
`apps/portail/ipgei_source.py`, non étendu jusqu'ici.

POURQUOI UN BROUILLON, ET PAS LA NOTE DIRECTEMENT. Cet écran sert sur le
MIROIR, où `MirrorReadOnlyMiddleware` refuse toute écriture : le miroir est
restauré depuis le dump du local à chaque publication, et une note officielle
née en ligne y disparaîtrait sans bruit. Les brouillons vivent donc dans
`SaisieNoteEnLigne`, table sans clé étrangère et retirée du dump
(`SYNC_EXCLUDE_TABLE`). La direction exporte, puis saisit en local, où seule la
note fait foi.

LE PÉRIMÈTRE VIENT DE L'EMPLOI DU TEMPS. `services.feuilles_enseignant` lit
`SeanceReelle` : il n'existe pas de table d'affectation à l'IPGEI, et la
planification est de toute façon le seul endroit tenu à jour de qui enseigne
quoi. Conséquence à connaître : un enseignant sans séance planifiée n'a aucune
feuille, et l'écran le lui dit plutôt que de rendre une page vide.
"""
from decimal import Decimal, InvalidOperation

from apps.ipgei.models import (
    Classe, InscriptionIPGEI, Matiere, Note, SemestreIPGEI,
)
from apps.ipgei import services

from apps.reclamations.models import SaisieNoteEnLigne

#: Les types de case qu'un enseignant peut remplir, en vocabulaire IPGEI.
#: Le rattrapage n'y est PAS : il se compose après le jury, et c'est la
#: scolarité qui l'ouvre. Le laisser ici inviterait à le saisir hors campagne.
TYPES_SAISISSABLES = ('ds', 'tp', 'exam')

#: `tp` n'a qu'une case ; `ds` et `exam` sont numérotés 1..N.
TYPES_NUMEROTES = ('ds', 'exam')


def feuilles_de(prof, annee_universitaire: str = ''):
    """Les couples (classe, matière, semestre) que cet enseignant a en charge."""
    if prof is None:
        return []
    return services.feuilles_enseignant(
        prof, annee_universitaire=annee_universitaire)


def peut_saisir(prof, classe_id, matiere_id, semestre_id) -> bool:
    """
    Cet enseignant a-t-il bien CETTE feuille ?

    Le contrôle porte sur le triplet complet et non sur la seule matière : un
    professeur de maths en MPSI A n'a rien à saisir en MP B, et l'écran passe
    ses paramètres en clair dans l'URL.
    """
    cible = (int(classe_id), int(matiere_id), int(semestre_id))
    return any(
        (f['classe'], f['matiere'], f['semestre']) == cible
        for f in feuilles_de(prof)
    )


def _cases(note, nb_ds: int, nb_exam: int) -> dict:
    """Les valeurs OFFICIELLES d'une ligne, indexées par (type, numéro)."""
    valeurs = {}
    for evaluation in note.evaluations.all():
        valeurs[(evaluation.type_evaluation, evaluation.numero)] = evaluation.valeur
    if note.note_tp is not None:
        valeurs[('tp', 1)] = note.note_tp
    return valeurs


def feuille(prof, classe_id, matiere_id, semestre_id) -> dict:
    """
    La grille de saisie, notes officielles et brouillons superposés.

    On NE crée aucune ligne de note manquante, contrairement à la grille de la
    scolarité (`ipgei.views.grille`) : celle-ci ouvre les lignes à la volée, ce
    qui est une écriture — impossible sur le miroir, et de toute façon une
    décision qui n'appartient pas à l'enseignant. L'écran affiche donc les
    inscrits, avec des cases vides là où la scolarité n'a pas encore ouvert la
    matière.

    Chaque case porte `brouillon: True` quand c'est la saisie en ligne qu'on
    lit, et non la note arrêtée : l'enseignant doit voir ce qui n'est pas encore
    officiel.
    """
    classe   = Classe.objects.filter(pk=classe_id).select_related('institution').first()
    matiere  = Matiere.objects.filter(pk=matiere_id).first()
    semestre = SemestreIPGEI.objects.filter(pk=semestre_id).first()
    if classe is None or matiere is None or semestre is None:
        return {'lignes': [], 'nb_ds': 0, 'nb_exam': 0, 'has_tp': False}

    inscriptions = (
        InscriptionIPGEI.objects
        .filter(classe=classe, actif=True)
        .select_related('etudiant')
        .order_by('numero_ordre', 'etudiant__nom')
    )
    notes = {
        n.inscription_id: n
        for n in Note.objects
        .filter(inscription__in=inscriptions, matiere=matiere, semestre=semestre)
        .prefetch_related('evaluations')
    }

    # Le nombre de colonnes suit ce qui est DÉJÀ saisi dans la classe, comme la
    # grille de la scolarité : la feuille s'élargit d'elle-même quand un DS
    # s'ajoute, sans réglage.
    nb_ds = nb_exam = 0
    for note in notes.values():
        for evaluation in note.evaluations.all():
            if evaluation.type_evaluation == 'ds':
                nb_ds = max(nb_ds, evaluation.numero)
            elif evaluation.type_evaluation == 'exam':
                nb_exam = max(nb_exam, evaluation.numero)
    nb_ds, nb_exam = max(nb_ds, 1), max(nb_exam, 1)

    brouillons = {
        (b.note_ipgei_id, b.type_note, b.numero): b.valeur
        for b in SaisieNoteEnLigne.objects.filter(
            prof_id=prof.pk, classe_id=classe.pk,
            matiere_id=matiere.pk, semestre_id=semestre.pk)
    }

    # La pondération de la CLASSE, qui peut déroger : sans elle, l'écran
    # afficherait une colonne TP à une classe qui n'en a pas, ou la cacherait à
    # celle qui en a une (cf. `ipgei.PonderationClasse`).
    regle = services.ponderation_applicable(matiere, classe)

    lignes = []
    for inscription in inscriptions:
        note = notes.get(inscription.pk)
        officielles = _cases(note, nb_ds, nb_exam) if note is not None else {}

        def case(type_note, numero):
            if note is not None:
                cle = (note.pk, type_note, numero)
                if cle in brouillons:
                    return {'valeur': _texte(brouillons[cle]), 'brouillon': True}
            valeur = officielles.get((type_note, numero))
            return {'valeur': _texte(valeur), 'brouillon': False}

        lignes.append({
            'note':               note.pk if note is not None else None,
            'inscription':        inscription.pk,
            'etudiant_matricule': inscription.etudiant.matricule,
            'etudiant_nom':       inscription.etudiant.nom,
            # Une ligne sans note n'est pas saisissable : la scolarité n'a pas
            # encore ouvert la matière pour cette classe. On le dit plutôt que
            # d'accepter une saisie qui n'aurait nulle part où se poser.
            'ouverte':            note is not None,
            'ds':   [case('ds', n) for n in range(1, nb_ds + 1)],
            'exam': [case('exam', n) for n in range(1, nb_exam + 1)],
            'tp':   case('tp', 1) if regle.has_tp else None,
        })

    return {
        'classe_nom':       classe.nom,
        'matiere_code':     matiere.code,
        'matiere_intitule': matiere.intitule,
        'semestre_code':    semestre.code,
        'annee_universitaire': semestre.annee_universitaire,
        'nb_ds':            nb_ds,
        'nb_exam':          nb_exam,
        'has_tp':           regle.has_tp,
        'verrouille':       semestre.est_cloture,
        'lignes':           lignes,
    }


def _texte(valeur):
    """`Decimal` → chaîne, `None` conservé : l'écran édite du texte."""
    return None if valeur is None else str(valeur)


def valeur_note(brut):
    """
    Une note sur 20, ou `None` — ce qui vaut EFFACEMENT du brouillon.

    Rendre `None` pour une case vidée est délibéré : c'est ainsi que
    l'enseignant revient sur une erreur, et cela doit rester possible tant que
    la direction n'a pas exporté.
    """
    if brut is None or str(brut).strip() == '':
        return None
    try:
        valeur = Decimal(str(brut).replace(',', '.'))
    except (InvalidOperation, ValueError):
        raise ValueError('Note illisible.')
    if valeur < 0 or valeur > 20:
        raise ValueError('Une note va de 0 à 20.')
    return valeur


def deposer(prof, user, note: Note, type_note: str, numero: int, valeur) -> str:
    """
    Dépose (ou efface) UN brouillon. Rend 'cree', 'efface' ou 'inchange'.

    L'instantané — matricule, nom, code de matière, classe — est recopié ICI,
    au moment du dépôt, et non lu plus tard par jointure : ces lignes survivent
    à la restauration qui remplace tout le reste du miroir, elles ne peuvent
    donc dépendre d'aucun objet que cette restauration réécrit.
    """
    if type_note not in TYPES_SAISISSABLES:
        raise ValueError(f'Type de note inattendu : {type_note}.')
    numero = int(numero) if type_note in TYPES_NUMEROTES else 1
    if numero < 1:
        raise ValueError('Le numéro du devoir commence à 1.')

    cle = {'prof_id': prof.pk, 'note_ipgei_id': note.pk,
           'type_note': type_note, 'numero': numero}

    if valeur is None:
        supprimees, _ = SaisieNoteEnLigne.objects.filter(**cle).delete()
        return 'efface' if supprimees else 'inchange'

    inscription = note.inscription
    SaisieNoteEnLigne.objects.update_or_create(
        **cle,
        defaults={
            'prof_nom':  getattr(prof, 'nom', '') or '',
            'user_id':   getattr(user, 'pk', None),
            'valeur':    valeur,
            'statut':    'brouillon',
            'matiere_id':  note.matiere_id,
            'classe_id':   inscription.classe_id,
            'semestre_id': note.semestre_id,
            'annee_universitaire': inscription.annee_universitaire,
            'institution_id':      inscription.classe.institution_id,
            'etudiant_matricule':  inscription.etudiant.matricule,
            'etudiant_nom':        inscription.etudiant.nom,
            'matiere_code':        note.matiere.code,
            'matiere_intitule':    note.matiere.intitule,
            'classe_nom':          inscription.classe.nom,
            'semestre_code':       note.semestre.code,
        },
    )
    return 'cree'
