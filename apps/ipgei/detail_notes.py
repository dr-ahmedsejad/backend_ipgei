"""
Détail des notes d'une délibération de semestre — édition PDF.

Document de séance : le jury le lit élève par élève, alors que le PV donne une
ligne par élève. On y trouve, pour chacun, sa photo, son numéro d'anonymat, sa
moyenne, son rang, puis le détail matière par matière avec la moyenne de la
classe en regard — c'est ce vis-à-vis qui permet de juger un 12 sans avoir à
feuilleter les autres copies.

Collectif comme le PV, il ne passe donc pas par `DocumentOfficiel`, dont chaque
pièce est rattachée à un étudiant et à un numéro de série individuel.
"""
import logging
from datetime import date

from apps.documents.services import _get_logo_b64
from core.pdf_renderer import render_pdf_bytes

from . import services
from .models import AnonymatIPGEI, Deliberation, Note, SessionEvaluationIPGEI

logger = logging.getLogger('siga')

TEMPLATE = 'ipgei/detail_notes.html'

# Portrait : chaque élève tient dans un bloc étroit, trois par page. En
# paysage, les blocs s'étalent et l'on perd la lecture verticale du classement.
OPTIONS_PDF = {
    'page-size':     'A4',
    'orientation':   'Portrait',
    # Marges hautes et basses réservées au bandeau et au pied répétés.
    'margin-top':    '16mm',
    'margin-bottom': '14mm',
    'margin-left':   '10mm',
    'margin-right':  '10mm',
    'encoding':      'UTF-8',
    'enable-local-file-access': None,
}

HEADER = 'ipgei/_detail_notes_header.html'
PIED   = 'ipgei/_detail_notes_footer.html'


def _photo_b64(etudiant) -> str:
    """Photo de l'étudiant en base64, ou chaîne vide. Jamais d'exception."""
    from .documents import _imagefield_b64

    try:
        return _imagefield_b64(getattr(etudiant, 'photo', None)) or ''
    except Exception:                                    # noqa: BLE001
        logger.exception('Photo illisible pour %s', etudiant)
        return ''


def _anonymats(deliberation: Deliberation) -> dict:
    """
    Numéro d'anonymat par inscription, tiré de la session normale du semestre.

    Le document de séance le porte pour qu'on puisse remonter d'une copie à son
    auteur pendant la délibération — c'est là, et seulement là, que le lien se
    fait. Vide si aucun anonymat n'a été tiré.
    """
    if not deliberation.semestre_id:
        return {}
    session = (
        SessionEvaluationIPGEI.objects
        .filter(semestre_id=deliberation.semestre_id,
                type_session=SessionEvaluationIPGEI.TYPE_NORMALE)
        .order_by('-id').first()
    )
    if session is None:
        return {}
    return dict(
        AnonymatIPGEI.objects.filter(session=session)
        .values_list('inscription_id', 'numero')
    )


