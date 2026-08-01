"""
Service de génération de documents officiels.

La numérotation est thread-safe via NumeroSerieConfig.generer_prochain().
La génération du PDF utilise pdfkit/wkhtmltopdf (HTML → PDF) avec templates Django bilingues FR/AR.
Les QR codes pointent vers l'URL de vérification du document.
"""
import base64
import hashlib
import io
import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP


def _round_half_up(val, decimals=2):
    """Arrondi mathematique classique (15.125 -> 15.13). Compatible Decimal et float."""
    if val is None:
        return None
    if not isinstance(val, Decimal):
        val = Decimal(str(val))
    quant = Decimal('1').scaleb(-decimals)  # 0.01 pour decimals=2
    return val.quantize(quant, rounding=ROUND_HALF_UP)


def _round_half_up_float(val, decimals=2):
    """Version float du round half-up (15.125 -> 15.13). Pour calculs en float (notes_index)."""
    if val is None:
        return None
    factor = 10 ** decimals
    # math.floor(x * factor + 0.5) / factor donne round-half-up pour positifs
    # On utilise Decimal pour eviter les erreurs de float (ex 0.1 + 0.2 = 0.30000000000000004)
    return float(Decimal(str(val)).quantize(
        Decimal('1').scaleb(-decimals), rounding=ROUND_HALF_UP,
    ))

from django.core.files.base import ContentFile
from django.template.loader import render_to_string
from django.utils import timezone

from .models import DocumentOfficiel, NumeroSerieConfig

logger = logging.getLogger('siga')


def enregistrer_pdf_document(doc, pdf_bytes):
    """Écrit `pdf_bytes` dans doc.fichier_pdf SANS créer d'orphelin.

    Django ajoute un suffixe (_xxxxxxx) si un fichier du même nom existe déjà,
    laissant l'ancien orphelin sur le disque. On supprime donc avant d'écrire :
      1. le fichier actuellement référencé (s'il y en a un) ;
      2. tout fichier résiduel au nom canonique « {numero_serie}.pdf ».
    Résultat : nom canonique stable, aucun fichier orphelin accumulé.
    """
    filename = f'{doc.numero_serie}.pdf'
    storage  = doc.fichier_pdf.storage
    if doc.fichier_pdf and doc.fichier_pdf.name:
        try:
            doc.fichier_pdf.delete(save=False)
        except Exception:
            logger.warning('Suppression ancien PDF doc#%s échouée', doc.pk, exc_info=True)
    target = f'documents/officiels/{filename}'
    if storage.exists(target):
        storage.delete(target)
    doc.fichier_pdf.save(filename, ContentFile(pdf_bytes), save=True)

# Préfixes par défaut — cohérents avec les seeds de data migration
_PREFIXE_DEFAUT = {
    'attestation_inscription': 'AI',
    'releve_semestre':         'RS',
    'releve_complet':          'RC',
    'attestation_reussite':    'AR',
    'attestation_diplome':     'AD',
    'diplome':                 'DI',
}

# Équivalents arabes des mentions FR (registre des diplômes) — pour l'attestation
# de diplôme bilingue. Repli sur '' si la mention n'est pas reconnue.
# Grille de mention DIPLÔME : un diplômé a TOUJOURS la moyenne (≥ 10) → 5 niveaux,
# PAS d'« Insuffisant ». FR → équivalent arabe (validés avec le client).
MENTION_DIPLOME_AR = {
    'Excellent':  'ممتاز',
    'Très Bien':  'جيد جدا',
    'Bien':       'جيد',
    'Assez Bien': 'مستحسن',
    'Passable':   'مقبول',
}


def _mention_diplome(moyenne) -> str:
    """Mention d'un DIPLÔME selon la moyenne générale (grille à 5 niveaux) :
    ≥ 18 → Excellent · ≥ 16 → Très Bien · ≥ 14 → Bien · ≥ 12 → Assez Bien ·
    ≥ 10 → Passable. Pas d'« Insuffisant » : on ne délivre pas de diplôme sous 10."""
    if moyenne is None:
        return ''
    if not isinstance(moyenne, Decimal):
        moyenne = Decimal(str(moyenne))
    if moyenne >= 18:
        return 'Excellent'
    if moyenne >= 16:
        return 'Très Bien'
    if moyenne >= 14:
        return 'Bien'
    if moyenne >= 12:
        return 'Assez Bien'
    return 'Passable'


def _libelle_diplome(filiere) -> str:
    """
    Libellé du diplôme (vérification QR) — MÊME formulation que le corps de
    l'attestation PDF, pour rester cohérent des deux côtés :

        « Licence professionnelle en statistique, filière : <intitulé filière> »

    « en statistique » = domaine ISS, aligné sur le template attestation
    (attestation_diplome.html) ; à généraliser si une filière d'un autre domaine
    est ajoutée. La filière affichée est celle de FIN DE CYCLE (ex. SDID), pas le
    tronc commun parent (LPSTAT).
    """
    if not filiere:
        return ''
    type_dip = (getattr(filiere, 'type_diplome', 'LP') or 'LP')
    option   = (getattr(filiere, 'intitule_fr', '') or '').strip()
    base = ("Diplôme national d'ingénieur" if type_dip == 'ING'
            else 'Licence professionnelle en statistique')
    return f'{base}, filière : {option}' if option else base


# Préfixe du numéro de diplôme selon le type de filière (désignations officielles) :
# DLP = Diplôme de Licence Professionnelle (Art. 1 Arrêté 562) ; DNI = Diplôme
# National d'Ingénieur (Décret 2018-070).
DIPLOME_PREFIXE = {'LP': 'DLP', 'ING': 'DNI'}


def generer_numero_diplome(institution, filiere, annee_universitaire) -> str:
    """
    Numéro OFFICIEL de diplôme.

    - Si l'établissement a un CODE (``Institution.code_etablissement``) : le code
      est ENCODÉ dans le dernier segment — ``<PREFIXE>-<ANNÉE>-<CODE><SÉQ>``
      (ex. code '05' → ``DLP-2026-0501``, ``…-0502`` ; code '02' → ``DNI-2026-0201``).
      La séquence (2 chiffres minimum) démarre à 1 PAR (institution, année).
    - Sinon (code vide) : repli lisible ``<PREFIXE>-<ACRONYME>-<ANNÉE>-<SÉQ>`` avec
      l'offset ``Institution.diplome_sequence_debut`` (ex. ``DLP-ISS-2026-0500``).

    PREFIXE : DLP (licence pro) / DNI (ingénieur), selon ``filiere.type_diplome``.
    Identifiant ADMINISTRATIF : l'anti-falsification reste le QR (token aléatoire)
    + la signature PDF, PAS ce numéro. Registre append-only.
    """
    from .models import RegistreDiplome

    type_dip  = (getattr(filiere, 'type_diplome', 'LP') or 'LP')
    prefixe   = DIPLOME_PREFIXE.get(type_dip, 'DLP')
    annee_fin = str(annee_universitaire or '').split('-')[-1]
    code      = (getattr(institution, 'code_etablissement', '') or '').strip()

    if code:
        # Code établissement encodé en tête du dernier segment : <CODE><SÉQ>.
        base, largeur, debut = f'{prefixe}-{annee_fin}-{code}', 2, 1
    else:
        # Repli : acronyme lisible + offset (ne part pas de 1).
        acronyme = (getattr(institution, 'acronyme', '') or 'ISS').upper()
        base     = f'{prefixe}-{acronyme}-{annee_fin}-'
        largeur  = 4
        debut    = getattr(institution, 'diplome_sequence_debut', 1) or 1

    max_seq = debut - 1
    for num in (RegistreDiplome.objects
                .filter(institution=institution, numero_diplome__startswith=base)
                .values_list('numero_diplome', flat=True)):
        try:
            max_seq = max(max_seq, int(num[len(base):]))
        except (ValueError, IndexError):
            continue
    return f'{base}{max_seq + 1:0{largeur}d}'


def _moyenne_generale_cycle(etudiant, annee_univ):
    """
    Moyenne générale du DIPLÔME = moyenne ARITHMÉTIQUE des moyennes des 6
    semestres consolidés (demande directeur) :

        Moyenne générale = (Moy S1 + Moy S2 + … + Moy S6) / 6

    Chaque moyenne de semestre vient du MÊME moteur que le relevé
    (calculer_resultat_semestre_consolide : max SN/SR + compensation + report).
    Le diviseur est le NOMBRE de semestres ayant une moyenne (6 pour un diplômé).
    """
    from apps.inscriptions.models import InscriptionPedagogique

    sems = {}
    for ip in (InscriptionPedagogique.objects
               .filter(inscription_admin__etudiant=etudiant)
               .select_related('semestre')):
        if ip.semestre:
            sems[ip.semestre.code_semestre] = ip.semestre

    total = Decimal('0')
    n = 0
    for sem in sems.values():
        res = calculer_resultat_semestre_consolide(etudiant, sem, annee_univ)
        moy = res.get('moyenne_semestre')
        if moy is None:
            continue
        total += Decimal(str(moy))
        n += 1
    if n == 0:
        return Decimal('0')
    return _round_half_up(total / n, 2)


