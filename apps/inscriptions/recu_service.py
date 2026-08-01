"""
Génération du REÇU DE PAIEMENT des frais d'inscription (PDF demi-A4 210×148 mm).

Design identique au reçu de SIGA privé (module finances) : logo institution centré,
filet vert, tableau d'information, bandeau vert « Montant réglé », montant en toutes
lettres, « Encaissé par » + « Signature & cachet ». Pas de QR, pas d'arabe.

Réutilise l'infrastructure PDF partagée (core.pdf_renderer.render_pdf_bytes) et le
logo institution encodé base64 (apps.documents.services._get_logo_b64).
"""
import logging
from decimal import Decimal

from django.utils import timezone

from apps.documents.services import _get_logo_b64, NIVEAU_LABELS

logger = logging.getLogger('siga')

TEMPLATE = 'inscriptions/recu_frais.html'


# ── Montant en toutes lettres (français) — 0 .. 999 999 999 ───────────────────
_UNITS = ['zéro', 'un', 'deux', 'trois', 'quatre', 'cinq', 'six', 'sept', 'huit',
          'neuf', 'dix', 'onze', 'douze', 'treize', 'quatorze', 'quinze', 'seize']
_TENS = {20: 'vingt', 30: 'trente', 40: 'quarante', 50: 'cinquante',
         60: 'soixante', 80: 'quatre-vingt'}


def _below_100(n: int) -> str:
    if n < 17:
        return _UNITS[n]
    if n < 20:
        return 'dix-' + _UNITS[n - 10]
    if n < 100:
        d, u = divmod(n, 10)
        if d in (7, 9):                       # 70-79, 90-99 → soixante-/quatre-vingt-
            base = _TENS[60 if d == 7 else 80]
            return base + '-' + _below_100(n - (60 if d == 7 else 80))
        base = _TENS[d * 10]
        if u == 0:
            return base + ('s' if n == 80 else '')
        if u == 1 and d in (2, 3, 4, 5, 6):
            return base + '-et-un'
        return base + '-' + _UNITS[u]
    return str(n)


def _below_1000(n: int) -> str:
    if n < 100:
        return _below_100(n)
    c, r = divmod(n, 100)
    cent = 'cent' if c == 1 else _UNITS[c] + ' cent'
    if r == 0:
        return cent + ('s' if c > 1 else '')
    return cent + ' ' + _below_100(r)


def montant_en_lettres(montant) -> str:
    """Convertit la partie entière d'un montant en toutes lettres FR."""
    try:
        n = int(Decimal(str(montant)))
    except Exception:
        return ''
    if n == 0:
        return 'zéro'
    parts = []
    millions, reste = divmod(n, 1_000_000)
    milliers, unites = divmod(reste, 1_000)
    if millions:
        parts.append('un million' if millions == 1 else _below_1000(millions) + ' millions')
    if milliers:
        parts.append('mille' if milliers == 1 else _below_1000(milliers) + ' mille')
    if unites:
        parts.append(_below_1000(unites))
    return ' '.join(parts)


def _fmt_montant(montant) -> str:
    """15000 → '15 000' ; 15000.50 → '15 000,50'."""
    try:
        val = Decimal(str(montant or 0))
    except Exception:
        return '0'
    entier = int(val)
    cents = val - entier
    s = f'{entier:,}'.replace(',', ' ')
    if cents:
        s += ',' + f'{cents:.2f}'.split('.')[1]
    return s


def build_recu_context(insc, agent: str = '') -> dict:
    """Contexte du template à partir d'une InscriptionAdministrative payée."""
    etu     = insc.etudiant
    inst    = insc.institution
    filiere = insc.filiere

    prenom = (getattr(etu, 'prenom_fr', '') or '').strip()
    nom    = (getattr(etu, 'nom_fr', '') or getattr(etu, 'nom', '') or '').strip()
    nom_complet = f'{prenom} {nom}'.strip() or getattr(etu, 'nom', '') or '—'

    niveau = getattr(insc, 'niveau', None)
    niveau_label = f'Niveau {niveau}' if niveau else ''
    fil_nom = (getattr(filiere, 'intitule_fr', '') or getattr(filiere, 'code', '') or '—').strip()
    fil_niv = ' · '.join([x for x in (fil_nom, niveau_label) if x])

    annee = getattr(getattr(insc, 'annee_univ', None), 'annee', '') or ''
    objet = "Frais d'inscription" + (f' — {annee}' if annee else '')

    coords = ' · '.join([x for x in (
        (getattr(inst, 'ville_fr', '') or '').strip() if inst else '',
        ('Tél. ' + inst.telephone) if inst and getattr(inst, 'telephone', '') else '',
        (getattr(inst, 'email', '') or '').strip() if inst else '',
    ) if x])

    montant = insc.montant_frais or Decimal('0')

    return {
        'logo_b64':        _get_logo_b64(inst),
        'inst_acronyme':   (getattr(inst, 'acronyme', '') or 'SIGA').strip(),
        'coords':          coords,
        'numero_recu':     insc.recu_paiement or '—',
        'date_paiement':   insc.date_paiement.strftime('%d/%m/%Y') if insc.date_paiement else '',
        'mode':            'Espèces',
        'etudiant_nom':    nom_complet,
        'matricule':       (getattr(etu, 'matricule', '') or '').strip(),
        'fil_niv':         fil_niv,
        'objet':           objet,
        'montant_fmt':     _fmt_montant(montant),
        'montant_lettres': montant_en_lettres(montant),
        'agent':           agent or '—',
    }


def generer_recu_pdf(insc, agent: str = '') -> bytes:
    """Rend le reçu de paiement en PDF (bytes) — demi-A4 210×148 mm."""
    from core.pdf_renderer import render_pdf_bytes
    context = build_recu_context(insc, agent)
    options = {
        'encoding': 'UTF-8',
        'enable-local-file-access': None,
        'load-error-handling': 'ignore',
        'load-media-error-handling': 'ignore',
        'dpi': 150,
        # Demi-feuille A4 pré-coupée : une feuille = un reçu, pas de découpe.
        'page-width': '210mm', 'page-height': '148mm',
        'margin-top': '0mm', 'margin-bottom': '0mm',
        'margin-left': '0mm', 'margin-right': '0mm',
    }
    return render_pdf_bytes(TEMPLATE, context, options)
