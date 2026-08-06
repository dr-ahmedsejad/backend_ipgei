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
from decimal import Decimal
from io import BytesIO

from django.utils import timezone

from apps.documents.services import _get_logo_b64
from core.pdf_renderer import render_pdf_bytes
from core.pdf_utils import get_institution_context

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
        .prefetch_related('details__semestre')
        .order_by('rang', '-moyenne_generale')
    )


def _moyennes_par_semestre(lignes, codes: list) -> None:
    """
    Pose sur chaque ligne la moyenne de chaque semestre de l'année.

    Un PV annuel qui ne montre que la moyenne générale ne dit pas ce qui la
    compose : le jury voit un 9,80 sans savoir s'il vient d'un semestre raté
    rattrapé, ou de deux semestres également faibles — et c'est justement la
    différence qui fonde une décision.

    Calculées sur les DÉTAILS figés à la délibération, non recalculées depuis
    les notes : le PV doit dire ce que le jury a vu, même si une note a bougé
    depuis.
    """
    for ligne in lignes:
        cumuls = {code: [Decimal('0'), Decimal('0')] for code in codes}
        for detail in ligne.details.all():
            code = detail.semestre.code if detail.semestre_id else None
            if code not in cumuls or detail.note_retenue is None:
                continue
            coef = Decimal(detail.coefficient or 0)
            cumuls[code][0] += Decimal(detail.note_retenue) * coef
            cumuls[code][1] += coef
        ligne.par_semestre = [
            (cumul / poids).quantize(Decimal('0.01')) if poids else None
            for cumul, poids in (cumuls[code] for code in codes)
        ]


def _libelle_decision(code: str) -> str:
    return dict(LigneDeliberation.DECISION_CHOICES).get(code, '')


def contexte(deliberation: Deliberation, *, base_url: str = '') -> dict:
    """Contexte de rendu du PV — partagé par le PDF et, en partie, par l'Excel."""
    lignes = list(_lignes(deliberation))
    membres = services.membres_jury_ordonnes(deliberation)

    # Colonnes de semestre : seulement sur un PV ANNUEL. Sur un PV de semestre,
    # elles répéteraient la moyenne générale dans une colonne voisine.
    codes_semestres = (
        services.codes_semestres_du_niveau(deliberation.niveau, deliberation.institution)
        if deliberation.portee == Deliberation.PORTEE_ANNUELLE else []
    )
    if codes_semestres:
        _moyennes_par_semestre(lignes, codes_semestres)

    # Répartition des décisions : le jury la relit avant de signer, et elle
    # évite de recompter à la main sur une promotion de 80 élèves.
    repartition = {}
    for ligne in lignes:
        if ligne.decision:
            repartition[ligne.decision] = repartition.get(ligne.decision, 0) + 1

    # L'en-tête partagé lit `inst_nom_fr`, `inst_logo_url`… : sans ce contexte
    # il se rendrait vide, sans la moindre erreur.
    donnees = get_institution_context()
    donnees.update({
        'deliberation':  deliberation,
        'institution':   deliberation.institution,
        'logo_b64':      _get_logo_b64(deliberation.institution),
        # Le titre nomme la CLASSE, comme les PV que l'institut édite depuis
        # toujours : « PV des Résultats annuels de la Classe MPSI A ». Sans
        # elle, deux PV d'un même niveau ne se distinguent qu'au contenu.
        'titre':         services.libelle_pv(deliberation),
        'codes_semestres': codes_semestres,
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
    })
    return donnees


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
    from openpyxl.utils import get_column_letter

    classeur = Workbook()
    feuille  = classeur.active
    feuille.title = 'Résultats'

    vert    = PatternFill('solid', fgColor='006633')
    blanc   = Font(color='FFFFFF', bold=True, size=10)
    bordure = Border(*[Side(style='thin', color='D5D5D5')] * 4)

    feuille['A1'] = (f'{services.libelle_pv(deliberation)} — '
                     f'{deliberation.annee_universitaire}')
    feuille['A1'].font = Font(bold=True, size=13, color='006633')
    feuille['A2'] = (f'{"Semestre " + deliberation.semestre.code if deliberation.semestre_id else "Année complète"}'
                     f' · Seuil {deliberation.seuil_validation} · '
                     f'{deliberation.get_statut_display()}'
                     f'{" — PROJET, non validé" if deliberation.statut != Deliberation.STATUT_VALIDEE else ""}')
    feuille['A2'].font = Font(size=9, italic=True, color='555555')

    # Mêmes colonnes que le PDF, semestres compris : le classeur sert à
    # retrier et à recompter, il ne doit pas montrer moins que la feuille
    # signée.
    codes_semestres = (
        services.codes_semestres_du_niveau(deliberation.niveau, deliberation.institution)
        if deliberation.portee == Deliberation.PORTEE_ANNUELLE else []
    )
    entetes = EN_TETES[:4] + list(codes_semestres) + EN_TETES[4:]
    for colonne, libelle in enumerate(entetes, start=1):
        cellule = feuille.cell(row=4, column=colonne, value=libelle)
        cellule.fill, cellule.font, cellule.border = vert, blanc, bordure
        cellule.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    lignes = list(_lignes(deliberation))
    if codes_semestres:
        _moyennes_par_semestre(lignes, codes_semestres)

    for index, ligne in enumerate(lignes, start=5):
        etudiant = ligne.inscription.etudiant
        valeurs = [
            ligne.rang,
            etudiant.matricule,
            getattr(etudiant, 'nom_display', None) or etudiant.nom,
            ligne.inscription.classe.nom,
            *[float(m) if m is not None else None
              for m in getattr(ligne, 'par_semestre', [])],
            float(ligne.moyenne_generale) if ligne.moyenne_generale is not None else None,
            float(ligne.total_coefficients) if ligne.total_coefficients is not None else None,
            _libelle_decision(ligne.decision_auto),
            _libelle_decision(ligne.decision),
            ligne.motif_ajustement,
            ligne.observations,
        ]
        # Les colonnes numériques suivent le nombre de semestres insérés : les
        # figer à (5, 6) laissait la moyenne générale en texte dès qu'une
        # colonne s'ajoutait devant.
        numeriques = set(range(5, 5 + len(codes_semestres) + 2))
        for colonne, valeur in enumerate(valeurs, start=1):
            cellule = feuille.cell(row=index, column=colonne, value=valeur)
            cellule.border = bordure
            if colonne in numeriques:
                cellule.number_format = '0.00'

    largeurs = ([7, 16, 30, 12] + [9] * len(codes_semestres)
                + [15, 12, 24, 24, 34, 26])
    for index_colonne, largeur in enumerate(largeurs, start=1):
        feuille.column_dimensions[
            get_column_letter(index_colonne)].width = largeur
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