def attribuer_diplomes_pv(pv, date_delivrance=None) -> dict:
    """
    Attribue les diplômes (crée les RegistreDiplome) des diplômés d'un PV annuel
    de FIN DE CYCLE — Art. 26 Arrêté 562 / Art. 30 Décret 2018-070 (PV de jury de
    délivrance). Alimente le registre (jusque-là jamais peuplé).

    - Périmètre : PV annuel, niveau == filiere.niveau_fin, hors tronc commun
      (filière sans filles) — même règle que le blocage / est_annee_diplome.
    - Diplômables : lignes décision='admis' (les redoublements/exclusions sont
      déjà écartés par le blocage). Éligibilité RE-VÉRIFIÉE (180 crédits + note
      finale >= 12) via le moteur du relevé — défense en profondeur.
    - Idempotent : saute l'étudiant qui a déjà un diplôme pour cette année.
    - Numéro : generer_numero_diplome (DLP/DNI + offset). Registre append-only.

    Retourne {'crees','deja','non_eligibles','ignores'}.
    """
    from django.db import transaction
    from apps.evaluations.services.deliberation_annuelle import (
        get_deliberation_annuelle_service, CREDITS_DIPLOME, SEUIL_NOTE_DIPLOME,
    )
    from .models import RegistreDiplome

    stats = {'crees': 0, 'deja': 0, 'non_eligibles': 0, 'ignores': 0}

    fil = pv.filiere
    if not fil or pv.type_pv != 'annuel':
        return stats
    niveau_fin = fil.niveau_fin or 3
    if pv.niveau != niveau_fin or fil.filieres_filles.exists():
        return stats  # pas une année de diplôme (ou tronc commun)

    annee    = pv.annee_univ.annee if pv.annee_univ else ''
    date_del = date_delivrance or getattr(pv, 'date_deliberation', None) or timezone.now().date()
    svc      = get_deliberation_annuelle_service(pv)

    with transaction.atomic():
        for ligne in pv.lignes.select_related(
                'inscription_admin__etudiant', 'inscription_admin__institution'):
            if ligne.decision != 'admis':
                stats['ignores'] += 1
                continue
            ia   = ligne.inscription_admin
            etu  = ia.etudiant
            inst = ia.institution or pv.institution

            if RegistreDiplome.objects.filter(
                    etudiant=etu, annee_universitaire=annee).exists():
                stats['deja'] += 1
                continue

            # Éligibilité (moteur relevé) — mêmes helpers que le blocage diplôme.
            credits = svc._credits_capitalises_diplome(ia)
            note_s6 = svc._moyenne_semestre_final(ia)
            if (credits < CREDITS_DIPLOME
                    or note_s6 is None
                    or Decimal(str(note_s6)) < SEUIL_NOTE_DIPLOME):
                stats['non_eligibles'] += 1
                continue

            moyenne = _moyenne_generale_cycle(etu, ia.annee_univ)
            RegistreDiplome.objects.create(
                institution=inst, etudiant=etu, filiere=fil,
                numero_diplome=generer_numero_diplome(inst, fil, annee),
                mention=_mention_diplome(moyenne),
                moyenne_generale=moyenne,
                date_delivrance=date_del,
                annee_universitaire=annee,
            )
            stats['crees'] += 1

    return stats

NIVEAU_LABELS = {
    1: '1ère Année Licence (L1)', 2: '2ème Année Licence (L2)', 3: '3ème Année Licence (L3)',
    4: '1ère Année Master (M1)', 5: '2ème Année Master (M2)',
    6: '1ère Année Doctorat',    7: '2ème Année Doctorat',    8: '3ème Année Doctorat',
}

NIVEAU_AR_LABELS = {
    1: 'السنة الأولى ليسانس',  2: 'السنة الثانية ليسانس', 3: 'السنة الثالثة ليسانس',
    4: 'السنة الأولى ماستر',   5: 'السنة الثانية ماستر',
    6: 'السنة الأولى دكتوراه', 7: 'السنة الثانية دكتوراه', 8: 'السنة الثالثة دكتوراه',
}

SEMESTRE_FR_LABELS = {
    'S1': 'Premier Semestre',   'S2': 'Deuxième Semestre',
    'S3': 'Troisième Semestre', 'S4': 'Quatrième Semestre',
    'S5': 'Cinquième Semestre', 'S6': 'Sixième Semestre',
}
SEMESTRE_AR_LABELS = {
    'S1': 'السداسي الأول',   'S2': 'السداسي الثاني',
    'S3': 'السداسي الثالث', 'S4': 'السداسي الرابع',
    'S5': 'السداسي الخامس', 'S6': 'السداسي السادس',
}


def _format_date_naissance(etudiant) -> str:
    """Formate la date de naissance en DD/MM/YYYY (les filtres Django ne fonctionnent pas via wkhtmltopdf)."""
    try:
        dn = getattr(etudiant, 'date_naissance', None)
        if dn is None:
            return ''
        if hasattr(dn, 'strftime'):
            return dn.strftime('%d/%m/%Y')
        from datetime import datetime
        dn_str = str(dn).strip()[:10]
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
            try:
                return datetime.strptime(dn_str, fmt).strftime('%d/%m/%Y')
            except ValueError:
                continue
        return dn_str
    except Exception as e:
        logger.warning('Erreur formatage date_naissance: %s', e)
        return str(getattr(etudiant, 'date_naissance', ''))


def _get_photo_b64(etudiant) -> str | None:
    """Retourne la photo de l'étudiant encodée en data URI base64, ou None."""
    try:
        if etudiant.photo:
            import os
            photo_path = etudiant.photo.path
            if os.path.exists(photo_path):
                ext  = photo_path.rsplit('.', 1)[-1].lower()
                mime = 'image/jpeg' if ext in ('jpg', 'jpeg') else f'image/{ext}'
                with open(photo_path, 'rb') as f:
                    return f'data:{mime};base64,' + base64.b64encode(f.read()).decode()
    except Exception:
        pass
    return None


def _get_logo_b64(institution) -> str | None:
    """Retourne le logo de l'institution encodé en data URI base64, ou None."""
    try:
        if institution and institution.logo:
            import os
            logo_path = institution.logo.path
            if os.path.exists(logo_path):
                ext  = logo_path.rsplit('.', 1)[-1].lower()
                mime = 'image/png' if ext == 'png' else ('image/jpeg' if ext in ('jpg', 'jpeg') else f'image/{ext}')
                with open(logo_path, 'rb') as f:
                    return f'data:{mime};base64,' + base64.b64encode(f.read()).decode()
    except Exception:
        pass
    return None


def _imagefield_b64(filefield) -> str | None:
    """Encode un ImageField (logo, sceau, signature) en data URI base64, ou None.
    Générique : wkhtmltopdf rend de manière fiable les images inline base64."""
    try:
        if filefield:
            import os
            path = filefield.path
            if os.path.exists(path):
                ext  = path.rsplit('.', 1)[-1].lower()
                mime = 'image/png' if ext == 'png' else ('image/jpeg' if ext in ('jpg', 'jpeg') else f'image/{ext}')
                with open(path, 'rb') as f:
                    return f'data:{mime};base64,' + base64.b64encode(f.read()).decode()
    except Exception:
        pass
    return None


def _next_numero_serie(type_document: str, institution_id: int | None) -> str:
    """
    Génère un numéro de série séquentiel thread-safe.
    Préfère NumeroSerieConfig si une config institution existe.
    Repli sur un compteur simple sans institution (compatibilité).
    """
    if institution_id:
        config = NumeroSerieConfig.objects.filter(
            institution_id=institution_id,
            type_document=type_document,
        ).first()
        if config:
            return config.generer_prochain()

    # Repli : prochain numéro = MAX des suffixes existants + 1 pour (préfixe, année).
    # NE PAS utiliser COUNT+1 : après une suppression (ex. purge de doublons) le compte
    # baisse alors que les numéros déjà émis subsistent → COUNT+1 retombe sur un numéro
    # existant → IntegrityError 1062. MAX+1 est robuste aux trous.
    prefix = _PREFIXE_DEFAUT.get(type_document, 'DOC')
    year   = timezone.now().year
    base   = f'{prefix}-{year}-'
    max_suffix = 0
    for ns in DocumentOfficiel.objects.filter(
        numero_serie__startswith=base,
    ).values_list('numero_serie', flat=True):
        try:
            max_suffix = max(max_suffix, int(ns[len(base):]))
        except (ValueError, TypeError):
            pass
    logger.warning(
        'NumeroSerieConfig introuvable pour type=%s institution=%s — repli non thread-safe',
        type_document, institution_id,
    )
    return f'{prefix}-{year}-{max_suffix + 1:05d}'


def _texte_qr_diplome(doc, etudiant) -> str:
    """
    Texte ENCODÉ dans le QR d'une attestation de diplôme — contenu autonome, propre
    au diplôme (au lieu de l'URL de vérification, qui sera rebranchée plus tard).
    Même formulation que la vérification en ligne (groupe, établissement, identité,
    libellé, NNI, matricule, date d'obtention).
    """
    from .models import RegistreDiplome
    inst   = getattr(doc, 'institution', None) or _get_institution(etudiant)
    groupe = (getattr(inst, 'groupe_fr', '') or '').strip()
    etab   = (getattr(inst, 'nom_complet_fr', '') or getattr(inst, 'nom_fr', '')
              or getattr(inst, 'nom', '') or '').strip()

    qs = RegistreDiplome.objects.filter(etudiant=etudiant)
    if getattr(doc, 'annee_universitaire', ''):
        qs = qs.filter(annee_universitaire=doc.annee_universitaire)
    reg = qs.select_related('filiere', 'filiere__filiere_parent').order_by('-date_delivrance').first()
    fil = getattr(reg, 'filiere', None) or getattr(etudiant, 'filiere', None)

    prenom = (getattr(etudiant, 'prenom_fr', '') or '').strip()
    nom    = (getattr(etudiant, 'nom_fr', '') or etudiant.nom or '').strip()
    nom_complet = f'{prenom} {nom}'.strip() or etudiant.nom
    d = getattr(reg, 'date_delivrance', None)

    lignes = []
    if groupe:
        lignes.append(groupe)
    if etab:
        lignes.append(etab)
    lignes += [
        'Attestation de diplôme',
        f'Nom complet: {nom_complet}',
        f'Diplôme : {_libelle_diplome(fil)}',
        f'NNI : {(getattr(etudiant, "cni", "") or "").strip()}',
        f'Matricule : {(getattr(etudiant, "matricule", "") or "").strip()}',
        f"Date d'obtention : {d.strftime('%d/%m/%Y') if d else ''}",
    ]
    return '\n'.join(lignes)


