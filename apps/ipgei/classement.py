"""
Classement de S3 — la pièce de travail de l'affectation.

Au terme du premier semestre de deuxième année, l'établissement arrête un ordre
et répartit ses étudiants. Ce document sert cette séance : il apporte les deux
chiffres sur lesquels on tranche — la moyenne de PREMIÈRE ANNÉE, qui dit le
fond, et celle de S3, qui dit où en est l'étudiant — et laisse VIDES les deux
colonnes qu'on remplit à la main : la décision et l'affectation.

Elles restent vides à dessein. Ce n'est pas un procès-verbal : rien n'y est
prononcé, et le logiciel n'a aucune proposition à y porter. La feuille se
remplit en séance, se signe, et ce qui en sort est ensuite saisi ailleurs.

À la différence du PV de délibération, ce classement ne s'appuie sur aucune
délibération : il s'édite quand on veut, sans jury ouvert, à partir des notes
telles qu'elles sont. C'est précisément ce qu'on demande d'une pièce
préparatoire.
"""
import io
import logging
from datetime import date

from django.core.exceptions import ValidationError

from apps.documents.services import _get_logo_b64
from core.pdf_renderer import render_pdf_bytes
from core.pdf_utils import get_institution_context

from . import services
from .models import InscriptionIPGEI, SemestreIPGEI

logger = logging.getLogger('siga')

TEMPLATE = 'ipgei/classement_s3.html'

#: Le semestre classé. Nommé ici plutôt qu'écrit en clair dans quatre endroits :
#: le jour où l'établissement classera sur S1, il n'y aura qu'une ligne à lire.
CODE_SEMESTRE = 'S3'

OPTIONS_PDF = {
    'page-size':    'A4',
    'orientation':  'Portrait',
    'margin-top':   '12mm',
    'margin-bottom': '12mm',
    'margin-left':  '12mm',
    'margin-right': '12mm',
    'encoding':     'UTF-8',
    'enable-local-file-access': None,
}

EN_TETES = ['Matricule', 'Moyenne 1re année', 'Moyenne S3',
            'Décision', 'Affectation']


def _moyenne_premiere_annee(inscription: InscriptionIPGEI):
    """
    Moyenne annuelle de la PREMIÈRE année de cet étudiant.

    On remonte à sa dernière inscription de rang 1 — MPSI dans le cursus
    d'origine —, quelle qu'en soit l'année. Prendre « l'année précédente » aurait
    suffi au cas courant et manqué tous les autres : un redoublant de deuxième
    année a fait sa première année deux ans plus tôt, et sa moyenne ne doit pas
    disparaître de la feuille pour autant.

    Rendue nulle si aucune première année n'est retrouvée — un étudiant admis
    en cours de cursus, venu d'un autre établissement. La colonne reste alors
    vide, ce qui est exact : l'établissement n'a pas cette note.
    """
    precedentes = (
        InscriptionIPGEI.objects
        .filter(etudiant_id=inscription.etudiant_id)
        .exclude(pk=inscription.pk)
        .select_related('classe__niveau')
        .order_by('-annee_universitaire')
    )
    for candidate in precedentes:
        if services.rang_du_niveau(candidate.niveau,
                                   candidate.classe.institution) != 1:
            continue
        moyenne, _, _ = services.moyenne_annuelle(
            candidate, candidate.annee_universitaire, absents_a_zero=True)
        if moyenne is not None:
            return moyenne, candidate.annee_universitaire
    return None, ''


def lignes(annee_universitaire: str, niveau: str = '', institution=None,
           classe=None) -> list:
    """
    Les étudiants à classer, du meilleur S3 au moins bon.

    Seuls les inscrits de DEUXIÈME année : S3 est leur semestre, et un MPSI n'a
    rien à faire sur cette feuille. Le filtre passe par le rang du niveau et non
    par le code « MP », pour qu'un niveau ajouté au référentiel y entre sans
    retoucher le code.

    `classe` restreint à un seul groupe. Le rang, lui, reste calculé sur TOUT
    le périmètre demandé — une classe isolée donne le classement de cette
    classe, ce qui est le sens de la demande : on édite la feuille de MP A.

    Les moyennes sont calculées avec les absents comptés ZÉRO, comme au jury :
    une épreuve non rendue ne s'efface pas d'un classement qui sert à affecter.
    """
    inscrits = (InscriptionIPGEI.objects
                .filter(actif=True, annee_universitaire=annee_universitaire)
                .select_related('etudiant', 'classe__niveau', 'classe__institution'))
    if niveau:
        inscrits = inscrits.filter(classe__niveau__niveau=niveau)
    if classe:
        inscrits = inscrits.filter(classe_id=classe)

    semestre = SemestreIPGEI.objects.filter(
        code=CODE_SEMESTRE, annee_universitaire=annee_universitaire).first()

    resultats = []
    for inscription in inscrits:
        if services.rang_du_niveau(inscription.niveau,
                                   inscription.classe.institution) != 2:
            continue
        moyenne_s3 = None
        if semestre is not None:
            moyenne_s3, _, _ = services.moyenne_semestrielle(
                inscription, semestre, absents_a_zero=True)
        premiere, annee_premiere = _moyenne_premiere_annee(inscription)
        resultats.append({
            'inscription':    inscription,
            'matricule':      inscription.etudiant.matricule,
            'nom':            inscription.etudiant.nom_display,
            'classe':         inscription.classe.nom,
            'moyenne_annee1': premiere,
            'annee1':         annee_premiere,
            'moyenne_s3':     moyenne_s3,
        })

    # Du meilleur au moins bon : c'est un CLASSEMENT, et l'ordre est ce que la
    # feuille apporte. Sans moyenne, on ferme la marche plutôt que d'être
    # rangé premier par un tri qui prend l'absence pour un zéro.
    resultats.sort(key=lambda r: (r['moyenne_s3'] is not None, r['moyenne_s3'] or 0),
                   reverse=True)
    for rang, resultat in enumerate(resultats, start=1):
        resultat['rang'] = rang if resultat['moyenne_s3'] is not None else None
    return resultats