def contexte(deliberation: Deliberation) -> dict:
    """
    Contexte de rendu — un bloc par élève, classés par moyenne décroissante.

    Les notes sont lues une seule fois pour toute la promotion, puis
    redistribuées en mémoire : une requête par élève et par matière aurait
    coûté plusieurs centaines d'allers-retours sur une promotion de trente-sept.
    """
    semestre = deliberation.semestre
    lignes = list(
        deliberation.lignes
        .select_related('inscription__etudiant', 'inscription__classe',
                        'inscription__sous_groupe')
        .order_by('rang', '-moyenne_generale')
    )
    anonymats = _anonymats(deliberation)

    # Toutes les notes du semestre pour les inscrits délibérés, en une passe.
    inscriptions = [ligne.inscription_id for ligne in lignes]
    notes = list(
        Note.objects
        .filter(semestre=semestre, inscription_id__in=inscriptions)
        .select_related('matiere')
        .order_by('matiere__ordre', 'matiere__code')
    )
    par_inscription = {}
    for note in notes:
        par_inscription.setdefault(note.inscription_id, []).append(note)

    # Moyenne de la classe par matière, et rang de chacun dedans. Reprend la
    # règle du relevé : les copies sans note ne pèsent pas et ne sont pas
    # classées, les ex æquo partagent leur rang.
    valeurs = {}
    for note in notes:
        if note.note_retenue is not None:
            valeurs.setdefault(note.matiere_id, []).append(
                (note.inscription_id, note.note_retenue))

    stats = {}
    for matiere_id, lot in valeurs.items():
        notes_seules = [n for _, n in lot]
        stats[matiere_id] = {
            'moyenne': sum(notes_seules) / len(notes_seules),
            'rangs':   {i: sum(1 for n in notes_seules if n > note) + 1
                        for i, note in lot},
        }

    # Moyenne générale de la promotion — le « MC » global du document, repère
    # unique répété en tête de chaque bloc.
    moyennes = [ligne.moyenne_generale for ligne in lignes
                if ligne.moyenne_generale is not None]
    moyenne_promotion = sum(moyennes) / len(moyennes) if moyennes else None

    blocs = []
    for ligne in lignes:
        inscription = ligne.inscription
        detail = []
        for note in par_inscription.get(inscription.pk, []):
            stat = stats.get(note.matiere_id, {})
            detail.append({
                'coefficient': (note.coefficient if note.coefficient is not None
                                else note.matiere.coefficient),
                'intitule':    note.matiere.intitule,
                'ds':          note.moy_ds,
                'tp':          note.note_tp if note.matiere.has_tp else None,
                'examen':      note.moy_exam,
                'me':          note.note_retenue,
                'mc':          stat.get('moyenne'),
                'rang':        (stat.get('rangs') or {}).get(inscription.pk),
            })
        blocs.append({
            'matricule':  inscription.etudiant.matricule,
            'nom':        inscription.etudiant.nom_display,
            'anonymat':   anonymats.get(inscription.pk),
            'photo_b64':  _photo_b64(inscription.etudiant),
            'moyenne':    ligne.moyenne_generale,
            'rang':       ligne.rang,
            'groupe':     (inscription.sous_groupe.libelle
                           if inscription.sous_groupe_id else ''),
            'detail':     detail,
        })

    return {
        'deliberation':      deliberation,
        'institution':       deliberation.institution,
        'logo_b64':          _get_logo_b64(deliberation.institution),
        'est_projet':        deliberation.statut != Deliberation.STATUT_VALIDEE,
        'libelle_cursus':    services.libelle_cursus(deliberation.niveau,
                                             deliberation.institution),
        'classe_libelle':    _classes_libelle(lignes),
        'semestre_libelle':  services.libelle_semestre_ordinal(semestre.code) if semestre else '',
        'effectif':          len(lignes),
        'moyenne_promotion': moyenne_promotion,
        'blocs':             blocs,
        'date_edition':      date.today().strftime('%d/%m/%Y'),
    }


def _classes_libelle(lignes) -> str:
    """Classes représentées, dédupliquées et ordonnées."""
    noms = sorted({ligne.inscription.classe.nom for ligne in lignes})
    return ', '.join(noms)


def _fichier_temporaire(gabarit: str, donnees: dict) -> str:
    """
    Rend un gabarit dans un fichier temporaire et renvoie son URL `file:///`.

    wkhtmltopdf n'accepte header et footer que sous forme de documents
    autonomes, pas de fragments du corps.
    """
    import tempfile

    from django.template.loader import get_template

    fichier = tempfile.NamedTemporaryFile(
        'w', suffix='.html', delete=False, encoding='utf-8')
    fichier.write(get_template(gabarit).render(donnees))
    fichier.close()
    # URL file:// et non un chemin Windows : les antislashs y échouent.
    return fichier.name


def generer_pdf(deliberation: Deliberation) -> bytes:
    """
    PDF du détail des notes, bandeau et pied répétés sur chaque page.

    Réservé aux délibérations de SEMESTRE : une annuelle porte deux semestres,
    dont les matières n'ont ni les mêmes coefficients ni parfois les mêmes
    intitulés — les juxtaposer sur une ligne unique produirait un tableau faux.
    """
    import os

    if deliberation.portee != Deliberation.PORTEE_SEMESTRE:
        from django.core.exceptions import ValidationError
        raise ValidationError(
            'Le détail des notes est un document de semestre. Pour une '
            'délibération annuelle, éditez celui de chaque semestre.'
        )

    donnees = contexte(deliberation)
    options = dict(OPTIONS_PDF)
    fichiers = []
    try:
        for cle, gabarit in (('header-html', HEADER), ('footer-html', PIED)):
            chemin = _fichier_temporaire(gabarit, donnees)
            fichiers.append(chemin)
            options[cle] = 'file:///' + chemin.replace(os.sep, '/')
        options['header-spacing'] = '4'
        options['footer-spacing'] = '3'
        return render_pdf_bytes(TEMPLATE, donnees, options)
    finally:
        for chemin in fichiers:
            try:
                os.remove(chemin)
            except OSError:
                pass