def _texte_qr_releve(doc, etudiant) -> str:
    """
    Texte encodé dans le QR d'un RELEVÉ DE NOTES (au lieu de l'URL de vérification,
    rebranchée plus tard comme pour l'attestation de diplôme). Contenu autonome :
    groupe, établissement, « RELEVÉ DES NOTES DU <semestre> », identité, programme,
    NNI, matricule. Semestre + filière tirés de l'inscription pédagogique du relevé.
    """
    from apps.parametres.models import Semestre
    from apps.inscriptions.models import InscriptionPedagogique

    inst   = getattr(doc, 'institution', None) or _get_institution(etudiant)
    groupe = (getattr(inst, 'groupe_fr', '') or '').strip()
    etab   = (getattr(inst, 'nom_complet_fr', '') or getattr(inst, 'nom_fr', '')
              or getattr(inst, 'nom', '') or '').strip()

    sem = (Semestre.objects.filter(id=doc.semestre_id).first()
           if getattr(doc, 'semestre_id', None) else None)
    sem_label = SEMESTRE_FR_LABELS.get(getattr(sem, 'code_semestre', ''), '') if sem else ''
    titre = f'RELEVÉ DES NOTES DU {sem_label.upper()}' if sem_label else 'RELEVÉ DES NOTES'

    # Filière portant ce semestre (repli : filière courante de l'étudiant).
    filiere = None
    if sem:
        ip = (InscriptionPedagogique.objects
              .filter(inscription_admin__etudiant=etudiant, semestre=sem)
              .select_related('inscription_admin__filiere',
                              'inscription_admin__filiere__filiere_parent')
              .order_by('-inscription_admin__annee_univ__annee').first())
        filiere = getattr(getattr(ip, 'inscription_admin', None), 'filiere', None)
    filiere = filiere or getattr(etudiant, 'filiere', None)

    prenom = (getattr(etudiant, 'prenom_fr', '') or '').strip()
    nom    = (getattr(etudiant, 'nom_fr', '') or etudiant.nom or '').strip()
    nom_complet = f'{prenom} {nom}'.strip() or etudiant.nom

    lignes = []
    if groupe:
        lignes.append(groupe)
    if etab:
        lignes.append(etab)
    lignes += [
        titre,
        f'Nom complet: {nom_complet}',
        f'Filière : {(getattr(filiere, "intitule_fr", "") or "").strip()}',
        f'NNI : {(getattr(etudiant, "cni", "") or "").strip()}',
        f'Matricule : {(getattr(etudiant, "matricule", "") or "").strip()}',
    ]
    return '\n'.join(lignes)


def _get_qr_base64(token: str, base_url: str = '', etudiant=None, doc=None, filiere_nom: str = '', niveau_label: str = '') -> str | None:
    """Génère un QR code en SVG inline (vectoriel — rendu fiable dans wkhtmltopdf)."""
    try:
        import qrcode  # noqa
        import qrcode.image.svg
    except ImportError:
        logger.warning('Module qrcode non disponible — installez : pip install qrcode[pil]')
        return None

    try:
        verify_url = f'{base_url}/verifier/{token}' if base_url else f'/verifier/{token}'

        # Le QR encode l'URL de vérification : scanné par un téléphone, il ouvre la
        # page publique /verifier/{token} qui affiche l'IDENTITÉ + la PHOTO de
        # l'étudiant (comparaison visuelle anti-fraude). Vaut pour TOUS les
        # documents — attestation d'inscription, relevé, attestation de diplôme
        # (demande directeur : vérification photo rebranchée). base_url doit être
        # absolu (http(s)://<domaine>) pour être scannable à distance.
        qr_content = verify_url
        logger.debug('QR content (%d chars): %s', len(qr_content), qr_content[:80])

        # Génération SVG inline — pas de fichier externe, pas de data URI
        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(qr_content, image_factory=factory, box_size=6, border=2)
        svg_bytes = img.to_string()
        b64 = base64.b64encode(svg_bytes).decode('ascii')
        logger.info('QR code SVG généré (%d chars)', len(svg_bytes))
        return f'data:image/svg+xml;base64,{b64}'

    except Exception as e:
        logger.exception('Erreur génération QR code: %s', e)
        return None


def _get_institution(etudiant):
    """
    Récupère l'institution rattachée à un étudiant.
    Priorité : filiere.institution → departement.institution → institution principale (active).
    """
    try:
        from apps.parametres.models import Institution
        filiere = getattr(etudiant, 'filiere', None)
        if filiere and getattr(filiere, 'institution_id', None):
            return filiere.institution
        dept = getattr(etudiant, 'departement', None)
        if dept and getattr(dept, 'institution_id', None):
            return dept.institution
        # Fallback : institution principale (active)
        return Institution.objects.filter(est_principale=True).first() or Institution.objects.first()
    except Exception:
        return None


def _mark_sockets_non_inheritable() -> None:
    """
    Windows uniquement — marque tous les sockets Python du processus courant
    comme non-héritables AVANT de lancer wkhtmltopdf.

    Sur Windows, quand un sous-processus hérite des HANDLEs du parent et se
    termine, il ferme ses copies de ces HANDLEs.  Si le socket d'écoute ou de
    connexion Django est hérité, wkhtmltopdf le ferme à sa sortie, ce qui
    provoque un RST TCP → TypeError: Failed to fetch côté navigateur.

    Python 3.4+ crée les sockets avec WSA_FLAG_NO_HANDLE_INHERIT, mais le
    socket accepté par wsgiref peut garder l'indicateur héritable dans certaines
    versions.  On force le passage à non-héritable pour tous les sockets actifs.
    """
    import sys
    if sys.platform != 'win32':
        return
    try:
        import gc
        import socket as _sock
        import ctypes
        k32 = ctypes.windll.kernel32
        HANDLE_FLAG_INHERIT = 0x00000001
        for obj in gc.get_objects():
            if isinstance(obj, _sock.socket):
                try:
                    h = obj.fileno()
                    if h and h != -1:
                        k32.SetHandleInformation(h, HANDLE_FLAG_INHERIT, 0)
                except Exception:
                    pass
    except Exception:
        pass


# Footers NATIFS wkhtmltopdf : template -> (template_footer, margin-bottom). Permet de
# COLLER une ligne au bas de la page (le corps 240mm est réduit à une échelle imprévisible
# et sa hauteur ignorée → ni spacer CSS ni position:fixed ne fonctionnent). Le footer est
# rendu séparément AVEC le contexte (contenu dynamique comme l'email) dans un fichier temp.
_FOOTER_MAP = {
    'documents/releve_notes.html':            ('documents/_releve_footer.html',      '8mm'),
    'documents/attestation_inscription.html': ('documents/_attestation_footer.html', '8mm'),
}


def _render_pdf(template_name: str, context: dict) -> bytes:
    """Render Django template → wkhtmltopdf PDF bytes via le renderer partagé."""
    # Sur Windows, empêche wkhtmltopdf d'hériter des sockets Django.
    _mark_sockets_non_inheritable()
    from core.pdf_renderer import render_pdf_bytes
    import os
    options = {
        'encoding': 'UTF-8',
        'enable-local-file-access': None,
        # Ignore network errors (Google Fonts, etc.) — continue generation
        'load-error-handling': 'ignore',
        'load-media-error-handling': 'ignore',
        'dpi': 150,
        # Le template gère ses propres marges via @page { margin:0 }
        # et .page { padding: 8mm 10mm 15mm 10mm } ; 0 pour éviter le double-marge.
        'margin-top': '0',
        'margin-bottom': '0',
        'margin-left': '0',
        'margin-right': '0',
        'page-size': 'A4',
    }
    # Footer natif : rendu AVEC le contexte (email dynamique) dans un fichier temporaire,
    # lu par wkhtmltopdf via une URL file:// (un chemin Windows à backslash échoue).
    # margin-bottom 8mm = ligne à ~5mm du bas RÉEL (mesuré) ET garde 1 page.
    tmp_footer = None
    footer_tpl, margin_bottom = _FOOTER_MAP.get(template_name, (None, None))
    if footer_tpl:
        from django.template.loader import get_template
        from core.arabe import normaliser_arabe
        import tempfile
        footer_html = normaliser_arabe(get_template(footer_tpl).render(context))
        fd = tempfile.NamedTemporaryFile('w', suffix='.html', delete=False, encoding='utf-8')
        fd.write(footer_html)
        fd.close()
        tmp_footer = fd.name
        options['margin-bottom']  = margin_bottom
        options['footer-html']    = 'file:///' + tmp_footer.replace('\\', '/')
        options['footer-spacing'] = '0'
    try:
        pdf_bytes = render_pdf_bytes(template_name, context, options)
    except Exception as e:
        logger.exception('Erreur pdfkit.from_string: %s', e)
        raise
    finally:
        if tmp_footer:
            try:
                os.remove(tmp_footer)
            except OSError:
                pass
    # Signature numérique PAdES (anti-falsification). NON bloquante : renvoie le PDF
    # non signé si la signature est indisponible/échoue (cf. signing.sign_pdf_bytes).
    from .signing import sign_pdf_bytes
    return sign_pdf_bytes(pdf_bytes)


