"""
Documents officiels IPGEI (plan §5) : relevé de semestre, relevé annuel,
décision de délibération, attestation d'autorisation CNIM.

On réutilise entièrement l'infrastructure `apps.documents` du socle — modèle
`DocumentOfficiel`, numérotation atomique, hash SHA-256, QR et page publique
`/verifier/{token}`. Rien de tout cela n'est réécrit : seul le CONTENU des
documents est propre à IPGEI.
"""
import hashlib
import logging
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.documents.models import DocumentOfficiel, NumeroSerieConfig
from apps.documents.services import (
    _get_qr_base64, _imagefield_b64, _get_logo_b64, enregistrer_pdf_document,
)
from core.pdf_renderer import render_pdf_bytes

from . import services
from .models import Deliberation, InscriptionIPGEI, LigneDeliberation, SemestreIPGEI

logger = logging.getLogger('siga')

TYPE_RELEVE_SEMESTRE = 'ipgei_releve_semestre'
TYPE_RELEVE_ANNUEL   = 'ipgei_releve_annuel'
TYPE_DECISION        = 'ipgei_decision_deliberation'
TYPE_CNIM            = 'ipgei_attestation_cnim'
TYPE_ATTESTATION     = 'ipgei_attestation_inscription'
TYPE_RECU            = 'ipgei_recu_paiement'

TYPES_IPGEI = [TYPE_RELEVE_SEMESTRE, TYPE_RELEVE_ANNUEL, TYPE_DECISION, TYPE_CNIM,
               TYPE_ATTESTATION, TYPE_RECU]

# Préfixes de numérotation. Distincts de ceux du socle LMD pour qu'un numéro
# identifie sans ambiguïté le document IPGEI dont il provient.
PREFIXES = {
    TYPE_RELEVE_SEMESTRE: 'IRS',
    TYPE_RELEVE_ANNUEL:   'IRA',
    TYPE_DECISION:        'IDD',
    TYPE_CNIM:            'ICN',
    TYPE_ATTESTATION:     'IAI',
    TYPE_RECU:            'IRP',
}

TEMPLATES = {
    TYPE_RELEVE_SEMESTRE: 'ipgei/releve_semestre.html',
    TYPE_RELEVE_ANNUEL:   'ipgei/releve_annuel.html',
    TYPE_DECISION:        'ipgei/decision_deliberation.html',
    TYPE_CNIM:            'ipgei/attestation_cnim.html',
    TYPE_ATTESTATION:     'ipgei/attestation_inscription.html',
    TYPE_RECU:            'ipgei/recu_paiement.html',
}

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


# ═════════════════════════════════════════════════════════════════════════════
# Création du document
# ═════════════════════════════════════════════════════════════════════════════
def _numero_serie(institution, type_document: str) -> str:
    config, _ = NumeroSerieConfig.objects.get_or_create(
        institution=institution, type_document=type_document,
        defaults={'prefixe': PREFIXES[type_document], 'nb_chiffres': 5},
    )
    return config.generer_prochain()


