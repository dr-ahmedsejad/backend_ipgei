"""
Résultat du concours CNIM, et situation de fin de cursus.

Le concours est EXTERNE : son résultat ne se calcule pas, il se constate. Ce
module ne fait donc pas de jury — il enregistre ce que l'établissement reçoit,
puis en tire la conséquence, qui, elle, est mécanique :

    admis      → SORTANT
    non admis  → REDOUBLANT   si le droit de redoublement reste ouvert
               → RÉORIENTÉ    s'il est consommé

C'est exactement la règle du jury de rattrapage — `decision_apres_seconde
_chance` —, réemployée telle quelle. Une seconde chance manquée et un concours
manqué ouvrent la même suite : ce serait une faute d'en écrire deux versions.

Deux précautions commandent le reste :

  * **le résultat est CONSERVÉ**, et non déduit du statut. Sans lui, on ne
    saurait plus dire pourquoi un étudiant est sortant, ni distinguer « non
    admis » de « jamais présenté » ;
  * **appliquer deux fois ne compte pas deux redoublements.** L'application ne
    touche qu'un étudiant encore « autorisé à concourir » : une fois sa
    situation posée, il sort du périmètre de lui-même.
"""
import logging
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from . import services
from .models import InscriptionIPGEI, LigneDeliberation, ParametresIPGEI

logger = logging.getLogger('siga')

#: Ce que le concours produit comme situation, une fois appliqué.
SITUATIONS = {
    LigneDeliberation.DECISION_REDOUBLANT: InscriptionIPGEI.STATUT_REDOUBLANT,
    LigneDeliberation.DECISION_REORIENTE:  InscriptionIPGEI.STATUT_REORIENTE,
}


def _concernes(annee_universitaire: str, niveau: str = '', institution=None):
    """
    Les candidats du concours : ceux qu'un jury a autorisés à concourir, et
    ceux dont la situation en a déjà été tirée.

    Les seconds restent listés à dessein — un écran qui les ferait disparaître
    dès l'application donnerait l'impression d'avoir perdu la moitié de la
    promotion, et ne permettrait plus de vérifier ce qui a été fait.
    """
    concernes = (InscriptionIPGEI.objects
                 .filter(annee_universitaire=annee_universitaire, actif=True)
                 .filter(statut__in=[InscriptionIPGEI.STATUT_CNIM,
                                     InscriptionIPGEI.STATUT_SORTANT,
                                     InscriptionIPGEI.STATUT_REDOUBLANT,
                                     InscriptionIPGEI.STATUT_REORIENTE])
                 .select_related('etudiant', 'classe__institution', 'classe__niveau')
                 .order_by('classe__nom', 'etudiant__matricule'))
    if niveau:
        concernes = concernes.filter(classe__niveau__niveau=niveau)
    if institution is not None:
        concernes = concernes.filter(classe__institution=institution)
    # Un redoublant ou un réorienté qui n'a jamais concouru n'a rien à faire
    # ici : sa situation vient du jury, pas du concours.
    return [i for i in concernes
            if i.statut == InscriptionIPGEI.STATUT_CNIM or i.resultat_cnim]


def situation_projetee(inscription, droit_max: int) -> str:
    """
    Ce que deviendra cet étudiant si l'on applique son résultat — sans rien
    changer encore.

    Rendu vide tant que le résultat n'est pas connu : on ne préjuge pas d'un
    concours qui n'a pas rendu sa liste.
    """
    if inscription.resultat_cnim == InscriptionIPGEI.CNIM_ADMIS:
        return InscriptionIPGEI.STATUT_SORTANT
    if inscription.resultat_cnim == InscriptionIPGEI.CNIM_NON_ADMIS:
        return SITUATIONS[services.decision_apres_seconde_chance(
            inscription, droit_max)]
    return ''