def _build_context_inscription(doc, etudiant, institution, data: dict) -> dict:
    """Contexte pour l'attestation d'inscription."""
    from apps.inscriptions.models import InscriptionAdministrative, InscriptionPedagogique

    inscription = None
    try:
        qs = InscriptionAdministrative.objects.filter(etudiant=etudiant)
        if doc.annee_universitaire:
            # Match exact (jamais __icontains : '2025' matche 2024-2025 ET 2025-2026 → bug ambiguite)
            qs = qs.filter(annee_univ__annee=doc.annee_universitaire)
        inscription = qs.select_related('filiere', 'annee_univ').order_by('-annee_univ__annee').first()
    except Exception:
        pass

    filiere     = getattr(inscription, 'filiere', None) or getattr(etudiant, 'filiere', None)
    filiere_nom = getattr(filiere, 'intitule_fr', '') or '—'
    filiere_ar  = getattr(filiere, 'intitule_ar', '') or ''
    filiere_code = getattr(filiere, 'code', '') or ''

    niveau      = getattr(inscription, 'niveau', None)
    niveau_label = NIVEAU_LABELS.get(niveau, f'Niveau {niveau}' if niveau else '—')
    niveau_ar   = NIVEAU_AR_LABELS.get(niveau, '')

    # Genre
    genre = getattr(etudiant, 'genre', 'M')
    civilite    = 'M'     if genre == 'M' else 'Mlle'
    genre_label = 'Masculin' if genre == 'M' else 'Féminin'
    genre_ar    = 'ذكر'   if genre == 'M' else 'أنثى'
    ne_e        = 'Né'    if genre == 'M' else 'Née'
    inscrit_e   = 'inscrit' if genre == 'M' else 'inscrite'

    date_naissance = _format_date_naissance(etudiant)
    photo_b64      = _get_photo_b64(etudiant)
    logo_b64       = _get_logo_b64(institution)

    # Groupe / sous-groupe depuis le Departement de l'étudiant
    departement = getattr(etudiant, 'departement', None)
    groupe      = getattr(departement, 'groupe', '') or ''
    annee_univ_label = getattr(getattr(inscription, 'annee_univ', None), 'annee', '') or \
                       getattr(departement, 'annee_universitaire', '') or \
                       doc.annee_universitaire or ''

    # Inscriptions pédagogiques → éléments par semestre
    elements_par_semestre = []
    groupes_info = []   # [{semestre_label, filiere_code, groupe, sous_groupe}]
    try:
        if inscription:
            inscriptions_ped = InscriptionPedagogique.objects.filter(
                inscription_admin=inscription,
            ).select_related('semestre').prefetch_related(
                'inscriptions_elements__em'
            ).order_by('semestre__code_semestre')

            from collections import OrderedDict as _OD
            semestre_dict = {}
            for insc_ped in inscriptions_ped:
                semestre = insc_ped.semestre
                if not semestre:
                    continue
                key = semestre.pk
                if key not in semestre_dict:
                    semestre_dict[key] = {
                        'semestre_label': str(semestre),
                        'modules': _OD(),   # module_pk → {code, intitule_fr, elements, total_credits, total_coeff}
                        'total_credits': 0,
                        'total_coeff': 0,
                    }
                for insc_elem in insc_ped.inscriptions_elements.select_related('em__module_lmd').all():
                    em = insc_elem.em
                    if not em:
                        continue
                    module    = getattr(em, 'module_lmd', None)
                    mod_key   = module.pk if module else 0
                    mod_code  = getattr(module, 'code', '—') if module else '—'
                    mod_label = getattr(module, 'intitule_fr', '—') if module else '—'

                    if mod_key not in semestre_dict[key]['modules']:
                        semestre_dict[key]['modules'][mod_key] = {
                            'code': mod_code, 'intitule_fr': mod_label,
                            'elements': [], 'total_credits': 0, 'total_coeff': 0,
                        }

                    credits_val = getattr(em, 'credits', 0) or 0
                    coeff_val   = getattr(em, 'coefficient', 0) or 0
                    semestre_dict[key]['modules'][mod_key]['elements'].append({
                        'code':        getattr(em, 'code_em', '—'),
                        'intitule_fr': getattr(em, 'intitule', '—'),
                        'credits':     credits_val,
                        'coefficient': coeff_val,
                    })
                    semestre_dict[key]['modules'][mod_key]['total_credits'] += credits_val
                    semestre_dict[key]['modules'][mod_key]['total_coeff']   += coeff_val
                    semestre_dict[key]['total_credits'] += credits_val
                    semestre_dict[key]['total_coeff']   += coeff_val

            # Convertir les OrderedDict de modules en listes
            for s in semestre_dict.values():
                s['modules'] = list(s['modules'].values())
            # Ne PAS afficher un semestre sans aucun élément. Après suppression de
            # dettes, l'inscription pédagogique (coquille) subsiste mais ne porte
            # plus d'EM. Une attestation d'inscription liste les éléments suivis →
            # un semestre à 0 EM n'a rien à attester et ne doit pas apparaître
            # (cas 23641 : S1/S2 vides restés après le nettoyage des dettes fantômes).
            elements_par_semestre = [s for s in semestre_dict.values() if s['modules']]

            # Info groupes (une ligne par semestre)
            for insc_ped in inscriptions_ped:
                s = insc_ped.semestre
                if not s:
                    continue
                groupes_info.append({
                    'semestre_label': str(s),
                    'filiere_code':   filiere_code,
                    'groupe':         groupe or '—',
                    'sous_groupe':    '',   # non stocké actuellement
                })
    except Exception as e:
        logger.warning('Erreur inscriptions pédagogiques pour doc %s: %s', doc.pk, e)

    # Email universitaire : champ stocké sinon matricule@domaine
    _email_stored = (getattr(etudiant, 'email', '') or '').strip()
    if _email_stored:
        email_univ = _email_stored
    else:
        # Domaine depuis site_web de l'institution, sinon fallback
        _site = (getattr(institution, 'site_web', '') or '').strip().lstrip('https://').lstrip('http://').lstrip('www.').strip('/')
        _domain = _site if _site else 'isms.esp.mr'
        email_univ = f"{etudiant.matricule}@{_domain}"

    return {
        'inscription':           inscription,
        'filiere_nom':           filiere_nom,
        'filiere_ar':            filiere_ar,
        'filiere_code':          filiere_code,
        'niveau_label':          niveau_label,
        'niveau_ar':             niveau_ar,
        'civilite':              civilite,
        'genre_label':           genre_label,
        'genre_ar':              genre_ar,
        'ne_e':                  ne_e,
        'inscrit_e':             inscrit_e,
        'annee_univ_label':      annee_univ_label,
        'date_naissance':        date_naissance,
        'photo_b64':             photo_b64,
        'logo_b64':              logo_b64,
        'groupe':                groupe,
        'elements_par_semestre': elements_par_semestre,
        'groupes_info':          groupes_info,
        'email_univ':            email_univ,
    }


def _calc_me(cc, tp, exam, has_tp: bool = False) -> float | None:
    """
    ME = (CC×2 + EX×3) / 5           si has_tp=False (EM.has_tp)
    ME = (CC×2 + EX×3 + TP×1) / 6   si has_tp=True
    has_tp déterminé par EM.has_tp — ignoré si note TP saisie par erreur.
    Retourne None si aucune note disponible.
    """
    if cc is None and exam is None:
        return None
    num = 0.0; den = 0
    if cc   is not None: num += cc   * 2; den += 2
    if exam is not None: num += exam * 3; den += 3
    if has_tp and tp is not None: num += tp * 1; den += 1
    return _round_half_up_float(num / den, 2) if den else None


def _calc_me_fixe(cc, tp, exam, has_tp: bool) -> float | None:
    """
    Formule pondérée FIXE (dénominateur constant) :
      ME = (CC×2 + EXAM×3 + TP×1) / 6   si has_tp
      ME = (CC×2 + EXAM×3) / 5           sinon
    Notes absentes traitées comme 0 (Strategie A : absent à l'examen = 0).
    Retourne None UNIQUEMENT si TOUTES les notes (CC, TP, EXAM) sont None
    (cas du semestre non encore evalue).
    """
    if cc is None and tp is None and exam is None:
        return None
    cc_v   = cc   if cc   is not None else 0.0
    tp_v   = tp   if tp   is not None else 0.0
    exam_v = exam if exam is not None else 0.0
    if has_tp:
        return _round_half_up_float((cc_v * 2 + exam_v * 3 + tp_v * 1) / 6, 2)
    else:
        return _round_half_up_float((cc_v * 2 + exam_v * 3) / 5, 2)


def _appliquer_plafond_rattrapage(me_n, me_r, me_final, plafond):
    """Plafond rattrapage (decision conseil scientifique) applique au releve.

    Un EM valide GRACE au rattrapage (echoue/absent en normale : me_n < 10 ou None,
    et me_r >= 10) voit sa note plafonnee a `plafond`. Aligne le releve sur
    ResultatElement et NoteCalculService.appliquer_regle_maximum_rattrapage.
    `plafond` None (session sans snapshot actif) => aucun plafond."""
    if (plafond is not None and me_r is not None and me_r >= 10
            and (me_n is None or me_n < 10)):
        return min(me_final, plafond)
    return me_final


