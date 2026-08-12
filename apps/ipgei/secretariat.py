"""
Pièces du secrétariat autour de l'anonymat des copies.

Deux documents, et TOUT tient dans ce qui les sépare :

  * **La liste du secrétariat** porte le couple `numéro ↔ matricule`. C'est la
    table de levée d'anonymat : elle ne quitte pas le secrétariat, et sert à
    rendre les copies à leurs auteurs une fois la correction close.

  * **La feuille du correcteur** ne porte QUE des numéros. La donner avec les
    matricules annulerait l'anonymat au moment précis où il sert — pendant la
    correction. Ce n'est pas une précaution de forme : c'est la raison d'être
    du dispositif.

Les deux se tirent de la même campagne, et l'on ne peut pas se tromper de
document : ils ne sortent pas de la même fonction.

Les numéros valent pour la SESSION, non par matière ni par classe : c'est
pourquoi la liste couvre toutes les classes d'un même niveau confondues — un
correcteur reçoit un paquet de copies, pas une classe.
"""
import io
import logging
from datetime import date

from django.core.exceptions import ValidationError

from core.pdf_renderer import render_pdf_bytes
from core.pdf_utils import get_institution_context

from .collecte import OPTIONS_PDF, _entete_excel, _logo_reduit
from .models import AnonymatIPGEI

logger = logging.getLogger('siga')

TEMPLATE_LISTE      = 'ipgei/anonymat_liste.html'
TEMPLATE_REPARTITION = 'ipgei/anonymat_repartition.html'


def lignes(session, niveau: str = '') -> list:
    """
    Les copies de la campagne : `(numéro, matricule, classe)`, par numéro.

    Toutes les classes du niveau sont confondues — c'est ainsi qu'on répartit
    des copies. `niveau` restreint quand deux niveaux partagent le semestre
    (MPSI et MPI en première année) et qu'on ne corrige que l'un des deux.
    """
    requete = (AnonymatIPGEI.objects
               .filter(session=session)
               .select_related('inscription__etudiant', 'inscription__classe')
               .order_by('numero'))
    if niveau:
        requete = requete.filter(inscription__classe__niveau__niveau=niveau)
    return [
        {
            'numero':    ligne.numero,
            'matricule': ligne.inscription.etudiant.matricule,
            'classe':    ligne.inscription.classe.nom,
        }
        for ligne in requete
    ]


def _contexte(session, niveau: str, copies: list) -> dict:
    donnees = get_institution_context()
    semestre = session.semestre
    donnees.update({
        'session':             session,
        'semestre':            semestre,
        'annee_universitaire': semestre.annee_universitaire,
        'niveau':              niveau or 'tous niveaux',
        'campagne':            session.get_type_session_display(),
        'copies':              copies,
        'effectif':            len(copies),
        'date_edition':        date.today().strftime('%d/%m/%Y'),
    })
    return donnees


# ═════════════════════════════════════════════════════════════════════════════
# Liste du secrétariat — numéro ↔ matricule
# ═════════════════════════════════════════════════════════════════════════════
def generer_pdf(session, niveau: str = '') -> bytes:
    copies = lignes(session, niveau)
    if not copies:
        raise ValidationError(_rien_a_editer(session, niveau))
    return render_pdf_bytes(TEMPLATE_LISTE, _contexte(session, niveau, copies),
                            OPTIONS_PDF)


def generer_excel(session, niveau: str = '') -> bytes:
    """
    Deux colonnes, numéro et matricule, et rien d'autre.

    Le classeur est fait pour être trié et recherché — c'est ce qu'on demande à
    une table de correspondance. Le nom de la classe suit en troisième colonne :
    il ne lève pas l'anonymat davantage que le matricule, et évite au
    secrétariat de croiser une seconde liste pour savoir où rendre la copie.
    """
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    copies = lignes(session, niveau)
    if not copies:
        raise ValidationError(_rien_a_editer(session, niveau))

    donnees = _contexte(session, niveau, copies)
    VERT = '006633'
    trait = Side(border_style='thin', color='999999')
    bordure = Border(left=trait, right=trait, top=trait, bottom=trait)
    centre = Alignment(horizontal='center', vertical='center')

    classeur = openpyxl.Workbook()
    feuille = classeur.active
    feuille.title = 'Anonymat'
    entetes = ['N° anonymat', 'Matricule', 'Classe']
    for index, largeur in enumerate((16, 18, 14), 1):
        feuille.column_dimensions[get_column_letter(index)].width = largeur

    rang = _entete_excel(feuille, donnees, len(entetes), _logo_reduit(donnees))

    titre = feuille.cell(rang, 1, 'Correspondance anonymat — matricule')
    titre.font = Font(bold=True, size=12, color=VERT)
    titre.alignment = Alignment(horizontal='center')
    feuille.merge_cells(start_row=rang, start_column=1,
                        end_row=rang, end_column=len(entetes))
    rang += 2

    for etiquette, valeur in (
        ('Semestre',       donnees['semestre'].code),
        ('Campagne',       donnees['campagne']),
        ('Niveau',         donnees['niveau']),
        ('Année',          donnees['annee_universitaire']),
        ('Copies',         str(donnees['effectif'])),
        ("Date d'édition", donnees['date_edition']),
    ):
        feuille.cell(rang, 1, f'{etiquette} :').font = Font(bold=True)
        feuille.cell(rang, 2, valeur)
        rang += 1

    ligne_entete = rang + 1
    for colonne, entete in enumerate(entetes, 1):
        cellule = feuille.cell(ligne_entete, colonne, entete)
        cellule.font = Font(bold=True, color='FFFFFF')
        cellule.fill = PatternFill('solid', fgColor=VERT)
        cellule.alignment = centre
        cellule.border = bordure

    rang = ligne_entete + 1
    for copie in copies:
        for colonne, valeur in enumerate(
            (copie['numero'], copie['matricule'], copie['classe']), 1,
        ):
            cellule = feuille.cell(rang, colonne, valeur)
            cellule.border = bordure
            if colonne < 3:
                cellule.alignment = centre
        rang += 1

    feuille.freeze_panes = feuille.cell(ligne_entete + 1, 1)
    tampon = io.BytesIO()
    classeur.save(tampon)
    return tampon.getvalue()