def grille(annee_universitaire: str, niveau: str = '', institution=None) -> dict:
    """La liste des candidats, leur résultat, et la situation qui en découlerait."""
    params = ParametresIPGEI.pour(institution) if institution else None
    droit_max = params.droit_redoublement_max if params else 1
    libelles = dict(InscriptionIPGEI.STATUT_CHOICES)

    lignes = []
    for inscription in _concernes(annee_universitaire, niveau, institution):
        projetee = situation_projetee(inscription, droit_max)
        lignes.append({
            'inscription':        inscription.pk,
            'etudiant_nom':       inscription.etudiant.nom_display,
            'etudiant_matricule': inscription.etudiant.matricule,
            'classe_nom':         inscription.classe.nom,
            'nb_redoublements':   inscription.nb_redoublements,
            'resultat':           inscription.resultat_cnim,
            'date_resultat':      inscription.date_resultat_cnim,
            'statut':             inscription.statut,
            'statut_display':     inscription.get_statut_display(),
            # Ce qui CHANGERAIT si l'on appliquait — vide si rien ne bouge.
            'situation_projetee': projetee,
            'situation_libelle':  libelles.get(projetee, ''),
            'applique':           inscription.statut != InscriptionIPGEI.STATUT_CNIM,
        })
    return {
        'lignes':            lignes,
        'droit_redoublement': droit_max,
        'en_attente':        sum(1 for l in lignes if not l['resultat']),
        'a_appliquer':       sum(1 for l in lignes
                                 if l['situation_projetee'] and not l['applique']),
    }


@transaction.atomic
def saisir(saisies: list) -> dict:
    """
    Enregistre les résultats saisis à la main.

    `saisies` : `[{'inscription': id, 'resultat': 'admis'|'non_admis'|''}]`.
    Une valeur vide remet en attente — on corrige une saisie sans avoir à
    défaire une situation déjà appliquée.
    """
    valides = {c for c, _ in InscriptionIPGEI.CNIM_CHOICES}
    ecrites = 0
    for saisie in saisies:
        resultat = saisie.get('resultat') or ''
        if resultat not in valides:
            raise ValidationError(
                {'resultat': f'« {resultat} » n\'est pas un résultat de concours.'})
        inscription = InscriptionIPGEI.objects.filter(
            pk=saisie['inscription']).first()
        if inscription is None:
            raise ValidationError(f"Inscription {saisie['inscription']} introuvable.")
        if inscription.statut != InscriptionIPGEI.STATUT_CNIM and not inscription.resultat_cnim:
            raise ValidationError(
                f'{inscription.etudiant.matricule} n\'a pas été autorisé à '
                'concourir : son résultat ne peut pas être saisi.')

        inscription.resultat_cnim = resultat
        inscription.date_resultat_cnim = timezone.now() if resultat else None
        inscription.save(update_fields=['resultat_cnim', 'date_resultat_cnim'])
        ecrites += 1
    return {'saisies': ecrites}


@transaction.atomic
def importer_admis(fichier, annee_universitaire: str, niveau: str = '',
                   institution=None, liste_definitive: bool = False) -> dict:
    """
    Importe le résultat du concours, sous l'une ou l'autre forme.

    **Le modèle de cet écran**, qui porte une colonne « Résultat » : chaque
    ligne dit ce qu'elle vaut — ADMIS ou NON ADMIS —, et rien n'est déduit.

    **Une simple liste de matricules**, telle qu'un jury de concours la publie :
    ce qu'elle contient devient « admis », ce qu'elle ne contient pas reste **en
    attente**. Une liste d'admis ne dit rien des autres tant qu'on ne la déclare
    pas complète ; `liste_definitive` bascule alors les absents, et c'est un
    geste explicite, jamais une déduction.

    Les matricules inconnus sont RENDUS, pas ignorés : un fichier qui ne
    correspond à personne doit se voir, sinon on croit l'import réussi.
    """
    releve = _lire_fichier(fichier)
    if not releve:
        raise ValidationError(
            'Aucun matricule lu dans ce fichier. Attendu : une colonne de '
            'matricules, avec ou sans en-tête — ou le modèle proposé sur cet '
            'écran, avec sa colonne « Résultat ».')

    candidats = {i.etudiant.matricule: i
                 for i in _concernes(annee_universitaire, niveau, institution)}
    inconnus = sorted(m for m in releve if m not in candidats)

    admis = non_admis = 0
    for matricule, inscription in candidats.items():
        if matricule not in releve:
            continue
        # Sans colonne « Résultat », figurer sur la liste vaut admission.
        resultat = releve[matricule] or InscriptionIPGEI.CNIM_ADMIS
        inscription.resultat_cnim = resultat
        inscription.date_resultat_cnim = timezone.now()
        inscription.save(update_fields=['resultat_cnim', 'date_resultat_cnim'])
        if resultat == InscriptionIPGEI.CNIM_ADMIS:
            admis += 1
        else:
            non_admis += 1

    if liste_definitive:
        for matricule, inscription in candidats.items():
            if matricule not in releve and not inscription.resultat_cnim:
                inscription.resultat_cnim = InscriptionIPGEI.CNIM_NON_ADMIS
                inscription.date_resultat_cnim = timezone.now()
                inscription.save(update_fields=['resultat_cnim',
                                                'date_resultat_cnim'])
                non_admis += 1

    logger.info('CNIM %s : %s admis, %s non admis, %s matricule(s) inconnu(s)',
                annee_universitaire, admis, non_admis, len(inconnus))
    return {'admis': admis, 'non_admis': non_admis, 'inconnus': inconnus,
            'lus': len(releve)}