def _consolider_ies_semestre(etudiant, semestre, annee_courante):
    """
    Consolidation des EMs d'un semestre selon les regles metier officielles :

    REGLE 1 — DERNIERE NOTE :
      Pour CC et EXAM, on prend toujours la note la PLUS RECENTE saisie
      (peu importe sa valeur, meme si plus basse que l'originale).

    REGLE 2 — TP PRESERVE :
      Le TP est TOUJOURS celui de l'IP D'ORIGINE (premiere annee où l'EM a
      ete inscrit). Jamais retape lors d'une dette.

    REGLE 3 — EM ACQUIS FIGE :
      Si l'EM a ete VALIDE (code_statut V/VC/VCI/VCS) lors de son IP d'origine,
      ses notes sont IMMUABLES. Aucune ré-saisie ulterieure ne le modifie.

    REGLE 4 — FORMULE FIXE :
      ME = (CC×2 + EXAM×3 + TP×1) / 6   si has_tp
      ME = (CC×2 + EXAM×3) / 5           sinon
      Le denominateur reste constant. Notes absentes -> 0.

    Retourne : list de dicts {
        'em', 'ie', 'annee_source', 'annee_source_id',
        'est_dette', 'est_courante', 'is_acquis',
        'cc', 'tp', 'exam', 'exam_rat', 'has_rat', 'me',
    }
    """
    from apps.evaluations.models import Note, SessionEvaluation, ResultatElement
    from apps.inscriptions.models import InscriptionPedagogique, InscriptionElement

    code_sem = semestre.code_semestre
    parite_session = 'Impairs' if semestre.type_semestre == 'I' else 'Pairs'

    # IPs triees par annee CROISSANTE (ancien -> recent), filtrees a
    # `annee_courante` et anterieures uniquement.
    #
    # Sans ce filtre, le releve d'une annee N est pollue par les notes des
    # annees > N (ex. redoublement N+1 qui ecrase via la regle "derniere note").
    # Avec ce filtre :
    #   - Releve 2024-2025 → snapshot pur 2024-2025 (pas de pollution N+1)
    #   - Releve 2025-2026 → consolidation activee sur 2024-2025 + 2025-2026
    #     (cas dette redoublee : derniere saisie l'emporte conformement a la regle 1)
    ips_qs = InscriptionPedagogique.objects.filter(
        inscription_admin__etudiant=etudiant,
        semestre__code_semestre=code_sem,
    )
    if annee_courante is not None:
        ips_qs = ips_qs.filter(
            inscription_admin__annee_univ__annee__lte=annee_courante.annee,
        )
    ips = list(
        ips_qs
        .select_related('semestre', 'inscription_admin__annee_univ')
        .order_by('inscription_admin__annee_univ__annee')
    )
    if not ips:
        return []

    # Cache des sessions par annee
    sessions_par_annee = {}
    def _get_sessions(annee_obj):
        if not annee_obj:
            return None, None, None
        if annee_obj.pk in sessions_par_annee:
            return sessions_par_annee[annee_obj.pk]
        sn = SessionEvaluation.objects.filter(
            annee_univ=annee_obj, type_session='normale', type_semestre=parite_session,
        ).first()
        sr = SessionEvaluation.objects.filter(
            annee_univ=annee_obj, type_session='rattrapage', type_semestre=parite_session,
        ).first()
        sn_id = sn.id if sn else None
        sr_id = sr.id if sr else None
        # Plafond rattrapage figé sur la session SR (None si snapshot inactif/absent).
        sr_plaf = (
            float(sr.rattrapage_plafond)
            if (sr and sr.rattrapage_plafond_actif and sr.rattrapage_plafond is not None)
            else None
        )
        sessions_par_annee[annee_obj.pk] = (sn_id, sr_id, sr_plaf)
        return sn_id, sr_id, sr_plaf

    # Collecter toutes les (IE, IP, annee, sn_id, sr_id) puis indexer notes
    occurrences = []   # liste ordonnee (par annee croissante)
    all_ie_ids = []
    for ip in ips:
        annee_obj = ip.inscription_admin.annee_univ
        sn_id, sr_id, sr_plaf = _get_sessions(annee_obj)
        ies = list(
            InscriptionElement.objects
            .filter(inscription_ped=ip)
            .select_related('em', 'em__module_lmd')
        )
        for ie in ies:
            if ie.em:
                occurrences.append((ie, ip, annee_obj, sn_id, sr_id, sr_plaf))
                all_ie_ids.append(ie.id)

    # Index notes : (ie_id, session_id, type_note) -> valeur
    notes_idx = {}
    if all_ie_ids:
        for n in Note.objects.filter(inscription_element_id__in=all_ie_ids):
            notes_idx[(n.inscription_element_id, n.session_id, n.type_note)] = float(n.valeur)

    # Index code_statut le plus recent par IE (pour detecter "EM acquis")
    re_statut_idx = {}
    if all_ie_ids:
        for re_obj in ResultatElement.objects.filter(
            inscription_element_id__in=all_ie_ids,
        ).order_by('inscription_element_id', '-date_calcul'):
            if re_obj.inscription_element_id not in re_statut_idx:
                re_statut_idx[re_obj.inscription_element_id] = (re_obj.code_statut or '').strip()

    # Regrouper occurrences par code EM en preservant l'ordre chronologique
    from collections import OrderedDict as _OD
    occurrences_par_em = _OD()
    for occ in occurrences:
        em_code = occ[0].em.code_em
        occurrences_par_em.setdefault(em_code, []).append(occ)

    # ── Calcul consolidé pour chaque EM ──────────────────────────────────────
    STATUTS_ACQUIS = {'V', 'VC', 'VCI', 'VCS'}
    consolides = []

    for em_code, occs in occurrences_par_em.items():
        ie_origine, ip_origine, annee_origine, sn_orig, sr_orig, sr_plaf_orig = occs[0]
        em = ie_origine.em
        has_tp = bool(getattr(em, 'has_tp', False))

        # Notes de l'IP d'origine
        cc_orig    = notes_idx.get((ie_origine.id, sn_orig, 'CC'))    if sn_orig else None
        tp_orig    = notes_idx.get((ie_origine.id, sn_orig, 'TP'))    if sn_orig else None
        exam_orig  = notes_idx.get((ie_origine.id, sn_orig, 'EXAM'))  if sn_orig else None
        exam_r_orig = notes_idx.get((ie_origine.id, sr_orig, 'EXAM')) if sr_orig else None

        # REGLE 3 : EM ACQUIS dans l'IP d'origine -> figé immuable
        statut_origine = re_statut_idx.get(ie_origine.id, '')
        is_acquis = statut_origine in STATUTS_ACQUIS

        if is_acquis:
            # Notes figees, formule fixe pour calcul ME
            me_n = _calc_me_fixe(cc_orig, tp_orig, exam_orig, has_tp=has_tp)
            me_r = _calc_me_fixe(cc_orig, tp_orig, exam_r_orig, has_tp=has_tp) if exam_r_orig is not None else None
            if me_r is not None and me_n is not None:
                me_final = max(me_n, me_r)   # Art. 18 max SN/SR intra-annee
            else:
                me_final = me_r if me_r is not None else me_n
            # Plafond rattrapage figé sur la session SR d'origine.
            me_final = _appliquer_plafond_rattrapage(me_n, me_r, me_final, sr_plaf_orig)
            consolides.append({
                'em':                em,
                'ie':                ie_origine,
                'annee_source':      annee_origine.annee if annee_origine else '',
                'annee_source_id':   annee_origine.pk if annee_origine else None,
                'est_dette':         False,
                'est_courante':      bool(annee_courante and annee_origine and annee_origine.pk == annee_courante.pk),
                'is_acquis':         True,
                'cc':                cc_orig,
                'tp':                tp_orig,
                'exam':              exam_orig,
                'exam_rat':          exam_r_orig,
                'has_rat':           exam_r_orig is not None,
                'me':                me_final,
            })
            continue

        # REGLES 1+2 : EM non-acquis -> CC/EXAM derniere saisie, TP origine
        cc_final     = cc_orig     # initial : origine
        exam_final   = exam_orig
        exam_r_final = exam_r_orig
        tp_final     = tp_orig     # TOUJOURS l'origine (regle 2)
        sr_plaf_final = sr_plaf_orig  # plafond de la SR ou le rattrapage retenu a ete pris
        ie_retenue       = ie_origine
        annee_retenue    = annee_origine
        est_dette_final  = bool(ie_origine.est_dette)

        # Parcourir les IPs ulterieures (ordre chronologique croissant)
        for ie_n, ip_n, annee_n, sn_n, sr_n, sr_plaf_n in occs[1:]:
            cc_n     = notes_idx.get((ie_n.id, sn_n, 'CC'))    if sn_n else None
            exam_n   = notes_idx.get((ie_n.id, sn_n, 'EXAM'))  if sn_n else None
            exam_r_n = notes_idx.get((ie_n.id, sr_n, 'EXAM'))  if sr_n else None
            # NOTE : on ignore le TP saisi cette annee (regle 2 — TP non retapable)

            a_des_saisies = (cc_n is not None or exam_n is not None or exam_r_n is not None)
            if cc_n is not None:
                cc_final = cc_n
            if exam_n is not None:
                exam_final = exam_n
            if a_des_saisies:
                # Tentative ultérieure RETENUE (redoublement) : le rattrapage provient
                # de CETTE année — None si l'étudiant n'a pas rattrapé cette année.
                # L'ancien rattrapage devient CADUC, comme CC/EXAM. Sinon un RAT d'une
                # année antérieure resterait collé à un CC/EXAM récent (bug 22640).
                exam_r_final    = exam_r_n
                sr_plaf_final   = sr_plaf_n
                ie_retenue      = ie_n
                annee_retenue   = annee_n
                est_dette_final = bool(ie_n.est_dette)

        # REGLE 4 : formule fixe
        me_n = _calc_me_fixe(cc_final, tp_final, exam_final, has_tp=has_tp)
        me_r = _calc_me_fixe(cc_final, tp_final, exam_r_final, has_tp=has_tp) if exam_r_final is not None else None
        if me_r is not None and me_n is not None:
            me_final = max(me_n, me_r)   # Art. 18 max SN/SR
        else:
            me_final = me_r if me_r is not None else me_n
        # Plafond rattrapage figé sur la session SR du rattrapage retenu.
        me_final = _appliquer_plafond_rattrapage(me_n, me_r, me_final, sr_plaf_final)

        consolides.append({
            'em':              em,
            'ie':              ie_retenue,
            'annee_source':    annee_retenue.annee if annee_retenue else '',
            'annee_source_id': annee_retenue.pk if annee_retenue else None,
            'est_dette':       est_dette_final,
            'est_courante':    bool(annee_courante and annee_retenue and annee_retenue.pk == annee_courante.pk),
            'is_acquis':       False,
            'cc':              cc_final,
            'tp':              tp_final,
            'exam':            exam_final,
            'exam_rat':        exam_r_final,
            'has_rat':         exam_r_final is not None,
            'me':              me_final,
        })

    # Tri par module + EM code
    consolides.sort(key=lambda c: (
        c['em'].module_lmd.code if c['em'].module_lmd else 'zzz',
        c['em'].code_em,
    ))
    return consolides


