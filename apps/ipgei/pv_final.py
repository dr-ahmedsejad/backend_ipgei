"""
Procès-verbal FINAL de deuxième année — l'acte de clôture du cycle.

Trois pièces disent chacune un moment, et aucune ne dit le résultat :

  * le PV annuel arrête les moyennes de la session normale et renvoie au
    rattrapage ceux qui sont sous le seuil ;
  * le PV de rattrapage dit ce qu'il est advenu d'eux ;
  * le résultat du concours, saisi ensuite, décide du sort de ceux qui y ont
    été autorisés.

Aucune ne se suffit : lire le premier seul, c'est croire un étudiant « autorisé
au rattrapage » alors qu'il redouble depuis deux mois. Ce document les réunit,
ligne à ligne, et porte la SITUATION FINALE.

Il ne prononce rien. Tout y a déjà été décidé — par un jury, ou par le concours.
C'est ce qui le distingue des trois autres : il ne se calcule pas, il se
constate, et c'est pourquoi il se réédite à volonté sans jamais rien changer.
"""
import io
import logging
from datetime import date

from django.core.exceptions import ValidationError

from apps.documents.services import _get_logo_b64
from core.pdf_renderer import render_pdf_bytes
from core.pdf_utils import get_institution_context

from . import services
from .models import Deliberation, InscriptionIPGEI, LigneDeliberation

logger = logging.getLogger('siga')

TEMPLATE = 'ipgei/pv_final.html'

#: Paysage : sept colonnes racontent un parcours — deux moyennes, trois
#: décisions, un résultat de concours et la situation. En portrait, la dernière
#: devient illisible, et c'est celle qu'on vient lire.
OPTIONS_PDF = {
    'page-size':    'A4',
    'orientation':  'Landscape',
    'margin-top':   '10mm',
    'margin-bottom': '10mm',
    'margin-left':  '10mm',
    'margin-right': '10mm',
    'encoding':     'UTF-8',
    'enable-local-file-access': None,
}

EN_TETES = ['Matricule', 'Nom et prénom', 'Classe', 'Moyenne annuelle',
            'Décision du jury', 'Après rattrapage', 'Décision du rattrapage',
            'Concours CNIM', 'Situation finale']

#: Ce que le concours a rendu, en toutes lettres.
LIBELLES_CNIM = {
    InscriptionIPGEI.CNIM_ADMIS:     'Admis',
    InscriptionIPGEI.CNIM_NON_ADMIS: 'Non admis',
    InscriptionIPGEI.CNIM_EN_ATTENTE: '',
}


def lignes(annuelle: Deliberation) -> list:
    """
    Le parcours de chaque étudiant du jury annuel, de bout en bout.

    On part du jury ANNUEL parce que c'est lui qui définit la promotion : le
    rattrapage n'en tient qu'une part, et le concours moins encore. Chacun y
    apporte sa colonne, vide s'il n'a pas eu à se prononcer.
    """
    decisions = dict(LigneDeliberation.DECISION_CHOICES)
    statuts   = dict(InscriptionIPGEI.STATUT_CHOICES)

    rattrapage = annuelle.jurys_rattrapage.first()
    apres = {}
    if rattrapage is not None:
        apres = {
            ligne.inscription_id: ligne
            for ligne in rattrapage.lignes.all()
        }

    resultats = []
    for ligne in (annuelle.lignes
                  .select_related('inscription__etudiant', 'inscription__classe')
                  .order_by('rang', 'inscription__etudiant__nom')):
        inscription = ligne.inscription
        seconde = apres.get(ligne.inscription_id)
        resultats.append({
            'rang':               ligne.rang,
            'matricule':          inscription.etudiant.matricule,
            'nom':                inscription.etudiant.nom_display,
            'classe':             inscription.classe.nom,
            'moyenne':            ligne.moyenne_generale,
            'decision':           decisions.get(ligne.decision, ''),
            # Vides pour qui n'est pas passé par le rattrapage : une colonne
            # remplie d'office ferait croire à une seconde session pour tous.
            'moyenne_rattrapage': seconde.moyenne_generale if seconde else None,
            'decision_rattrapage': decisions.get(seconde.decision, '') if seconde else '',
            'cnim':               LIBELLES_CNIM.get(inscription.resultat_cnim, ''),
            'situation':          statuts.get(inscription.statut, ''),
            'nb_redoublements':   inscription.nb_redoublements,
            # Sa situation n'est pas close : il est encore « autorisé à
            # concourir ». Soit le concours n'a pas rendu sa liste, soit elle
            # l'a rendue et personne n'a appliqué les situations.
            'ouvert':             inscription.statut == InscriptionIPGEI.STATUT_CNIM,
        })
    return resultats