#: En-têtes reconnus dans un fichier structuré — celui que le modèle produit.
#: Le reste (nom, classe) est ignoré : il n'est là que pour se relire.
ENTETE_MATRICULE = ('matricule', 'matricules', 'n° matricule')
ENTETE_RESULTAT  = ('résultat', 'resultat', 'décision', 'decision')

#: Ce qu'on accepte d'écrire dans la colonne « Résultat ».
MOTS_ADMIS     = ('admis', 'admise', 'oui', 'a', 'x', '1')
MOTS_NON_ADMIS = ('non admis', 'non-admis', 'nonadmis', 'non', 'refusé',
                  'refuse', 'ajourné', 'ajourne', 'n', '0')

#: Ce que le MODÈLE écrit — et qu'il doit savoir relire. Les deux listes
#: ci-dessus admettent bien d'autres formulations ; celles-ci sont celles qu'on
#: produit, et le tour complet en dépend.
MOTS_MODELE = {
    InscriptionIPGEI.CNIM_ADMIS:     'ADMIS',
    InscriptionIPGEI.CNIM_NON_ADMIS: 'NON ADMIS',
}


def _resultat_ecrit(texte: str) -> str | None:
    """Traduit ce qu'un agent a écrit dans la colonne « Résultat »."""
    propre = ' '.join((texte or '').strip().lower().split())
    if not propre:
        return None
    if propre in MOTS_ADMIS:
        return InscriptionIPGEI.CNIM_ADMIS
    if propre in MOTS_NON_ADMIS:
        return InscriptionIPGEI.CNIM_NON_ADMIS
    raise ValidationError(
        f'« {texte} » ne se comprend pas dans la colonne Résultat. '
        'Écrivez ADMIS ou NON ADMIS.')


def _lire_structure(lignes) -> dict | None:
    """
    Lit un fichier À EN-TÊTE : le modèle que cet écran propose.

    Renvoie `{matricule: résultat ou None}`, ou `None` si aucun en-tête
    « Matricule » n'est reconnu — le fichier est alors une simple liste, et
    c'est l'autre lecture qui s'applique.
    """
    for numero, cellules in enumerate(lignes):
        textes = [str(c).strip().lower() if c is not None else '' for c in cellules]
        if not any(t in ENTETE_MATRICULE for t in textes):
            continue
        colonne_matricule = next(i for i, t in enumerate(textes)
                                 if t in ENTETE_MATRICULE)
        colonne_resultat = next((i for i, t in enumerate(textes)
                                 if t in ENTETE_RESULTAT), None)

        releve = {}
        for suivante in lignes[numero + 1:]:
            if colonne_matricule >= len(suivante):
                continue
            matricule = _matricule(suivante[colonne_matricule])
            if not matricule:
                continue          # le tableau s'arrête à la première ligne vide
            resultat = None
            if colonne_resultat is not None and colonne_resultat < len(suivante):
                resultat = _resultat_ecrit(str(suivante[colonne_resultat] or ''))
            releve[matricule] = resultat
        return releve
    return None


