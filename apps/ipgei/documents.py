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
from django.db.models import Q
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

# Options de l'attestation, reprises du socle avec son gabarit : marges nulles
# (le gabarit pose les siennes dans `.page`), et 8 mm réservés en bas pour le
# pied de page natif — mesuré pour poser la barre à ~5 mm du bord sans faire
# déborder sur une seconde page.
OPTIONS_PDF_SOCLE = {
    'page-size':    'A4',
    'encoding':     'UTF-8',
    'enable-local-file-access': None,
    'load-error-handling':       'ignore',
    'load-media-error-handling': 'ignore',
    'dpi':           150,
    'margin-top':    '0',
    'margin-bottom': '8mm',
    'margin-left':   '0',
    'margin-right':  '0',
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
        # « Math Physique Sciences de l'Ingénieur (MPSI A) » : le code seul ne
        # dit rien à qui lit le document hors de l'établissement.
        'classe_label': services.libelle_classe(inscription.classe),
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


# Intitulés arabes des semestres, pour le titre et l'en-tête du tableau.
SEMESTRE_AR = {
    'S1': 'السداسي الأول', 'S2': 'السداسي الثاني',
    'S3': 'السداسي الثالث', 'S4': 'السداسي الرابع',
}


def _contexte_releve_semestre(doc, inscription, semestre, base_url) -> dict:
    donnees = services.releve_semestre(inscription, semestre)
    contexte = _contexte_commun(doc, inscription, base_url)
    niveau   = inscription.classe.niveau_code
    contexte.update({
        'titre':    'RELEVÉ DE NOTES — SEMESTRE',
        'semestre': semestre,
        'lignes':   [_ligne_note(n, donnees['stats_classe']) for n in donnees['notes']],
        'moyenne':  donnees['moyenne'],
        'total_coefficients': donnees['total_coefficients'],
        'seuil':    donnees['seuil'],
        'valide':   donnees['valide'],
        'absences': donnees['absences'],
        'rang_general': donnees['rang_general'],
        # Libellés attendus par le gabarit repris du socle.
        'semestre_label':    f'SEMESTRE {semestre.code[1:]}',
        'semestre_label_ar': SEMESTRE_AR.get(semestre.code, semestre.code),
        # La signature est bilingue : la ville aussi, sans quoi le côté droit
        # mêlerait un nom latin à une phrase arabe.
        'ville':    getattr(doc.institution, 'ville_fr', '') or 'Nouakchott',
        'ville_ar': getattr(doc.institution, 'ville_ar', '') or 'نواكشوط',
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


def _ligne_note(note, stats_classe: dict | None = None) -> dict:
    """Aplati une ligne de note pour le template (pas de logique dans le HTML)."""
    coef = note.coefficient if note.coefficient is not None else note.matiere.coefficient
    stats = (stats_classe or {}).get(note.matiere_id, {})
    return {
        # Repères de classe : la moyenne de la matière et le rang qu'y tient
        # l'étudiant. Une note isolée ne dit pas si 12 est un bon résultat.
        'moyenne_classe': stats.get('moyenne_classe'),
        'rang_matiere':   stats.get('rang'),
        'effectif_classe': stats.get('effectif'),
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
def _rendre(type_document: str, contexte: dict) -> bytes:
    """
    Rend le gabarit d'un document avec les options qui lui conviennent.

    L'attestation reprend le gabarit du socle, qui pose lui-même ses marges dans
    `.page` et attend donc des marges nulles côté wkhtmltopdf — les 12 mm des
    autres documents IPGEI les doubleraient. Elle porte en outre un pied de page
    natif : le corps de 240 mm est mis à une échelle imprévisible et sa hauteur
    ignorée, si bien qu'aucun CSS ne colle une barre au bas de la page.
    """
    PIEDS = {
        TYPE_ATTESTATION:     'ipgei/_attestation_footer.html',
        TYPE_RELEVE_SEMESTRE: 'ipgei/_releve_footer.html',
    }
    if type_document not in PIEDS:
        return render_pdf_bytes(TEMPLATES[type_document], contexte, OPTIONS_PDF)

    import os
    import tempfile

    from django.template.loader import get_template

    from core.arabe import normaliser_arabe

    options = dict(OPTIONS_PDF_SOCLE)
    fichier = tempfile.NamedTemporaryFile(
        'w', suffix='.html', delete=False, encoding='utf-8')
    try:
        # `render_pdf_bytes` normalise l'arabe du corps, mais ce pied de page est
        # rendu ici : sans le même passage, sa ligne arabe sortirait en variantes
        # non standard.
        fichier.write(normaliser_arabe(
            get_template(PIEDS[type_document]).render(contexte)))
        fichier.close()
        # URL file:// et non un chemin Windows : les antislashs y échouent.
        options['footer-html']    = 'file:///' + fichier.name.replace('\\', '/')
        options['footer-spacing'] = '0'
        return render_pdf_bytes(TEMPLATES[type_document], contexte, options)
    finally:
        try:
            os.remove(fichier.name)
        except OSError:
            pass


def generer_pdf(doc: DocumentOfficiel, contexte: dict) -> bytes:
    """Rend le PDF, l'attache au document et scelle son empreinte SHA-256."""
    pdf = _rendre(doc.type_document, contexte)

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
    from .models import Matiere

    contexte = _contexte_commun(doc, inscription, base_url)
    institution = doc.institution
    etudiant    = inscription.etudiant
    niveau      = inscription.classe.niveau_code

    # La source est la MAQUETTE du niveau, et non les lignes de notes déjà
    # créées. Une ligne de note n'existe que si le semestre a été ouvert au
    # calendrier : en début d'année le semestre pair ne l'est pas encore, et
    # l'attestation ne montrait alors que la moitié de l'année. L'étudiant est
    # pourtant inscrit aux deux — l'inscription pédagogique est entière et
    # automatique, sans dette ni crédit.
    #
    # La forme du contexte suit celle du socle (`elements_par_semestre`) parce
    # que le gabarit en est la reprise fidèle ; le niveau « module » n'existe
    # pas en prépa, les matières pendent directement du semestre.
    elements_par_semestre = []
    fiche_niveau = services.niveau_ref(niveau, institution)
    for code_semestre in services.codes_semestres_du_niveau(niveau, institution):
        matieres = list(
            Matiere.objects
            .filter(code_semestre=code_semestre, actif=True,
                    institution=institution)
            # Deux niveaux d'un même rang partagent les semestres sans partager
            # leur maquette : sans ce filtre, un MPI recevrait les matières de MP.
            # Les matières d'avant le référentiel n'ont pas de niveau : elles
            # restent visibles pour le cursus d'origine, qui est le leur.
            .filter(Q(niveau_ref=fiche_niveau) | Q(niveau_ref__isnull=True)
                    if fiche_niveau is not None else Q(niveau_ref__isnull=True))
            .order_by('ordre', 'code')
        )
        if not matieres:
            continue
        elements_par_semestre.append({
            'code':           code_semestre,
            'semestre_label': f'Semestre {code_semestre[1:]} — {code_semestre}',
            'elements': [{
                'code':        matiere.code,
                'intitule_fr': matiere.intitule,
                'coefficient': matiere.coefficient,
            } for matiere in matieres],
            'total_coeff': sum(matiere.coefficient for matiere in matieres),
        })

    feminin = (etudiant.genre or 'M').upper() == 'F'
    site = (getattr(institution, 'site_web', '') or '').strip()
    for prefixe in ('https://', 'http://', 'www.'):
        if site.startswith(prefixe):
            site = site[len(prefixe):]
    domaine = site.strip('/') or 'ipgei.mr'

    contexte.update({
        # Identité, accordée au genre comme sur l'attestation du socle.
        'civilite':    'Mme' if feminin else 'M',
        'genre_label': 'Féminin' if feminin else 'Masculin',
        'ne_e':        'Née' if feminin else 'Né',
        'inscrit_e':   'inscrite' if feminin else 'inscrit',
        'date_naissance': (etudiant.date_naissance.strftime('%d/%m/%Y')
                           if etudiant.date_naissance else '—'),

        'today':       date.today().strftime('%d/%m/%Y'),
        'ville':       getattr(institution, 'ville_fr', '') or 'Nouakchott',
        'niveau_label': services.libelle_annee_du_niveau(niveau, institution),
        'filiere_nom':  services.libelle_cursus(niveau, institution),
        'groupe':       services.libelle_classe(inscription.classe),
        'annee_univ_label': inscription.annee_universitaire,
        'email_univ':   f'{etudiant.matricule}@{domaine}',

        'elements_par_semestre': elements_par_semestre,
        'total_coef_annee': sum(s['total_coeff'] for s in elements_par_semestre),
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