def _contexte(annee_universitaire: str, niveau: str, institution=None,
              classe=None) -> dict:
    resultats = lignes(annee_universitaire, niveau, institution, classe)
    if not resultats:
        raise ValidationError(
            f'Aucun inscrit de deuxième année en {annee_universitaire}'
            + (f' au niveau {niveau}' if niveau else '')
            + ". Le classement de S3 porte sur les étudiants de deuxième "
              "année : il n'y a rien à classer tant qu'aucun n'est inscrit."
        )

    donnees = get_institution_context()
    donnees.update({
        'annee_universitaire': annee_universitaire,
        # Le titre nomme ce qu'on édite : la classe si l'on en vise une, le
        # niveau sinon. « MP » sur une feuille qui ne porte que MP A ferait
        # croire à un classement de promotion.
        'niveau':        (_nom_classe(resultats) if classe
                          else niveau or 'toutes classes de 2e année'),
        'classe_seule':  bool(classe),
        'lignes':        resultats,
        'effectif':      len(resultats),
        'logo_b64':      _get_logo_b64(institution) if institution else '',
        'date_edition':  date.today().strftime('%d/%m/%Y'),
        'code_semestre': CODE_SEMESTRE,
    })
    return donnees


def _nom_classe(resultats: list) -> str:
    """Le nom de la classe éditée, tiré des lignes plutôt que d'une requête."""
    noms = {resultat['classe'] for resultat in resultats}
    return noms.pop() if len(noms) == 1 else ', '.join(sorted(noms))


def generer_pdf(annee_universitaire: str, niveau: str = '', institution=None,
                classe=None) -> bytes:
    return render_pdf_bytes(
        TEMPLATE, _contexte(annee_universitaire, niveau, institution, classe),
        OPTIONS_PDF)


def generer_excel(annee_universitaire: str, niveau: str = '', institution=None,
                  classe=None) -> bytes:
    """
    Le même classement en classeur — pour le retrier et le remplir à l'écran.

    Les deux dernières colonnes restent vides ici aussi. Elles sont larges :
    c'est là qu'on écrit, au clavier comme au stylo.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    from .collecte import _entete_excel, _logo_reduit

    donnees = _contexte(annee_universitaire, niveau, institution, classe)

    classeur = Workbook()
    feuille = classeur.active
    feuille.title = f'Classement {CODE_SEMESTRE}'

    VERT = '006633'
    trait = Side(style='thin', color='D5D5D5')
    bordure = Border(trait, trait, trait, trait)

    # Les largeurs AVANT le bandeau : il s'en sert pour centrer le logo entre le
    # bloc français et le bloc arabe, et les poser après le laisserait de biais.
    for colonne, largeur in enumerate((16, 20, 14, 26, 30), start=1):
        feuille.column_dimensions[get_column_letter(colonne)].width = largeur

    # Le même bandeau d'institution que le classeur de collecte : ces feuilles
    # circulent par courriel, et sans en-tête on ne sait plus de quel
    # établissement vient le fichier reçu. Le helper rend la première ligne
    # libre — tout ce qui suit s'y accroche plutôt qu'à des numéros en dur.
    depart = _entete_excel(feuille, donnees, len(EN_TETES),
                           _logo_reduit(donnees))

    feuille.cell(depart, 1, f'Classement {CODE_SEMESTRE} — {donnees["niveau"]} — '
                            f'{annee_universitaire}')
    feuille.cell(depart, 1).font = Font(bold=True, size=13, color=VERT)
    feuille.cell(depart + 1, 1,
                 f'{donnees["effectif"]} étudiant(s) · édité le '
                 f'{donnees["date_edition"]} · décision et affectation à compléter')
    feuille.cell(depart + 1, 1).font = Font(size=9, italic=True, color='555555')

    ligne_entetes = depart + 3
    for colonne, libelle in enumerate(EN_TETES, start=1):
        cellule = feuille.cell(row=ligne_entetes, column=colonne, value=libelle)
        cellule.font = Font(color='FFFFFF', bold=True, size=10)
        cellule.fill = PatternFill('solid', fgColor=VERT)
        cellule.border = bordure
        cellule.alignment = Alignment(horizontal='center', vertical='center',
                                      wrap_text=True)

    for index, ligne in enumerate(donnees['lignes'], start=ligne_entetes + 1):
        valeurs = [
            ligne['matricule'],
            float(ligne['moyenne_annee1']) if ligne['moyenne_annee1'] is not None else None,
            float(ligne['moyenne_s3']) if ligne['moyenne_s3'] is not None else None,
            None,   # Décision — remplie en séance
            None,   # Affectation — idem
        ]
        for colonne, valeur in enumerate(valeurs, start=1):
            cellule = feuille.cell(row=index, column=colonne, value=valeur)
            cellule.border = bordure
            if colonne in (2, 3):
                cellule.number_format = '0.00'
                cellule.alignment = Alignment(horizontal='center')
            elif colonne == 1:
                cellule.alignment = Alignment(horizontal='center')

    # Le volet figé suit la ligne d'en-têtes, qui a glissé sous le bandeau.
    feuille.freeze_panes = f'A{ligne_entetes + 1}'

    tampon = io.BytesIO()
    classeur.save(tampon)
    return tampon.getvalue()