@transaction.atomic
def creer_document(inscription: InscriptionIPGEI, type_document: str, *,
                   semestre: SemestreIPGEI = None, utilisateur=None) -> DocumentOfficiel:
    """
    Enregistre le document au registre (numéro + token de vérification).

    Un document est réémis à chaque demande plutôt que réutilisé : chaque tirage
    a son propre numéro, ce qui permet de tracer qui a reçu quoi et quand.
    """
    if type_document not in TYPES_IPGEI:
        raise ValidationError(f'Type de document IPGEI inconnu : {type_document}')

    institution = inscription.classe.institution
    return DocumentOfficiel.objects.create(
        institution=institution,
        etudiant=inscription.etudiant,
        type_document=type_document,
        numero_serie=_numero_serie(institution, type_document),
        annee_universitaire=inscription.annee_universitaire,
        genere_par=utilisateur if getattr(utilisateur, 'is_authenticated', False) else None,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Contexte de rendu
# ═════════════════════════════════════════════════════════════════════════════
def _contexte_commun(doc: DocumentOfficiel, inscription: InscriptionIPGEI,
                     base_url: str = '') -> dict:
    institution = doc.institution
    etudiant    = inscription.etudiant
    return {
        'doc':          doc,
        'institution':  institution,
        'etudiant':     etudiant,
        'inscription':  inscription,
        'classe':       inscription.classe,
        'niveau':       inscription.classe.niveau_code,
        'annee_universitaire': inscription.annee_universitaire,
        'numero_serie': doc.numero_serie,
        'date_edition': date.today().strftime('%d/%m/%Y'),
        'logo_b64':     _get_logo_b64(institution),
        'logo_groupe_b64':  _imagefield_b64(getattr(institution, 'logo_groupe', None)),
        'signature_b64':    _imagefield_b64(getattr(institution, 'directeur_signature', None)),
        'photo_b64':    _imagefield_b64(getattr(etudiant, 'photo', None)),
        'qr_b64':       _get_qr_base64(str(doc.token_verification), base_url, etudiant=etudiant, doc=doc),
        'verify_url':   f'{base_url}/verifier/{doc.token_verification}' if base_url else '',
    }


def _contexte_releve_semestre(doc, inscription, semestre, base_url) -> dict:
    donnees = services.releve_semestre(inscription, semestre)
    contexte = _contexte_commun(doc, inscription, base_url)
    contexte.update({
        'titre':    'RELEVÉ DE NOTES — SEMESTRE',
        'semestre': semestre,
        'lignes':   [_ligne_note(n) for n in donnees['notes']],
        'moyenne':  donnees['moyenne'],
        'total_coefficients': donnees['total_coefficients'],
        'seuil':    donnees['seuil'],
        'valide':   donnees['valide'],
        'absences': donnees['absences'],
    })
    return contexte


def _contexte_releve_annuel(doc, inscription, base_url) -> dict:
    donnees = services.releve_annuel(inscription, inscription.annee_universitaire)
    contexte = _contexte_commun(doc, inscription, base_url)
    contexte.update({
        'titre':     'RELEVÉ DE NOTES — ANNÉE',
        'semestres': [
            {
                'code':    bloc['code'],
                'moyenne': bloc['moyenne'],
                'lignes':  [_ligne_note(n) for n in bloc['notes']],
            }
            for bloc in donnees['semestres']
        ],
        'moyenne':  donnees['moyenne'],
        'total_coefficients': donnees['total_coefficients'],
        'seuil':    donnees['seuil'],
        'valide':   donnees['valide'],
        'absences': donnees['absences'],
    })
    return contexte


def _ligne_note(note) -> dict:
    """Aplati une ligne de note pour le template (pas de logique dans le HTML)."""
    coef = note.coefficient if note.coefficient is not None else note.matiere.coefficient
    return {
        'code':           note.matiere.code,
        'intitule':       note.matiere.intitule,
        'coefficient':    coef,
        'volume_horaire': note.matiere.volume_horaire,
        'moy_ds':         note.moy_ds,
        'note_tp':        note.note_tp if note.matiere.has_tp else None,
        'moy_exam':       note.moy_exam,
        'moyenne':        note.moyenne,
        'rattrapage':     note.note_rattrapage,
        'note_retenue':   note.note_retenue,
        'ponderation':    _libelle_ponderation(note),
        'nb_ds':          note.evaluations.filter(type_evaluation='ds').count(),
        'nb_exam':        note.evaluations.filter(type_evaluation='exam').count(),
    }


def _libelle_ponderation(note) -> str:
    """
    Affiche la pondération TELLE QU'ELLE A SERVI au calcul (snapshot), pas celle
    de la matière aujourd'hui — c'est la garantie que le relevé reste lisible
    même après un changement de paramétrage.
    """
    if note.pct_ds is None:
        return ''
    parts = [f'DS {note.pct_ds:g}%']
    if note.pct_tp:
        parts.append(f'TP {note.pct_tp:g}%')
    parts.append(f'Exam {note.pct_exam:g}%')
    return ' · '.join(parts)


def _contexte_decision(doc, inscription, ligne: LigneDeliberation, base_url) -> dict:
    deliberation = ligne.deliberation
    contexte = _contexte_commun(doc, inscription, base_url)
    contexte.update({
        'titre':        'DÉCISION DE DÉLIBÉRATION',
        'deliberation': deliberation,
        'ligne':        ligne,
        'moyenne':      ligne.moyenne_generale,
        'rang':         ligne.rang,
        'decision':     ligne.get_decision_display(),
        'seuil':        deliberation.seuil_validation,
        'date_jury':    deliberation.date_validation or deliberation.date_creation,
        'details':      ligne.details.select_related('matiere', 'semestre').all(),
        'effectif':     deliberation.lignes.count(),
    })
    return contexte


def _contexte_cnim(doc, inscription, ligne: LigneDeliberation, base_url) -> dict:
    deliberation = ligne.deliberation
    contexte = _contexte_commun(doc, inscription, base_url)
    contexte.update({
        'titre':        "ATTESTATION D'AUTORISATION À CONCOURIR (CNIM)",
        'deliberation': deliberation,
        'ligne':        ligne,
        'moyenne':      ligne.moyenne_generale,
        'rang':         ligne.rang,
        'seuil':        deliberation.seuil_validation,
        'date_jury':    deliberation.date_validation or deliberation.date_creation,
        'effectif':     deliberation.lignes.count(),
    })
    return contexte


# ═════════════════════════════════════════════════════════════════════════════
# Génération
# ═════════════════════════════════════════════════════════════════════════════
def generer_pdf(doc: DocumentOfficiel, contexte: dict) -> bytes:
    """Rend le PDF, l'attache au document et scelle son empreinte SHA-256."""
    pdf = render_pdf_bytes(TEMPLATES[doc.type_document], contexte, OPTIONS_PDF)

    doc.hash_sha256 = hashlib.sha256(pdf).hexdigest()
    if doc.premiere_generation is None:
        doc.premiere_generation = timezone.now()
    doc.save(update_fields=['hash_sha256', 'premiere_generation'])
    enregistrer_pdf_document(doc, pdf)
    return pdf


def generer_releve_semestre(inscription, semestre, *, utilisateur=None, base_url=''):
    # `DocumentOfficiel.semestre` pointe sur le `parametres.Semestre` du socle
    # LMD : on le laisse vide, le semestre IPGEI figurant dans le PDF et dans le
    # contexte de rendu.
    doc = creer_document(inscription, TYPE_RELEVE_SEMESTRE, semestre=semestre, utilisateur=utilisateur)
    pdf = generer_pdf(doc, _contexte_releve_semestre(doc, inscription, semestre, base_url))
    return doc, pdf


def generer_releve_annuel(inscription, *, utilisateur=None, base_url=''):
    doc = creer_document(inscription, TYPE_RELEVE_ANNUEL, utilisateur=utilisateur)
    pdf = generer_pdf(doc, _contexte_releve_annuel(doc, inscription, base_url))
    return doc, pdf


def _ligne_validee(deliberation: Deliberation, inscription: InscriptionIPGEI) -> LigneDeliberation:
    """
    Récupère la ligne de jury de l'étudiant, en exigeant une délibération
    validée : émettre une décision officielle depuis un brouillon reviendrait à
    délivrer un document qui peut encore changer.
    """
    if not deliberation.est_verrouillee:
        raise ValidationError(
            'La délibération doit être validée avant toute émission de document officiel.'
        )
    ligne = deliberation.lignes.filter(inscription=inscription).first()
    if ligne is None:
        raise ValidationError("Cet étudiant ne figure pas dans cette délibération.")
    return ligne


def generer_decision(deliberation, inscription, *, utilisateur=None, base_url=''):
    ligne = _ligne_validee(deliberation, inscription)
    doc = creer_document(inscription, TYPE_DECISION, utilisateur=utilisateur)
    pdf = generer_pdf(doc, _contexte_decision(doc, inscription, ligne, base_url))
    return doc, pdf


def generer_attestation_cnim(deliberation, inscription, *, utilisateur=None, base_url=''):
    """
    L'attestation CNIM ne s'émet que pour un étudiant effectivement autorisé à
    concourir : c'est le document qui ouvre l'accès au concours.
    """
    ligne = _ligne_validee(deliberation, inscription)
    if ligne.decision != LigneDeliberation.DECISION_CNIM:
        raise ValidationError(
            "L'attestation CNIM ne concerne que les étudiants autorisés à concourir "
            f"(décision actuelle : {ligne.get_decision_display() or 'aucune'})."
        )
    doc = creer_document(inscription, TYPE_CNIM, utilisateur=utilisateur)
    pdf = generer_pdf(doc, _contexte_cnim(doc, inscription, ligne, base_url))
    return doc, pdf


# ═════════════════════════════════════════════════════════════════════════════
# Attestation d'inscription et reçu de paiement
# ═════════════════════════════════════════════════════════════════════════════
def _contexte_attestation(doc, inscription, base_url) -> dict:
    """
    L'attestation dit ce que l'étudiant suit, pas ce qu'il a obtenu.

    À l'IPGEI l'inscription pédagogique est entière et automatique : la
    maquette du niveau EST la liste de ce qu'il suit. On la joint donc, ce qui
    évite d'avoir à produire un second document pour la justifier.
    """
    contexte = _contexte_commun(doc, inscription, base_url)
    matieres = []
    for semestre in services.semestres_du_niveau(
        inscription.classe.niveau_code, inscription.annee_universitaire,
    ):
        for note in (inscription.notes
                     .filter(semestre=semestre)
                     .select_related('matiere')
                     .order_by('matiere__code')):
            matieres.append({
                'semestre':    semestre.code,
                'code':        note.matiere.code,
                'intitule':    note.matiere.intitule,
                'coefficient': note.matiere.coefficient,
            })
    contexte.update({
        'titre':    "ATTESTATION D'INSCRIPTION",
        'matieres': matieres,
        'libelle_niveau': ('1re année — MPSI' if inscription.classe.niveau_code == 'MPSI'
                           else '2e année — MP'),
    })
    return contexte


def _contexte_recu(doc, inscription, base_url) -> dict:
    contexte = _contexte_commun(doc, inscription, base_url)
    contexte.update({
        'titre':          'REÇU DE PAIEMENT',
        'montant':        inscription.montant_frais,
        'date_paiement':  inscription.date_paiement,
        'reference':      inscription.recu_paiement,
        'libelle_niveau': ('1re année — MPSI' if inscription.classe.niveau_code == 'MPSI'
                           else '2e année — MP'),
    })
    return contexte


def generer_attestation_inscription(inscription, *, utilisateur=None, base_url=''):
    """
    Attestation de scolarité de l'année en cours.

    Refusée sur une inscription inactive : attester d'une scolarité abandonnée
    ou annulée ferait de ce document un faux, et il circule hors de
    l'établissement.
    """
    if not inscription.actif:
        raise ValidationError(
            "Cette inscription n'est pas active : aucune attestation ne peut en être tirée."
        )
    doc = creer_document(inscription, TYPE_ATTESTATION, utilisateur=utilisateur)
    pdf = generer_pdf(doc, _contexte_attestation(doc, inscription, base_url))
    return doc, pdf


def generer_recu_paiement(inscription, *, utilisateur=None, base_url=''):
    """
    Reçu des frais d'inscription.

    Exige le paiement enregistré : un reçu émis avant encaissement vaut
    quittance, et rien ne permettrait ensuite d'établir que la somme est
    toujours due.
    """
    if not inscription.est_payee:
        raise ValidationError(
            "Les frais d'inscription ne sont pas enregistrés comme payés : "
            'aucun reçu ne peut être délivré.'
        )
    doc = creer_document(inscription, TYPE_RECU, utilisateur=utilisateur)
    pdf = generer_pdf(doc, _contexte_recu(doc, inscription, base_url))
    return doc, pdf
