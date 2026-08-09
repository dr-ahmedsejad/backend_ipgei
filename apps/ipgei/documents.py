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

#: L'attestation CNIM tient ses marges ICI, et son gabarit n'en pose aucune.
#:
#: Les poser des deux côtés les additionnait — 12 mm ici, 22 mm dans le CSS —
#: ET faisait déborder le bloc `.page` large de 210 mm d'une zone imprimable de
#: 186 mm : wkhtmltopdf réduit alors la page entière, sans rien signaler. Le
#: document sortait rétréci, décalé, avec un blanc plus large à droite qu'à
#: gauche. Un seul endroit commande la marge, et c'est celui-ci.
OPTIONS_PDF_CNIM = {
    **OPTIONS_PDF,
    'margin-top':    '13mm',
    'margin-bottom': '13mm',
    'margin-left':   '14mm',
    'margin-right':  '14mm',
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
                   semestre: SemestreIPGEI = None, utilisateur=None,
                   signataire_id=None) -> DocumentOfficiel:
    """
    Enregistre le document au registre (numéro + token de vérification).

    Un document est réémis à chaque demande plutôt que réutilisé : chaque tirage
    a son propre numéro, ce qui permet de tracer qui a reçu quoi et quand.

    Le signataire est RECOPIÉ ici, pas pointé : la pièce garde le nom qu'elle
    porte, même si l'intéressé change de fonction ou quitte l'établissement.
    C'est aussi ce qui permet de savoir, des mois plus tard, qui a signé.
    """
    if type_document not in TYPES_IPGEI:
        raise ValidationError(f'Type de document IPGEI inconnu : {type_document}')

    institution = inscription.classe.institution
    signataire  = services.signataire_du_document(institution, signataire_id)
    return DocumentOfficiel.objects.create(
        institution=institution,
        etudiant=inscription.etudiant,
        type_document=type_document,
        numero_serie=_numero_serie(institution, type_document),
        annee_universitaire=inscription.annee_universitaire,
        genere_par=utilisateur if getattr(utilisateur, 'is_authenticated', False) else None,
        signataire_nom=signataire['nom'],
        signataire_titre=signataire['titre'],
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
        # La signature est bilingue : la ville aussi, sans quoi un côté mêlerait
        # un nom latin à une phrase arabe.
        'ville':        getattr(institution, 'ville_fr', '') or 'Nouakchott',
        'ville_ar':     getattr(institution, 'ville_ar', '') or 'نواكشوط',
        # Lu sur le document, pas sur le paramétrage courant : rééditer un
        # duplicata doit redonner la pièce telle qu'elle a été délivrée, avec
        # le nom de qui l'a signée ce jour-là.
        'signataire':   {
            'nom':   doc.signataire_nom,
            'titre': doc.signataire_titre or services.SIGNATAIRE_DEFAUT['titre'],
        },
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


#: Tutelles imprimées en tête de l'autorisation CNIM, telles qu'elles figurent
#: sur la pièce que l'institut délivre. Le socle ne porte qu'UN champ
#: `ministere_fr` ; il en faut trois, et bilingues. Elles vivent donc ici, avec
#: le document qui les porte — le jour où la hiérarchie change, c'est ce
#: document-là qu'on rééditera.
TUTELLES_CNIM = [
    {'ar': 'وزارة الدفاع الوطني وشؤون المتقاعدين وأولاد الشهداء',
     'fr': 'Ministère de la Défense, des Affaires des Retraités '
           'et des Enfants des Martyrs'},
    {'ar': 'وزارة التعليم العالي والبحث العلمي',
     'fr': "Ministère de l'Enseignement Supérieur et de la Recherche Scientifique"},
    {'ar': 'الأركان العامة للجيوش',
     'fr': 'Etat-Major Général des Armées'},
    {'ar': 'مجمع بوليتكنيك',
     'fr': 'Groupe Polytechnique'},
]


def _session_concours(annee_universitaire: str) -> str:
    """
    Session du concours : l'année de FIN du libellé — « 2024-2025 » → « 2025 ».

    Le CNIM se tient au terme de l'année universitaire, et c'est ce millésime
    que porte l'attestation. Imprimer « 2024-2025 » ferait une session qui
    n'existe pas.
    """
    parts = str(annee_universitaire or '').split('-')
    return parts[-1] if parts and parts[-1].isdigit() else str(annee_universitaire or '')


def _contexte_cnim(doc, inscription, ligne: LigneDeliberation, base_url,
                   *, signataire_impose: bool = False) -> dict:
    """
    Contexte de l'autorisation CNIM.

    NI moyenne, NI rang, NI seuil : la pièce atteste une autorisation, pas un
    résultat. Y inscrire une note exposerait le dossier de l'étudiant à qui lit
    son autorisation de concours — un jury, un secrétariat de concours.

    Elle est signée par le DIRECTEUR : c'est lui qui autorise à concourir. Le
    directeur de l'institution s'impose donc d'office… SAUF si l'agent a
    désigné quelqu'un — le directeur absent, un adjoint signe par ordre. Ce
    choix-là prime, sans quoi le sélecteur serait sans effet dès que le
    directeur est renseigné : on choisirait un nom, un autre s'imprimerait.

    À défaut de l'un et de l'autre, le signataire par défaut de l'institution.
    """
    deliberation = ligne.deliberation
    contexte = _contexte_commun(doc, inscription, base_url)
    institution = doc.institution

    directeur_nom   = (getattr(institution, 'directeur_nom_fr', '') or '').strip()
    directeur_titre = (getattr(institution, 'directeur_titre_fr', '') or '').strip()

    if signataire_impose:
        # Recopié sur le document à sa création : c'est la main qui signe, avec
        # SON titre. Le mot « Directeur » ne se recopie pas sur un adjoint.
        signataire = dict(contexte['signataire'])
        signataire['est_directeur'] = False
    else:
        # Le titre vient de l'institution : « Directeur », « Directeur général »
        # ou ce que porte l'établissement. L'écrire en dur ici le ferait mentir
        # le jour où l'institut change d'intitulé.
        signataire = {
            'nom':   directeur_nom or contexte['signataire']['nom'],
            'titre': directeur_titre or 'Directeur',
            # Le bandeau arabe dit « المدير » — le directeur, et rien d'autre.
            # On ne l'affiche donc que si c'est bien lui : traduire un titre
            # quelconque en arabe n'est pas à la portée d'un gabarit, et laisser
            # « المدير » au-dessus d'un adjoint ferait dire au document qu'il est
            # signé par quelqu'un qui ne l'a pas signé.
            'est_directeur': True,
        }

    contexte.update({
        'titre':        "ATTESTATION D'AUTORISATION DE PARTICIPATION AU CNIM",
        'deliberation': deliberation,
        'ligne':        ligne,
        'date_jury':    deliberation.date_validation or deliberation.date_creation,
        'tutelles':     TUTELLES_CNIM,
        'session':      _session_concours(inscription.annee_universitaire),
        'signataire':   signataire,
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
    if type_document == TYPE_CNIM:
        return render_pdf_bytes(TEMPLATES[type_document], contexte, OPTIONS_PDF_CNIM)
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


def generer_releve_semestre(inscription, semestre, *, utilisateur=None, base_url='',
                            signataire_id=None):
    # `DocumentOfficiel.semestre` pointe sur le `parametres.Semestre` du socle
    # LMD : on le laisse vide, le semestre IPGEI figurant dans le PDF et dans le
    # contexte de rendu.
    doc = creer_document(inscription, TYPE_RELEVE_SEMESTRE, semestre=semestre,
                         utilisateur=utilisateur, signataire_id=signataire_id)
    pdf = generer_pdf(doc, _contexte_releve_semestre(doc, inscription, semestre, base_url))
    return doc, pdf


def generer_releve_annuel(inscription, *, utilisateur=None, base_url='', signataire_id=None):
    doc = creer_document(inscription, TYPE_RELEVE_ANNUEL, utilisateur=utilisateur,
                         signataire_id=signataire_id)
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


#: Ce qu'il faut faire quand la décision du jury n'ouvre pas le concours.
#: Un refus n'aide que s'il dit la suite : sans cela, l'agent réessaie, croit à
#: une panne, ou cherche le document dans un autre écran.
SUITES_SANS_CNIM = {
    LigneDeliberation.DECISION_REDOUBLANT:
        "Un redoublant se réinscrit en deuxième année ; il concourra au terme "
        "de l'année suivante.",
    LigneDeliberation.DECISION_EXCLU:
        "Aucune suite ne lui est ouverte dans le cursus préparatoire.",
    LigneDeliberation.DECISION_ADMIS:
        "Cette décision fait passer en deuxième année : le concours vient au "
        "terme de celle-ci, pas avant.",
    LigneDeliberation.DECISION_REORIENTE:
        "Un étudiant réorienté quitte le cursus préparatoire.",
}


def generer_attestation_cnim(deliberation, inscription, *, utilisateur=None, base_url='',
                             signataire_id=None):
    """
    L'attestation CNIM ne s'émet que pour un étudiant effectivement autorisé à
    concourir : c'est le document qui ouvre l'accès au concours.

    Le refus NOMME l'étudiant et dit la suite. « Décision actuelle :
    Redoublant » laissait l'agent devant un écran de trente-sept lignes sans
    savoir laquelle avait déclenché le refus, ni s'il fallait corriger le jury
    ou changer de document.
    """
    ligne = _ligne_validee(deliberation, inscription)
    if ligne.decision != LigneDeliberation.DECISION_CNIM:
        etudiant = inscription.etudiant
        qui = f'{etudiant.nom_display} ({etudiant.matricule})'
        if not ligne.decision:
            raise ValidationError(
                f"{qui} n'a pas encore de décision de jury : l'autorisation de "
                'concourir ne peut être attestée avant que le jury ne se soit '
                'prononcé.'
            )
        morceaux = [
            f"{qui} n'est pas autorisé à concourir : le jury a prononcé "
            f'« {ligne.get_decision_display()} ».',
            SUITES_SANS_CNIM.get(ligne.decision, ''),
            'Pour attester cette décision-là, éditez la décision de délibération.',
        ]
        raise ValidationError(' '.join(m for m in morceaux if m))
    doc = creer_document(inscription, TYPE_CNIM, utilisateur=utilisateur,
                         signataire_id=signataire_id)
    pdf = generer_pdf(doc, _contexte_cnim(doc, inscription, ligne, base_url,
                                          signataire_impose=bool(signataire_id)))
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


def generer_attestation_inscription(inscription, *, utilisateur=None, base_url='',
                                    signataire_id=None):
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
    doc = creer_document(inscription, TYPE_ATTESTATION, utilisateur=utilisateur,
                         signataire_id=signataire_id)
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


# ═════════════════════════════════════════════════════════════════════════════
# Édition par groupe
# ═════════════════════════════════════════════════════════════════════════════
#: Ce qui s'édite pour toute une classe d'un coup. Le reçu de paiement en est
#: exclu : il n'existe que pour une inscription réglée, et l'éditer en lot
#: produirait autant de refus que d'impayés.
TYPES_EN_LOT = {
    TYPE_RELEVE_SEMESTRE: 'Relevé de semestre',
    TYPE_RELEVE_ANNUEL:   'Relevé annuel',
    TYPE_ATTESTATION:     "Attestation d'inscription",
    TYPE_CNIM:            'Attestation CNIM',
}


def generer_lot(inscriptions, type_document: str, *, semestre=None,
                deliberation=None, utilisateur=None, base_url: str = '',
                signataire_id=None) -> tuple:
    """
    Édite un document par étudiant, et les FUSIONNE en un seul PDF.

    Un fichier par élève obligerait à ouvrir quarante pièces jointes puis à les
    imprimer une par une ; un PDF unique s'imprime d'un geste et se classe
    ensuite. C'est la façon de faire du socle, reprise telle quelle.

    Chaque élève garde SON document officiel — son numéro de série, son
    empreinte, son QR de vérification. La fusion ne concerne que le papier :
    au registre, ce sont quarante pièces distinctes.

    Un élève en échec — dossier incomplet, inscription close — est REPORTÉ et
    n'interrompt pas le lot : sur une promotion entière, un seul cas bloquant
    empêcherait d'imprimer les trente-neuf autres.

    `signataire_id` vaut pour TOUTE la fournée : quarante relevés sortis d'un
    même tirage portent une seule signature manuscrite au bas de chacun, donc un
    seul signataire. Le laisser vide reprend celui par défaut.

    Renvoie `(pdf, nb_edites, nb_attendus, erreurs)`.
    """
    from io import BytesIO

    from pypdf import PdfWriter

    if type_document not in TYPES_EN_LOT:
        raise ValidationError(
            f"« {type_document} » ne s'édite pas par groupe.")
    if type_document == TYPE_RELEVE_SEMESTRE and semestre is None:
        raise ValidationError({'semestre': 'Un relevé de semestre vise un semestre.'})
    if type_document == TYPE_CNIM and deliberation is None:
        raise ValidationError({'deliberation':
                               'Une autorisation de concourir découle d\'un jury.'})

    # Le signataire est vérifié UNE FOIS, avant la boucle : mal désigné, il
    # ferait échouer chaque élève pour la même raison, et le bilan afficherait
    # quarante lignes disant la même chose au lieu d'un refus net.
    if inscriptions:
        services.signataire_du_document(
            inscriptions[0].classe.institution, signataire_id)

    editeurs = {
        TYPE_RELEVE_SEMESTRE: lambda i: generer_releve_semestre(
            i, semestre, utilisateur=utilisateur, base_url=base_url,
            signataire_id=signataire_id),
        TYPE_RELEVE_ANNUEL:   lambda i: generer_releve_annuel(
            i, utilisateur=utilisateur, base_url=base_url,
            signataire_id=signataire_id),
        TYPE_ATTESTATION:     lambda i: generer_attestation_inscription(
            i, utilisateur=utilisateur, base_url=base_url,
            signataire_id=signataire_id),
        TYPE_CNIM:            lambda i: generer_attestation_cnim(
            deliberation, i, utilisateur=utilisateur, base_url=base_url,
            signataire_id=signataire_id),
    }
    editer = editeurs[type_document]

    fusion = PdfWriter()
    edites, erreurs = 0, []
    for inscription in inscriptions:
        try:
            _, pdf = editer(inscription)
            fusion.append(BytesIO(pdf))
            edites += 1
        except Exception as exc:                              # noqa: BLE001
            motif = (getattr(exc, 'messages', None) or [str(exc)])[0]
            erreurs.append({
                'etudiant': inscription.etudiant.matricule,
                'nom':      inscription.etudiant.nom_display,
                'motif':    motif[:200],
            })
            logger.exception('Édition en lot refusée pour %s', inscription)

    total = len(inscriptions) if hasattr(inscriptions, '__len__') else edites + len(erreurs)
    if edites == 0:
        raise ValidationError(
            'Aucun document produit. ' +
            ' · '.join(f"{e['etudiant']} : {e['motif']}" for e in erreurs[:3])
        )

    sortie = BytesIO()
    fusion.write(sortie)
    fusion.close()
    return sortie.getvalue(), edites, total, erreurs