def calculer_resultat_semestre_consolide(etudiant, semestre, annee_univ):
    """
    SOURCE UNIQUE — résultat CONSOLIDÉ d'un semestre, compensation-aware (Art. 12-15).

    Consolide les EM (dernière note + EM acquis figés via _consolider_ies_semestre),
    calcule les moyennes module, applique la compensation Art. 13/14 et la
    capitalisation Art. 12/13/15, puis retropropage les décisions (un EM dans un
    module/semestre validé devient « Validé » même < 10 → reconnaît VCI/VCS).

    Utilisé par le relevé PDF (_build_context_releve) ET la consultation écran
    (apps.absence.views.notes_etudiant) : mêmes crédits / moyenne / statuts partout.

    Retourne dict {
      'modules': list,            # module→EM, decisions propagées (compensation)
      'moyenne_semestre': float|None,
      'credits_valides': int,     # crédits capitalisés finaux (Art. 15)
      'credits_valides_em': int,  # crédits capitalisés EM (Art. 12/13)
      'credits_total': int,
      'est_admis': bool,
      'all_mm_above_8': bool,
      'has_eliminatoire': bool,
    }
    """
    from collections import OrderedDict

    consolides = _consolider_ies_semestre(etudiant, semestre, annee_univ)

    modules_dict: OrderedDict = OrderedDict()
    credits_total = 0

    for cand in consolides:
        em = cand['em']
        module    = em.module_lmd
        mod_key   = module.pk   if module else 0
        mod_code  = module.code if module else '—'
        mod_label = module.intitule_fr if module else '—'

        if mod_key not in modules_dict:
            modules_dict[mod_key] = {'code': mod_code, 'intitule_fr': mod_label, 'elements': []}

        me     = cand['me']
        est_valide       = (me is not None and me >= 10)
        est_eliminatoire = (me is None) or (me < 6)

        if em.credits:
            credits_total += em.credits

        modules_dict[mod_key]['elements'].append({
            'code':             em.code_em,
            'intitule_fr':      em.intitule,
            'cc':               cand['cc'],
            'tp':               cand['tp'],
            'exam':             cand['exam'],
            'cc_rat':           None,
            'tp_rat':           None,
            'exam_rat':         cand['exam_rat'],
            'has_rat':          cand['has_rat'],
            'me':               me,
            'credits':          em.credits,
            'coefficient':      em.coefficient,
            'est_valide':       est_valide,
            'est_eliminatoire': est_eliminatoire,
            'decision':         'Validé' if est_valide else 'Non validé',
            'annee_source':     cand['annee_source'],
            'est_courante':     cand['est_courante'],
            'est_dette':        cand['est_dette'],
            # Champs supplémentaires pour la consultation écran (ignorés par le relevé) :
            'is_acquis':        cand.get('is_acquis', False),
            'has_tp':           bool(getattr(em, 'has_tp', False)),
        })

    # ── Moyennes modules (Strategie A : EM sans note = 0/20) ─────────────────
    modules_list = []
    total_coeff_semestre = 0.0
    for mod in modules_dict.values():
        elems = mod['elements']
        num = den = 0.0
        has_elim_in_mod = False
        has_ungraded_em = False
        for e in elems:
            if e['coefficient']:
                me_val = e['me'] if e['me'] is not None else 0.0
                num += me_val * float(e['coefficient'])
                den += float(e['coefficient'])
                total_coeff_semestre += float(e['coefficient'])
                if e['me'] is None:
                    has_ungraded_em = True
            if e['est_eliminatoire']:
                has_elim_in_mod = True
        note_module = _round_half_up_float(num / den, 2) if den else None
        mod_valide  = (note_module is not None and note_module >= 10 and not has_elim_in_mod)
        mod['note_module']      = note_module
        mod['has_eliminatoire'] = has_elim_in_mod
        mod['has_ungraded_em']  = has_ungraded_em
        mod['decision']         = 'Validé' if mod_valide else 'Non validé'
        modules_list.append(mod)

    # ── MGS + crédits capitalisés (Art. 12 + Art. 13) ───────────────────────
    mg_num = 0.0
    credits_valides_em = 0
    has_eliminatoire_global = False
    for mod in modules_list:
        mod_valide_local = (mod['decision'] == 'Validé')
        for e in mod['elements']:
            if e['coefficient']:
                me_val = e['me'] if e['me'] is not None else 0.0
                mg_num += me_val * float(e['coefficient'])
            if e['est_eliminatoire']:
                has_eliminatoire_global = True
            if e['credits'] and not e['est_eliminatoire']:
                if mod_valide_local or e['est_valide']:
                    credits_valides_em += e['credits']

    moyenne_semestre = _round_half_up_float(mg_num / total_coeff_semestre, 2) if total_coeff_semestre > 0 else None

    # Art. 15 : semestre validé si MGS ≥ 10 ET tous MM ≥ 8 ET aucune note éliminatoire
    all_mm_above_8 = all(
        (mod['note_module'] is not None and mod['note_module'] >= 8)
        for mod in modules_list
    )
    est_admis = (
        moyenne_semestre is not None and
        moyenne_semestre >= 10 and
        all_mm_above_8 and
        not has_eliminatoire_global
    )

    # Retropropagation des décisions (Art. 14 puis Art. 13) — VCI/VCS reflétés.
    if est_admis:
        for mod in modules_list:
            if (mod['decision'] != 'Validé'
                    and mod['note_module'] is not None
                    and mod['note_module'] >= 8
                    and not mod['has_eliminatoire']):
                mod['decision'] = 'Validé'
    for mod in modules_list:
        if mod['decision'] == 'Validé':
            for e in mod['elements']:
                if e['est_eliminatoire']:
                    continue   # E reste E (jamais validé, Art. 12 al. 2)
                if e['decision'] != 'Validé':
                    e['decision'] = 'Validé'

    # Art. 15 : semestre validé → tous les crédits du semestre capitalisés d'un coup.
    credits_valides = credits_total if est_admis else credits_valides_em

    return {
        'modules':            modules_list,
        'moyenne_semestre':   moyenne_semestre,
        'credits_valides':    credits_valides,
        'credits_valides_em': credits_valides_em,
        'credits_total':      credits_total,
        'est_admis':          est_admis,
        'all_mm_above_8':     all_mm_above_8,
        'has_eliminatoire':   has_eliminatoire_global,
    }


def _build_context_releve(doc, etudiant, institution, data: dict) -> dict:
    """
    Contexte pour le relevé de notes semestriel.
    Données groupées par module, colonnes CC/EX/RAT/ME/Crédit/Décision.
    ME calculé on-the-fly : (CC×2 + EX×3[+TP×1]) / (5 ou 6).
    MG = Σ(ME × coeff) / 20.
    """
    from collections import OrderedDict
    from apps.evaluations.models import (
        Note, SessionEvaluation,
    )
    from apps.inscriptions.models import (
        InscriptionAdministrative, InscriptionPedagogique, InscriptionElement,
    )

    semestre_id    = doc.semestre_id or data.get('semestre')
    annee_univ_str = doc.annee_universitaire or data.get('annee_universitaire', '')

    # ── Valeurs vides par défaut ──────────────────────────────────────────────
    empty = {
        'modules': [], 'semestre_label': '—', 'semestre_label_ar': '',
        'filiere_nom': '—', 'filiere_ar': '', 'niveau_label': '—', 'niveau_ar': '',
        'date_naissance': _format_date_naissance(etudiant),
        'photo_b64':      _get_photo_b64(etudiant),
        'logo_b64':       _get_logo_b64(institution),
        'moyenne_semestre': None, 'credits_valides': 0, 'credits_total': 0,
        'decision_semestre': '—', 'mention': '—', 'mention_code': 'AJ',
        'has_rat_session': False, 'annee_univ_label': annee_univ_str,
    }

    # ── 1. Inscription administrative ─────────────────────────────────────────
    # Match exact sur annee_univ__annee (jamais __icontains : '2025' matche
    # 2024-2025 ET 2025-2026 → ambiguite, retourne la mauvaise inscription).
    ia_qs = InscriptionAdministrative.objects.filter(etudiant=etudiant)
    if annee_univ_str:
        ia_qs = ia_qs.filter(annee_univ__annee=annee_univ_str)
    inscription = ia_qs.select_related('filiere', 'annee_univ').order_by('-annee_univ__annee').first()
    if not inscription or not semestre_id:
        logger.warning('_build_context_releve: inscription ou semestre introuvable pour etudiant=%s annee=%s', etudiant.pk, annee_univ_str)
        return empty

    # ── 2. Inscription pédagogique ────────────────────────────────────────────
    insc_ped = (
        InscriptionPedagogique.objects
        .filter(inscription_admin=inscription, semestre_id=semestre_id)
        .select_related('semestre')
        .first()
    )
    if not insc_ped:
        logger.warning('_build_context_releve: InscriptionPedagogique introuvable semestre_id=%s', semestre_id)
        return empty

    # ── 3. Semestre → parité → sessions ──────────────────────────────────────
    semestre          = insc_ped.semestre
    code_sem          = semestre.code_semestre  # ex. 'S1', 'S2', …
    parity_map        = {'I': 'Impairs', 'P': 'Pairs'}
    type_sem_session  = parity_map.get(semestre.type_semestre, 'Impairs')
    annee_univ        = inscription.annee_univ

    session_normale = SessionEvaluation.objects.filter(
        annee_univ=annee_univ,
        type_session='normale',
        type_semestre=type_sem_session,
    ).first()
    session_rat = SessionEvaluation.objects.filter(
        annee_univ=annee_univ,
        type_session='rattrapage',
        type_semestre=type_sem_session,
    ).first()

    sn_id = session_normale.id if session_normale else None
    sr_id = session_rat.id    if session_rat    else None
    all_session_ids = [s for s in [sn_id, sr_id] if s]

    # ── 4. Calcul consolidé du semestre (compensation Art. 12-15) ────────────
    # SOURCE UNIQUE : délégué au helper partagé, qui consolide les EM (dernière
    # note + EM acquis) puis applique compensation et capitalisation. La
    # consultation écran (notes_etudiant) utilise EXACTEMENT le même helper →
    # crédits / moyenne / statuts strictement identiques entre relevé et écran.
    _res = calculer_resultat_semestre_consolide(etudiant, semestre, annee_univ)
    modules_list            = _res['modules']
    credits_total           = _res['credits_total']
    credits_valides_em      = _res['credits_valides_em']
    moyenne_semestre        = _res['moyenne_semestre']
    all_mm_above_8          = _res['all_mm_above_8']
    has_eliminatoire_global = _res['has_eliminatoire']
    est_admis               = _res['est_admis']
    credits_valides         = _res['credits_valides']

    # ── 8. Mention ────────────────────────────────────────────────────────────
    mention = '—'; mention_code = 'AJ'
    if moyenne_semestre is not None:
        if   moyenne_semestre >= 16: mention, mention_code = 'Très Bien', 'TB'
        elif moyenne_semestre >= 14: mention, mention_code = 'Bien',      'B'
        elif moyenne_semestre >= 12: mention, mention_code = 'Assez Bien','AB'
        elif moyenne_semestre >= 10: mention, mention_code = 'Passable',  'P'
        else:                        mention, mention_code = 'Ajourné',   'AJ'

    # ── 9. Labels ─────────────────────────────────────────────────────────────
    semestre_label    = SEMESTRE_FR_LABELS.get(code_sem, str(semestre))
    semestre_label_ar = SEMESTRE_AR_LABELS.get(code_sem, '')
    filiere           = getattr(inscription, 'filiere', None)
    filiere_nom       = getattr(filiere, 'intitule_fr', '') or '—'
    filiere_ar        = getattr(filiere, 'intitule_ar', '') or ''
    niveau            = getattr(inscription, 'niveau', None)
    niveau_label      = NIVEAU_LABELS.get(niveau, f'Niveau {niveau}' if niveau else '—')
    niveau_ar         = NIVEAU_AR_LABELS.get(niveau, '')
    annee_univ_label  = getattr(annee_univ, 'annee', '') if annee_univ else annee_univ_str

    return {
        'modules':              modules_list,
        'semestre_label':       semestre_label,
        'semestre_label_ar':    semestre_label_ar,
        'filiere_nom':          filiere_nom,
        'filiere_ar':           filiere_ar,
        'niveau_label':         niveau_label,
        'niveau_ar':            niveau_ar,
        'date_naissance':       _format_date_naissance(etudiant),
        'photo_b64':            _get_photo_b64(etudiant),
        'logo_b64':             _get_logo_b64(institution),
        'moyenne_semestre':     _round_half_up(moyenne_semestre, 2),
        'credits_valides':      credits_valides,
        'credits_valides_em':   credits_valides_em,
        'credits_total':        credits_total,
        'decision_semestre':    'Validé' if est_admis else 'Non validé',
        'est_admis':            est_admis,
        'all_mm_above_8':       all_mm_above_8,
        'has_eliminatoire':     has_eliminatoire_global,
        'mention':              mention,
        'mention_code':         mention_code,
        'has_rat_session':      session_rat is not None,
        'annee_univ_label':     annee_univ_label,
    }


