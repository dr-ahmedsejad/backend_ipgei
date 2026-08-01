"""
Signature numérique PAdES des PDF officiels (anti-falsification).

Source unique : `sign_pdf_bytes(pdf_bytes)`. Appelée à la fin de `_render_pdf`,
elle signe le PDF avec le certificat PKCS#12 configuré (pyHanko).

Garantie NON BLOQUANTE : toute défaillance (signature désactivée, certificat
absent, pyHanko indisponible, erreur de signature) renvoie le PDF *non signé*
et logge — la génération de document n'échoue JAMAIS à cause de la signature.
(Le certificat est aujourd'hui auto-signé : la signature prouve l'INTÉGRITÉ ;
l'identité « de confiance » viendra avec un certificat d'AC officielle.)
"""
import logging
import os
import threading
from io import BytesIO

from django.conf import settings

logger = logging.getLogger('siga')

_signer = None          # cache du signer chargé (réutilisable entre requêtes)
_lock = threading.Lock()


def _get_signer():
    """Charge (et met en cache) le signer pyHanko depuis le PKCS#12 configuré.
    Renvoie None si la signature est désactivée ou le certificat introuvable —
    sans mise en cache de l'échec, pour reprendre dès que le certificat existe."""
    global _signer
    if _signer is not None:
        return _signer
    if not getattr(settings, 'PDF_SIGNING_ENABLED', False):
        return None
    path = getattr(settings, 'PDF_SIGN_PKCS12_PATH', '') or ''
    if not path or not os.path.exists(path):
        return None
    with _lock:
        if _signer is not None:
            return _signer
        try:
            from pyhanko.sign import signers
            pwd = (getattr(settings, 'PDF_SIGN_PKCS12_PASSWORD', '') or '').encode() or None
            _signer = signers.SimpleSigner.load_pkcs12(pfx_file=path, passphrase=pwd)
            if _signer is None:
                logger.warning('Signature PDF : chargement du certificat échoué (%s).', path)
            else:
                logger.info('Signature PDF : certificat chargé (%s).', path)
        except Exception:
            logger.warning('Signature PDF : init du signer impossible — PDF non signés.', exc_info=True)
            _signer = None
    return _signer


def sign_pdf_bytes(pdf_bytes: bytes) -> bytes:
    """Signe les octets PDF (PAdES) et les renvoie signés. En cas d'indisponibilité
    ou d'erreur, renvoie les octets D'ORIGINE (non bloquant)."""
    if not pdf_bytes:
        return pdf_bytes
    signer = _get_signer()
    if signer is None:
        return pdf_bytes
    try:
        from pyhanko.sign import signers
        from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
        writer = IncrementalPdfFileWriter(BytesIO(pdf_bytes))
        meta = signers.PdfSignatureMetadata(
            field_name='SignatureETAB',
            reason=getattr(settings, 'PDF_SIGN_REASON', 'Document officiel'),
            location=getattr(settings, 'PDF_SIGN_LOCATION', ''),
        )
        out = signers.sign_pdf(writer, meta, signer=signer)
        return out.getvalue()
    except Exception:
        logger.warning('Signature PDF : signature échouée — PDF renvoyé non signé.', exc_info=True)
        return pdf_bytes


def signature_active() -> bool:
    """Indique si la signature est effectivement opérationnelle (diagnostic)."""
    return _get_signer() is not None