def _matricule(brut) -> str:
    """
    Matricule lu d'une cellule.

    Excel range volontiers « 26001 » en nombre : il revient en 26001.0, qui ne
    correspond à aucun étudiant.
    """
    if brut is None:
        return ''
    if isinstance(brut, float) and brut.is_integer():
        brut = int(brut)
    texte = str(brut).strip()
    if texte.endswith('.0') and texte[:-2].isdigit():
        texte = texte[:-2]
    return texte if texte and any(c.isdigit() for c in texte) and len(texte) <= 20 else ''


def _lire_fichier(fichier) -> dict:
    """
    `{matricule: résultat ou None}` — la lecture, quelle que soit la forme.

    On tente d'abord la lecture STRUCTURÉE, celle du modèle : elle seule permet
    de dire « non admis » ligne à ligne. À défaut d'en-tête reconnu, on retombe
    sur la lecture large — tout ce qui ressemble à un matricule —, parce qu'une
    liste publiée par un jury de concours n'a pas de gabarit.
    """
    lignes = _lignes_du_fichier(fichier)
    structure = _lire_structure(lignes)
    if structure is not None:
        return structure
    return {matricule: None
            for ligne in lignes for cellule in ligne
            if (matricule := _matricule(cellule))}


def _lignes_du_fichier(fichier) -> list:
    """Les cellules du fichier, ligne par ligne — classeur, CSV ou texte."""
    nom = (getattr(fichier, 'name', '') or '').lower()
    if nom.endswith(('.xlsx', '.xlsm')):
        import openpyxl
        classeur = openpyxl.load_workbook(fichier, read_only=True, data_only=True)
        return [list(ligne)
                for feuille in classeur.worksheets
                for ligne in feuille.iter_rows(values_only=True)]
    contenu = fichier.read()
    if isinstance(contenu, bytes):
        contenu = contenu.decode('utf-8-sig', errors='ignore')
    return [ligne.replace(';', ',').replace('	', ',').split(',')
            for ligne in contenu.splitlines()]


