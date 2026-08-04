"""
Procès-verbal de délibération IPGEI — éditions PDF et Excel.

À la différence des documents de `documents.py`, le PV est **collectif** : il
porte toute une promotion, pas un étudiant. Il ne passe donc pas par
`DocumentOfficiel`, dont chaque ligne est rattachée à un étudiant et à un numéro
de série individuel. Il se génère à la demande, à partir des lignes de jury
telles qu'elles sont au moment du tirage.

Tant que la délibération n'est pas validée, l'édition porte la mention
« projet » : un PV non arrêté qui circulerait sans marque serait pris pour un
document définitif.
"""
import logging
from datetime import date
from io import BytesIO

from django.utils import timezone

from apps.documents.services import _get_logo_b64
from core.pdf_renderer import render_pdf_bytes

from . import services
from .models import Deliberation, LigneDeliberation

logger = logging.getLogger('siga')

TEMPLATE = 'ipgei/pv_deliberation.html'

# Paysage : le tableau porte le rang, la moyenne, les deux décisions et le
# motif. En portrait, la colonne des motifs devient illisible.
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

EN_TETES = [
    'Rang', 'Matricule', 'Nom et prénom', 'Classe', 'Moyenne générale',
    'Total coef.', 'Décision proposée', 'Décision du jury', 'Motif', 'Observations',
]


def _lignes(deliberation: Deliberation):
    return (
        deliberation.lignes
        .select_related('inscription__etudiant', 'inscription__classe')
        .order_by('rang', '-moyenne_generale')
    )


def _libelle_decision(code: str) -> str:
    return dict(LigneDeliberation.DECISION_CHOICES).get(code, '')


def contexte(deliberation: Deliberation, *, base_url: str = '') -> dict:
    """Contexte de rendu du PV — partagé par le PDF et, en partie, par l'Excel."""
    lignes = list(_lignes(deliberation))
    membres = services.membres_jury_ordonnes(deliberation)

    # Répartition des décisions : le jury la relit avant de signer, et elle
    # évite de recompter à la main sur une promotion de 80 élèves.
    repartition = {}
    for ligne in lignes:
        if ligne.decision:
            repartition[ligne.decision] = repartition.get(ligne.decision, 0) + 1

    return {
        'deliberation':  deliberation,
        'institution':   deliberation.institution,
        'logo_b64':      _get_logo_b64(deliberation.institution),
        'titre':         'Procès-verbal de délibération',
        'est_projet':    deliberation.statut != Deliberation.STATUT_VALIDEE,
        'niveau_libelle': 'MPSI — 1re année' if deliberation.niveau == 'MPSI' else 'MP — 2e année',
        'portee_libelle': (f'Semestre {deliberation.semestre.code}'
                           if deliberation.semestre_id else 'Année complète'),
        'lignes':        lignes,
        'effectif':      len(lignes),
        'membres':       membres,
        'repartition':   [
            {'libelle': _libelle_decision(code), 'nombre': nombre}
            for code, nombre in sorted(repartition.items(), key=lambda c: -c[1])
        ],
        'date_edition':  date.today().strftime('%d/%m/%Y'),
        'base_url':      base_url,
    }


def generer_pdf(deliberation: Deliberation, *, base_url: str = '') -> bytes:
    return render_pdf_bytes(TEMPLATE, contexte(deliberation, base_url=base_url), OPTIONS_PDF)


def generer_excel(deliberation: Deliberation) -> bytes:
    """
    Classeur du PV : une feuille de résultats, une feuille de jury.

    L'Excel sert à retravailler les chiffres — il reprend donc les valeurs
    brutes (moyennes en nombres, pas en texte), là où le PDF fait foi.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    classeur = Workbook()
    feuille  = classeur.active
    feuille.title = 'Résultats'

    vert    = PatternFill('solid', fgColor='006633')
    blanc   = Font(color='FFFFFF', bold=True, size=10)
    bordure = Border(*[Side(style='thin', color='D5D5D5')] * 4)

    feuille['A1'] = f'Procès-verbal de délibération — {deliberation.niveau} '\
                    f'{deliberation.annee_universitaire}'
    feuille['A1'].font = Font(bold=True, size=13, color='006633')
    feuille['A2'] = (f'{"Semestre " + deliberation.semestre.code if deliberation.semestre_id else "Année complète"}'
                     f' · Seuil {deliberation.seuil_validation} · '
                     f'{deliberation.get_statut_display()}'
                     f'{" — PROJET, non validé" if deliberation.statut != Deliberation.STATUT_VALIDEE else ""}')
    feuille['A2'].font = Font(size=9, italic=True, color='555555')

    for colonne, libelle in enumerate(EN_TETES, start=1):
        cellule = feuille.cell(row=4, column=colonne, value=libelle)
        cellule.fill, cellule.font, cellule.border = vert, blanc, bordure
        cellule.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    for index, ligne in enumerate(_lignes(deliberation), start=5):
        etudiant = ligne.inscription.etudiant
        valeurs = [
            ligne.rang,
            etudiant.matricule,
            getattr(etudiant, 'nom_display', None) or etudiant.nom,
            ligne.inscription.classe.nom,
            float(ligne.moyenne_generale) if ligne.moyenne_generale is not None else None,
            float(ligne.total_coefficients) if ligne.total_coefficients is not None else None,
            _libelle_decision(ligne.decision_auto),
            _libelle_decision(ligne.decision),
            ligne.motif_ajustement,
            ligne.observations,
        ]
        for colonne, valeur in enumerate(valeurs, start=1):
            cellule = feuille.cell(row=index, column=colonne, value=valeur)
            cellule.border = bordure
            if colonne in (5, 6):
                cellule.number_format = '0.00'

    for colonne, largeur in zip('ABCDEFGHIJ', (7, 16, 30, 12, 15, 12, 24, 24, 34, 26)):
        feuille.column_dimensions[colonne].width = largeur
    feuille.freeze_panes = 'A5'

    jury = classeur.create_sheet('Jury')
    for colonne, libelle in enumerate(['Rôle', 'Membre', 'Signature'], start=1):
        cellule = jury.cell(row=1, column=colonne, value=libelle)
        cellule.fill, cellule.font = vert, blanc
    for index, membre in enumerate(services.membres_jury_ordonnes(deliberation), start=2):
        jury.cell(row=index, column=1, value=membre.get_role_display())
        jury.cell(row=index, column=2,
                  value=membre.utilisateur.name or membre.utilisateur.username)
        jury.cell(row=index, column=3,
                  value=(timezone.localtime(membre.signature_le).strftime('%d/%m/%Y %H:%M')
                         if membre.a_signe else 'Non signé'))
    for colonne, largeur in zip('ABC', (28, 34, 20)):
        jury.column_dimensions[colonne].width = largeur

    tampon = BytesIO()
    classeur.save(tampon)
    return tampon.getvalue()