def _contexte(annuelle: Deliberation, base_url: str = '') -> dict:
    if annuelle.portee != Deliberation.PORTEE_ANNUELLE:
        raise ValidationError(
            "Le procès-verbal final se tire du jury ANNUEL : c'est lui qui "
            'définit la promotion. Le rattrapage n\'en tient qu\'une part.')

    resultats = lignes(annuelle)
    if not resultats:
        raise ValidationError('Ce jury ne porte aucun étudiant.')

    rattrapage = annuelle.jurys_rattrapage.first()

    # La synthèse : ce que le lecteur cherche d'abord, et qu'il ne devrait pas
    # avoir à compter à la main sur quatre-vingts lignes.
    repartition = {}
    for resultat in resultats:
        if resultat['situation']:
            repartition[resultat['situation']] = repartition.get(
                resultat['situation'], 0) + 1

    donnees = get_institution_context()
    donnees.update({
        'deliberation':   annuelle,
        'rattrapage':     rattrapage,
        'institution':    annuelle.institution,
        'logo_b64':       _get_logo_b64(annuelle.institution),
        'titre':          _titre(annuelle),
        'lignes':         resultats,
        'effectif':       len(resultats),
        'membres':        services.membres_jury_ordonnes(annuelle),
        'repartition':    [{'libelle': libelle, 'nombre': nombre}
                           for libelle, nombre in sorted(repartition.items(),
                                                         key=lambda c: -c[1])],
        # Deux réserves distinctes, et il faut les distinguer : l'une attend
        # le concours, l'autre attend un clic. Les confondre laisserait croire
        # que le PV est en retard sur l'établissement, alors qu'il l'est sur
        # une action qui n'a pas été faite.
        'en_attente':     sum(1 for r in resultats if r['ouvert'] and not r['cnim']),
        'a_appliquer':    sum(1 for r in resultats if r['ouvert'] and r['cnim']),
        'niveau_libelle': services.libelle_classe(annuelle.classe)
                          if annuelle.classe_id else annuelle.niveau,
        'date_edition':   date.today().strftime('%d/%m/%Y'),
        'base_url':       base_url,
    })
    return donnees


def _titre(annuelle: Deliberation) -> str:
    cible = (f'la Classe {annuelle.classe.nom}' if annuelle.classe_id
             else f'du niveau {annuelle.niveau}')
    return f'PV FINAL des Résultats de {cible}'


def generer_pdf(annuelle: Deliberation, *, base_url: str = '') -> bytes:
    return render_pdf_bytes(TEMPLATE, _contexte(annuelle, base_url), OPTIONS_PDF)


def generer_excel(annuelle: Deliberation) -> bytes:
    """Le même parcours en classeur — pour retrier, compter, archiver."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    donnees = _contexte(annuelle)

    VERT = '006633'
    trait = Side(style='thin', color='D5D5D5')
    bordure = Border(trait, trait, trait, trait)

    classeur = Workbook()
    feuille = classeur.active
    feuille.title = 'PV final'

    feuille['A1'] = f"{donnees['titre']} — {annuelle.annee_universitaire}"
    feuille['A1'].font = Font(bold=True, size=13, color=VERT)
    feuille['A2'] = (f"{donnees['effectif']} étudiant(s) · édité le "
                     f"{donnees['date_edition']}"
                     + (f" · {donnees['en_attente']} sans résultat de concours"
                        if donnees['en_attente'] else ''))
    feuille['A2'].font = Font(size=9, italic=True, color='555555')

    for colonne, libelle in enumerate(EN_TETES, start=1):
        cellule = feuille.cell(row=4, column=colonne, value=libelle)
        cellule.font = Font(color='FFFFFF', bold=True, size=10)
        cellule.fill = PatternFill('solid', fgColor=VERT)
        cellule.border = bordure
        cellule.alignment = Alignment(horizontal='center', vertical='center',
                                      wrap_text=True)

    for index, ligne in enumerate(donnees['lignes'], start=5):
        valeurs = [
            ligne['matricule'], ligne['nom'], ligne['classe'],
            float(ligne['moyenne']) if ligne['moyenne'] is not None else None,
            ligne['decision'],
            float(ligne['moyenne_rattrapage'])
            if ligne['moyenne_rattrapage'] is not None else None,
            ligne['decision_rattrapage'], ligne['cnim'], ligne['situation'],
        ]
        for colonne, valeur in enumerate(valeurs, start=1):
            cellule = feuille.cell(row=index, column=colonne, value=valeur)
            cellule.border = bordure
            if colonne in (4, 6):
                cellule.number_format = '0.00'
                cellule.alignment = Alignment(horizontal='center')
            elif colonne in (1, 3, 8):
                cellule.alignment = Alignment(horizontal='center')

    for colonne, largeur in enumerate((14, 28, 10, 14, 24, 14, 24, 14, 26), start=1):
        feuille.column_dimensions[get_column_letter(colonne)].width = largeur
    feuille.freeze_panes = 'A5'

    jury = classeur.create_sheet('Jury')
    for colonne, libelle in enumerate(['Rôle', 'Membre'], start=1):
        cellule = jury.cell(row=1, column=colonne, value=libelle)
        cellule.fill = PatternFill('solid', fgColor=VERT)
        cellule.font = Font(color='FFFFFF', bold=True)
    for index, membre in enumerate(donnees['membres'], start=2):
        jury.cell(row=index, column=1, value=membre.get_role_display())
        jury.cell(row=index, column=2,
                  value=membre.utilisateur.name or membre.utilisateur.username)
    for colonne, largeur in zip('AB', (28, 34)):
        jury.column_dimensions[colonne].width = largeur

    tampon = io.BytesIO()
    classeur.save(tampon)
    return tampon.getvalue()