def _build_context_diplome(doc, etudiant, institution, data: dict) -> dict:
    """Contexte pour le diplôme."""
    from apps.inscriptions.models import InscriptionAdministrative
    inscription = InscriptionAdministrative.objects.filter(
        etudiant=etudiant,
    ).select_related('filiere').order_by('-id').first()

    niveau = getattr(inscription, 'niveau', 3)
    if niveau <= 3:
        grade = 'Licence'
        grade_ar = 'الليسانس'
    elif niveau <= 5:
        grade = 'Master'
        grade_ar = 'الماستر'
    else:
        grade = 'Doctorat'
        grade_ar = 'الدكتوراه'

    filiere_nom = getattr(getattr(inscription, 'filiere', None), 'intitule_fr', '') or '—'

    # Moyenne / mention / filière : lues depuis le registre des diplômes (autorité
    # serveur), JAMAIS depuis la requête client — anti-falsification de la moyenne.
    from .models import RegistreDiplome
    registre = RegistreDiplome.objects.filter(etudiant=etudiant)
    if doc.annee_universitaire:
        registre = registre.filter(annee_universitaire=doc.annee_universitaire)
    registre = registre.order_by('-date_delivrance').first()

    from apps.evaluations.services.calcul_notes import NoteCalculService
    if registre:
        moyenne = float(registre.moyenne_generale)
        if getattr(registre.filiere, 'intitule_fr', ''):
            filiere_nom = registre.filiere.intitule_fr
        mention = registre.mention or NoteCalculService.calculer_mention(moyenne)
    else:
        moyenne = 0.0
        mention = NoteCalculService.calculer_mention(moyenne)
    mention_code = {
        'Très Bien': 'TB', 'Bien': 'B', 'Assez Bien': 'AB',
        'Passable': 'P', 'Ajourné': 'AJ',
    }.get(mention, 'P')

    return {
        'grade':             grade,
        'grade_ar':          grade_ar,
        'filiere_nom':       filiere_nom,
        'annee_universitaire': doc.annee_universitaire,
        'moyenne_generale':  _round_half_up(moyenne, 2),
        'mention':           mention,
        'mention_code':      mention_code,
    }


def _de_l_fr(nom: str) -> str:
    """
    Élision française pour un titre « Directeur … » : « de l'X » si X commence par
    une voyelle ou un h (Institut, Université, École…), sinon « de X ». Permet un
    titre de fonction dérivé dynamiquement du nom de l'établissement.
    """
    n = (nom or '').strip()
    if not n:
        return ''
    return f"de l'{n}" if n[0].lower() in 'aàâäeéèêëiîïoôöuùûüyh' else f'de {n}'


def _build_context_attestation_diplome(doc, etudiant, institution, data: dict) -> dict:
    """Contexte pour l'« Attestation de diplôme de licence professionnelle ».

    Reproduit le modèle officiel bilingue FR/AR. Données dynamiques tirées du
    REGISTRE DES DIPLÔMES (autorité serveur : mention, filière, année, date du PV
    de jury) et de l'étudiant (identité, NNI, naissance). Signataires lus depuis
    la configuration institution (Directeur + Commandant du Groupe de tutelle).
    """
    from .models import RegistreDiplome

    # Registre = autorité (anti-falsification) : mention/filière/année/date jury.
    registre = RegistreDiplome.objects.filter(etudiant=etudiant)
    if doc.annee_universitaire:
        registre = registre.filter(annee_universitaire=doc.annee_universitaire)
    registre = registre.select_related('filiere').order_by('-date_delivrance').first()

    filiere    = getattr(registre, 'filiere', None) or getattr(etudiant, 'filiere', None)
    filiere_fr = getattr(filiere, 'intitule_fr', '') or '—'
    filiere_ar = getattr(filiere, 'intitule_ar', '') or ''
    annee      = getattr(registre, 'annee_universitaire', '') or doc.annee_universitaire or ''

    # Mention : grille DIPLÔME (≥16 Excellent, ≥14 Très Bien, ≥12 Bien, ≥10 Assez
    # Bien — pas d'« Insuffisant »), calculée sur la moyenne générale du registre.
    # Repli sur la mention stockée si la moyenne est absente. + équivalent arabe.
    moyenne = getattr(registre, 'moyenne_generale', None) if registre else None
    if moyenne is not None:
        mention_fr = _mention_diplome(moyenne)
    else:
        mention_fr = getattr(registre, 'mention', '') or ''
    mention_ar = MENTION_DIPLOME_AR.get(mention_fr, '')

    # Date de DÉLIBÉRATION : tirée du PV annuel d'attribution du diplôme
    # (date_deliberation), sinon repli sur la date de délivrance au registre.
    from apps.evaluations.models import LigneDeliberation
    _delib = (LigneDeliberation.objects
              .filter(inscription_admin__etudiant=etudiant, pv__type_pv='annuel')
              .select_related('pv'))
    if annee:
        _delib = _delib.filter(pv__annee_univ__annee=annee)
    _delib = _delib.order_by('-pv__date_deliberation').first()
    _delib_date = (getattr(getattr(_delib, 'pv', None), 'date_deliberation', None)
                   or (registre.date_delivrance if registre else None))
    jury_date = _delib_date.strftime('%d/%m/%Y') if _delib_date else ''

    # Identité bilingue.
    genre = getattr(etudiant, 'genre', 'M')
    is_f  = (genre == 'F')
    nom_fr = (f'{etudiant.prenom_fr} {etudiant.nom_fr}'.strip()) or etudiant.nom
    nom_ar = (f'{etudiant.prenom_ar} {etudiant.nom_ar}'.strip())

    # Accords de genre (FR + AR).
    etu_label_fr = "L'étudiante" if is_f else "L'étudiant"
    ne_fr        = 'Née'         if is_f else 'Né'
    etu_label_ar = 'الطالبة'      if is_f else 'الطالب'
    ne_ar        = 'المولودة'     if is_f else 'المولود'
    satisf_ar    = 'قد استوفت'    if is_f else 'قد استوفى'
    delivree_ar  = 'سلمت لها'      if is_f else 'سلمت له'

    # Signataires (config institution).
    inst_nom_fr  = (getattr(institution, 'nom_complet_fr', '') or getattr(institution, 'nom_fr', '')
                    or getattr(institution, 'nom', ''))
    inst_nom_ar  = getattr(institution, 'nom_ar', '') or ''
    # FONCTION du signataire, DÉRIVÉE dynamiquement du nom de l'établissement — et
    # NON l'honorifique (« Dr » / « د. ») stocké dans directeur_titre_*, qui ne
    # convient pas comme titre de signature sur un document officiel. Ainsi le
    # titre suit automatiquement le nom de l'institution (dynamique, sans hardcode).
    dir_titre_fr = f'Directeur {_de_l_fr(inst_nom_fr)}' if inst_nom_fr else 'Le Directeur'
    dir_titre_ar = f'مدير {inst_nom_ar}' if inst_nom_ar else 'المدير'

    return {
        'nom_fr':         nom_fr,
        'nom_ar':         nom_ar,
        'matricule':      etudiant.matricule,
        'nni':            getattr(etudiant, 'cni', '') or '',
        'date_naissance': _format_date_naissance(etudiant),
        'lieu_fr':        getattr(etudiant, 'lieu_naissance_fr', '') or '',
        'lieu_ar':        getattr(etudiant, 'lieu_naissance_ar', '') or '',
        'filiere_fr':     filiere_fr,
        'filiere_ar':     filiere_ar,
        'filiere_nom':    filiere_fr,   # consommé par le générateur de QR
        'annee':          annee,
        'mention_fr':     mention_fr,
        'mention_ar':     mention_ar,
        'jury_date':      jury_date,
        # Accords de genre
        'etu_label_fr':   etu_label_fr,
        'ne_fr':          ne_fr,
        'etu_label_ar':   etu_label_ar,
        'ne_ar':          ne_ar,
        'satisf_ar':      satisf_ar,
        'delivree_ar':    delivree_ar,
        # En-tête / tutelle
        'groupe_fr':      getattr(institution, 'groupe_fr', '') or '',
        'groupe_ar':      getattr(institution, 'groupe_ar', '') or '',
        'inst_nom_fr':    inst_nom_fr,
        'inst_nom_ar':    inst_nom_ar,
        'logo_b64':       _imagefield_b64(getattr(institution, 'logo', None)),
        # Logo du Groupe de tutelle (champ dédié) ; repli sur logo_republique pour
        # les institutions configurées avant l'ajout du champ.
        'logo_groupe_b64': (_imagefield_b64(getattr(institution, 'logo_groupe', None))
                            or _imagefield_b64(getattr(institution, 'logo_republique', None))),
        # Signataire 1 : Directeur de l'institut
        'dir_titre_fr':   dir_titre_fr,
        'dir_titre_ar':   dir_titre_ar,
        'dir_nom_fr':     getattr(institution, 'directeur_nom_fr', '') or '',
        'dir_nom_ar':     getattr(institution, 'directeur_nom_ar', '') or '',
        'dir_sig_b64':    _imagefield_b64(getattr(institution, 'directeur_signature', None)),
        # Signataire 2 : Commandant du Groupe de tutelle
        'cmd_titre_fr':   getattr(institution, 'commandant_titre_fr', '') or 'Le Commandant du Groupe Polytechnique',
        'cmd_titre_ar':   getattr(institution, 'commandant_titre_ar', '') or 'قائد مجمع بوليتكنيك',
        'cmd_nom_fr':     getattr(institution, 'commandant_nom_fr', '') or '',
        'cmd_nom_ar':     getattr(institution, 'commandant_nom_ar', '') or '',
        'cmd_sig_b64':    _imagefield_b64(getattr(institution, 'commandant_signature', None)),
    }


_TEMPLATE_MAP = {
    'attestation_inscription': 'documents/attestation_inscription.html',
    'releve_semestre':         'documents/releve_notes.html',
    'releve_complet':          'documents/releve_notes.html',
    'releve_notes':            'documents/releve_notes.html',
    'attestation_reussite':    'documents/attestation_inscription.html',
    'attestation_diplome':     'documents/attestation_diplome.html',
    'diplome':                 'documents/diplome.html',
}