def _rien_a_editer(session, niveau: str) -> str:
    quoi = f' en {niveau}' if niveau else ''
    return (f"Aucun numéro d'anonymat tiré pour "
            f'{session.get_type_session_display().lower()} de '
            f'{session.semestre.code}{quoi}. Le tirage se fait depuis '
            'Notes → Anonymat des copies, avant la correction.')


# ═════════════════════════════════════════════════════════════════════════════
# Répartition des copies entre correcteurs
# ═════════════════════════════════════════════════════════════════════════════
def repartir(session, lots: list, niveau: str = '') -> dict:
    """
    Partage les copies entre correcteurs, par paquets consécutifs.

    `lots` : `[{'correcteur': 'Nom', 'nb': 20}, …]`. Les paquets suivent l'ordre
    des numéros — c'est ainsi qu'on distribue une pile de copies, et cela permet
    de dire « de 1 à 20 » plutôt que d'énumérer vingt nombres.

    Le total demandé n'a pas à couvrir toute la pile : le reste est RENDU dans
    le bilan plutôt que réparti d'office. Un secrétariat qui garde vingt copies
    pour un correcteur qui n'est pas encore désigné doit pouvoir le faire — mais
    il doit le voir.

    Demander PLUS de copies qu'il n'en existe est en revanche refusé : servir
    les premiers et laisser les derniers sans rien, en silence, ferait croire à
    une répartition complète.
    """
    copies = lignes(session, niveau)
    if not copies:
        raise ValidationError(_rien_a_editer(session, niveau))

    propres = []
    for lot in lots:
        nom = (lot.get('correcteur') or '').strip()
        try:
            nb = int(lot.get('nb') or 0)
        except (TypeError, ValueError):
            raise ValidationError({'lots': f'Nombre de copies invalide pour « {nom} ».'})
        if not nom:
            raise ValidationError({'lots': 'Chaque lot doit nommer son correcteur.'})
        if nb <= 0:
            raise ValidationError({'lots': f'« {nom} » : le nombre de copies doit '
                                           'être supérieur à zéro.'})
        propres.append({'correcteur': nom, 'nb': nb})
    if not propres:
        raise ValidationError({'lots': 'Aucun correcteur désigné.'})

    demande = sum(lot['nb'] for lot in propres)
    if demande > len(copies):
        raise ValidationError(
            {'lots': f'{demande} copies demandées pour {len(copies)} disponibles. '
                     f'Il en manque {demande - len(copies)}.'})

    paquets, curseur = [], 0
    for lot in propres:
        tranche = copies[curseur:curseur + lot['nb']]
        curseur += lot['nb']
        paquets.append({
            'correcteur': lot['correcteur'],
            'nb':         len(tranche),
            'premier':    tranche[0]['numero'],
            'dernier':    tranche[-1]['numero'],
            # Les NUMÉROS seuls : la feuille remise au correcteur ne doit
            # porter aucun matricule, sans quoi l'anonymat tombe au moment
            # même où il sert.
            'numeros':    [c['numero'] for c in tranche],
        })
    return {
        'paquets':    paquets,
        'total':      len(copies),
        'reparties':  curseur,
        'restantes':  [c['numero'] for c in copies[curseur:]],
    }


def generer_repartition_pdf(session, lots: list, niveau: str = '') -> bytes:
    """Une page par correcteur — numéros seuls, aucun matricule."""
    bilan = repartir(session, lots, niveau)
    donnees = _contexte(session, niveau, [])
    donnees.update(bilan)
    return render_pdf_bytes(TEMPLATE_REPARTITION, donnees, OPTIONS_PDF)