def modele_excel(annee_universitaire: str, niveau: str = '',
                 institution=None) -> bytes:
    """
    Le modèle à remplir : les candidats, une colonne « Résultat », rien d'autre.

    Pré-rempli avec ceux que le jury a autorisés à concourir. C'est ce qui en
    fait un modèle utile plutôt qu'une feuille vide : on n'y retape aucun
    matricule, on écrit ADMIS ou NON ADMIS en face des noms — et ce que l'on
    laisse blanc reste en attente.

    Il se reverse par le même écran : c'est le tour complet, comme les fiches de
    collecte.
    """
    import io as flux

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    candidats = _concernes(annee_universitaire, niveau, institution)
    if not candidats:
        raise ValidationError(
            f"Aucun étudiant autorisé à concourir en {annee_universitaire}"
            + (f' au niveau {niveau}' if niveau else '')
            + ". L'autorisation vient du jury annuel ou du jury de rattrapage.")

    VERT = '006633'
    trait = Side(style='thin', color='D5D5D5')
    bordure = Border(trait, trait, trait, trait)

    classeur = Workbook()
    feuille = classeur.active
    feuille.title = 'Résultat CNIM'

    feuille['A1'] = f'Résultat du concours CNIM — {annee_universitaire}'
    feuille['A1'].font = Font(bold=True, size=13, color=VERT)
    feuille['A2'] = ("Écrivez ADMIS ou NON ADMIS dans la dernière colonne. "
                     'Une case laissée vide reste « en attente ». '
                     'Ne modifiez ni les matricules ni les en-têtes.')
    feuille['A2'].font = Font(size=9, italic=True, color='555555')

    entetes = ['Matricule', 'Nom et prénom', 'Classe', 'Résultat']
    for colonne, libelle in enumerate(entetes, start=1):
        cellule = feuille.cell(row=4, column=colonne, value=libelle)
        cellule.font = Font(color='FFFFFF', bold=True, size=10)
        cellule.fill = PatternFill('solid', fgColor=VERT)
        cellule.border = bordure
        cellule.alignment = Alignment(horizontal='center', vertical='center')

    for index, inscription in enumerate(candidats, start=5):
        valeurs = (inscription.etudiant.matricule,
                   inscription.etudiant.nom_display,
                   inscription.classe.nom,
                   # Le résultat déjà connu est reporté — rouvrir le modèle
                   # ne doit pas effacer ce qu'on sait —, et dans les MOTS que
                   # l'import relit. Y écrire le libellé complet, « Admis au
                   # concours », aurait produit un fichier que le logiciel
                   # refuse de relire lui-même.
                   MOTS_MODELE.get(inscription.resultat_cnim))
        for colonne, valeur in enumerate(valeurs, start=1):
            cellule = feuille.cell(row=index, column=colonne, value=valeur)
            cellule.border = bordure
            if colonne in (1, 3, 4):
                cellule.alignment = Alignment(horizontal='center')

    # Une liste déroulante plutôt qu'une saisie libre : « ajourné », « refusé »
    # ou « non-admis » se comprennent à la relecture, mais autant éviter d'avoir
    # à les deviner.
    choix = DataValidation(type='list', formula1='"ADMIS,NON ADMIS"',
                           allow_blank=True, showDropDown=False)
    choix.error = 'Écrivez ADMIS ou NON ADMIS, ou laissez vide.'
    feuille.add_data_validation(choix)
    choix.add(f'D5:D{4 + len(candidats)}')

    for colonne, largeur in enumerate((16, 32, 12, 18), start=1):
        feuille.column_dimensions[get_column_letter(colonne)].width = largeur
    feuille.freeze_panes = 'A5'

    tampon = flux.BytesIO()
    classeur.save(tampon)
    return tampon.getvalue()


@transaction.atomic
def appliquer(annee_universitaire: str, niveau: str = '', institution=None,
              apercu: bool = False) -> dict:
    """
    Applique les résultats : chacun reçoit la situation qui découle du sien.

    `apercu=True` rend le bilan SANS rien écrire — c'est ce que l'écran montre
    avant d'agir, parce qu'un redoublement compté ne se reprend pas d'un clic.

    Le compteur de redoublement est incrémenté ICI pour ceux qui redoublent,
    une seule fois : l'application ne touche qu'un étudiant encore « autorisé à
    concourir », si bien qu'un second passage le laisse tel quel.
    """
    params = ParametresIPGEI.pour(institution) if institution else None
    droit_max = params.droit_redoublement_max if params else 1
    libelles = dict(InscriptionIPGEI.STATUT_CHOICES)

    mouvements, ignores = [], 0
    for inscription in _concernes(annee_universitaire, niveau, institution):
        if inscription.statut != InscriptionIPGEI.STATUT_CNIM:
            continue                       # sa situation est déjà posée
        situation = situation_projetee(inscription, droit_max)
        if not situation:
            ignores += 1                   # résultat non connu : on ne touche à rien
            continue
        mouvements.append((inscription, situation))

    if not apercu:
        for inscription, situation in mouvements:
            inscription.statut = situation
            champs = ['statut']
            if situation == InscriptionIPGEI.STATUT_REDOUBLANT:
                inscription.nb_redoublements += 1
                champs.append('nb_redoublements')
            inscription.save(update_fields=champs)

    bilan = {}
    for _, situation in mouvements:
        bilan[libelles[situation]] = bilan.get(libelles[situation], 0) + 1
    return {
        'appliques':   0 if apercu else len(mouvements),
        'a_appliquer': len(mouvements),
        'en_attente':  ignores,
        'repartition': bilan,
        'detail': [
            {'matricule': i.etudiant.matricule, 'nom': i.etudiant.nom_display,
             'classe': i.classe.nom, 'resultat': i.resultat_cnim,
             'situation': libelles[s]}
            for i, s in mouvements
        ],
    }