_CONTEXT_BUILDER = {
    'attestation_inscription': _build_context_inscription,
    'releve_semestre':         _build_context_releve,
    'releve_complet':          _build_context_releve,
    'releve_notes':            _build_context_releve,
    'attestation_reussite':    _build_context_inscription,
    'attestation_diplome':     _build_context_attestation_diplome,
    'diplome':                 _build_context_diplome,
}


def _generer_pdf(doc: DocumentOfficiel, etudiant, data: dict, is_duplicata: bool = False) -> bytes | None:
    """Génère le PDF du document. Retourne None si une erreur survient (non bloquant).

    `is_duplicata` (filigrane) est décidé par l'appelant (l'endpoint de délivrance),
    PAS ici : la génération ne marque jamais le document comme délivré (sinon un
    pré-cache marquerait à tort la 1re vraie impression comme duplicata)."""
    try:
        institution = _get_institution(etudiant)
        type_doc = doc.type_document
        template = _TEMPLATE_MAP.get(type_doc)
        builder  = _CONTEXT_BUILDER.get(type_doc)

        if not template or not builder:
            logger.warning('Pas de template pour type_document=%s', type_doc)
            return None

        # URL de vérification (configurable via settings)
        from django.conf import settings
        base_url = getattr(settings, 'DOCUMENTS_BASE_URL', '').rstrip('/')
        verify_url = f'{base_url}/verifier/{doc.token_verification}' if base_url else f'/verifier/{doc.token_verification}'

        specific_ctx = builder(doc, etudiant, institution, data)

        qr_image = _get_qr_base64(
            str(doc.token_verification),
            base_url,
            etudiant=etudiant,
            doc=doc,
            filiere_nom=specific_ctx.get('filiere_nom', ''),
            niveau_label=specific_ctx.get('niveau_label', ''),
        )
        if not qr_image:
            logger.warning('qr_image est None pour doc %s — QR absent du PDF', doc.pk)

        context = {
            'doc':         doc,
            'etudiant':    etudiant,
            'institution': institution or type('FakeInstitution', (), {
                'nom': 'Université', 'nom_fr': '', 'nom_ar': '',
                'logo': None, 'logo_republique': None,
                'directeur_nom_fr': '', 'directeur_titre_fr': '', 'directeur_signature': None,
            })(),
            'today':        timezone.now().strftime('%d/%m/%Y'),
            'verify_url':   verify_url,
            'qr_image':     qr_image,
            'is_duplicata': is_duplicata,
            **specific_ctx,
        }

        return _render_pdf(template, context)

    except Exception as e:
        logger.exception('Erreur génération PDF pour doc %s: %s', doc.pk, e)
        return None


def _creer_document_officiel(etudiant, type_document, annee_univ, semestre_id, user):
    """Crée et persiste UN DocumentOfficiel (numéro de série, token, hash) pour un
    étudiant. Applique les garde-fous métier (diplôme au registre ; attestation /
    relevé = inscription requise). NE génère PAS le PDF (rendu à la demande).
    Factorisé pour être réutilisé par la génération UNITAIRE et GROUPÉE."""
    # Diplôme / attestation de diplôme : ne générer que si l'étudiant figure au
    # registre des diplômes (autorité serveur — diplôme effectivement attribué).
    if type_document in ('diplome', 'attestation_diplome'):
        from .models import RegistreDiplome
        registre_qs = RegistreDiplome.objects.filter(etudiant=etudiant)
        if annee_univ:
            registre_qs = registre_qs.filter(annee_universitaire=annee_univ)
        if not registre_qs.exists():
            raise ValueError(
                "Diplôme non délivré au registre pour cet étudiant : génération "
                "refusée (délibération / inscription au registre requise)."
            )

    # Attestation / relevé : refuser si aucune inscription administrative pour l'année.
    if type_document in ('attestation_inscription', 'releve_semestre', 'releve_complet', 'releve_notes'):
        from apps.inscriptions.models import InscriptionAdministrative
        insc_qs = InscriptionAdministrative.objects.filter(etudiant=etudiant)
        if annee_univ:
            insc_qs = insc_qs.filter(annee_univ__annee=annee_univ)
        if not insc_qs.exists():
            annee_txt = f" pour l'année universitaire {annee_univ}" if annee_univ else ""
            raise ValueError(
                f"Aucune inscription trouvée pour cet étudiant{annee_txt} : "
                f"génération refusée (l'étudiant n'est pas inscrit)."
            )

    institution = _get_institution(etudiant)
    if institution is None:
        raise ValueError("Aucune institution trouvee : ni l'etudiant ni l'institution principale ne sont definis.")

    numero  = _next_numero_serie(type_document, institution.pk)
    token   = uuid.uuid4()
    payload = f'{numero}{etudiant.matricule}{type_document}{annee_univ}'
    sha256  = hashlib.sha256(payload.encode()).hexdigest()

    doc = DocumentOfficiel.objects.create(
        etudiant            = etudiant,
        institution         = institution,
        type_document       = type_document,
        numero_serie        = numero,
        token_verification  = token,
        annee_universitaire = annee_univ,
        semestre_id         = semestre_id,
        hash_sha256         = sha256,
        est_valide          = True,
        genere_par          = user,
    )
    logger.info('Document créé: %s (PDF généré à la demande)', doc.numero_serie)
    return doc


def generer_document(data: dict, user) -> dict:
    """
    Crée et persiste un DocumentOfficiel (unitaire). Retourne un dict compatible
    avec DocumentOfficielSerializer. Le PDF est généré à la demande au 1er
    téléchargement (endpoint telecharger/).
    """
    from apps.absence.models import Etudiant
    etudiant = Etudiant.objects.get(pk=data.get('etudiant'))
    doc = _creer_document_officiel(
        etudiant,
        data.get('type_document'),
        data.get('annee_universitaire', ''),
        data.get('semestre'),
        user,
    )
    from .serializers import DocumentOfficielSerializer
    return DocumentOfficielSerializer(doc).data


def generer_documents_groupe(type_document, annee_univ, filiere_id, semestre_id, user):
    """
    Génération GROUPÉE : 1 document officiel par étudiant concerné, FUSIONNÉS en un
    seul PDF. Retourne (pdf_bytes, nb_ok, nb_total, erreurs).

    Sélection des étudiants concernés :
      - relevés (releve_*) : inscrits PÉDAGOGIQUEMENT au semestre (année + filière).
      - attestations       : inscrits ADMINISTRATIVEMENT (année + filière).

    Officiels individuels : chaque étudiant a son DocumentOfficiel (numéro de série
    + QR). RÉUTILISE un doc déjà créé pour (étudiant, type, année, semestre) — pas de
    nouveaux numéros à la ré-exécution. Un étudiant en erreur (sans inscription/IP,
    PDF KO) est ignoré et reporté, sans faire échouer tout le lot.
    """
    from io import BytesIO
    from pypdf import PdfWriter
    from apps.inscriptions.models import InscriptionAdministrative, InscriptionPedagogique

    is_releve  = type_document in ('releve_semestre', 'releve_complet', 'releve_notes')
    is_diplome = type_document in ('diplome', 'attestation_diplome')
    if is_releve and not semestre_id:
        raise ValueError("Le semestre est requis pour un relevé.")

    if is_releve:
        qs = (InscriptionPedagogique.objects
              .filter(inscription_admin__annee_univ__annee=annee_univ,
                      inscription_admin__filiere_id=filiere_id,
                      semestre_id=semestre_id)
              .select_related('inscription_admin__etudiant')
              .order_by('inscription_admin__etudiant__matricule'))
        etudiants = [ip.inscription_admin.etudiant for ip in qs]
    elif is_diplome:
        # Diplôme / attestation de diplôme : SEULS les étudiants effectivement
        # diplômés (présents au registre des diplômes) pour cette année + filière.
        from .models import RegistreDiplome
        qs = (RegistreDiplome.objects
              .filter(annee_universitaire=annee_univ, filiere_id=filiere_id)
              .select_related('etudiant')
              .order_by('etudiant__matricule'))
        etudiants = [r.etudiant for r in qs]
    else:
        qs = (InscriptionAdministrative.objects
              .filter(annee_univ__annee=annee_univ, filiere_id=filiere_id)
              .select_related('etudiant')
              .order_by('etudiant__matricule'))
        etudiants = [ia.etudiant for ia in qs]

    seen, uniques = set(), []
    for e in etudiants:
        if e.id not in seen:
            seen.add(e.id)
            uniques.append(e)
    if not uniques:
        from apps.scolarite.models import Filiere
        from apps.parametres.models import Semestre
        fil = Filiere.objects.filter(pk=filiere_id).first()
        fil_txt = f'« {fil.intitule_fr} »' if fil else f'#{filiere_id}'
        sem_txt = ''
        if semestre_id:
            sem = Semestre.objects.filter(pk=semestre_id).first()
            sem_txt = f' — semestre {sem.code_semestre}' if sem else ''
        raise ValueError(
            f"Aucun étudiant inscrit en {fil_txt}{sem_txt} pour l'année {annee_univ}. "
            f"Vérifiez la filière et le semestre choisis (le semestre doit correspondre "
            f"à une promotion réellement inscrite cette année)."
        )

    writer = PdfWriter()
    data   = {'annee_universitaire': annee_univ, 'semestre': semestre_id}
    nb_ok, erreurs = 0, []
    for etu in uniques:
        try:
            doc = (DocumentOfficiel.objects
                   .filter(etudiant=etu, type_document=type_document,
                           annee_universitaire=annee_univ, semestre_id=semestre_id)
                   .order_by('-id').first()
                   or _creer_document_officiel(etu, type_document, annee_univ, semestre_id, user))
            pdf_bytes = _generer_pdf(doc, etu, data, is_duplicata=False)
            if not pdf_bytes:
                erreurs.append(f'{etu.matricule}: PDF non généré')
                continue
            writer.append(BytesIO(pdf_bytes))
            nb_ok += 1
        except Exception as exc:
            erreurs.append(f'{etu.matricule}: {exc}')

    if nb_ok == 0:
        raise ValueError("Aucun document généré. " + (' | '.join(erreurs[:5]) if erreurs else ''))

    out = BytesIO()
    writer.write(out)
    writer.close()
    return out.getvalue(), nb_ok, len(uniques), erreurs
