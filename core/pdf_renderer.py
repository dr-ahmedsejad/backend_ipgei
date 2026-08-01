"""Rendu PDF partagé (pdfkit/wkhtmltopdf).

Déduplique les fonctions `_render_pdf` recopiées dans plusieurs apps. La partie
réellement commune — import pdfkit, configuration du binaire, appel from_string —
vit dans le kernel `render_pdf_bytes`. Chaque appelant garde ses options et son
enveloppe (HttpResponse / bytes), car elles diffèrent légitimement.

- `render_pdf_bytes`    : template -> bytes (lève en cas d'erreur). Kernel commun.
- `render_pdf_response` : enveloppe HttpResponse + contexte institution (avancement/vacation).
"""
import logging
from datetime import date

from django.conf import settings
from django.http import HttpResponse
from django.template.loader import get_template
from rest_framework.response import Response

logger = logging.getLogger('siga')

# Chemin du binaire wkhtmltopdf. Historiquement codé en dur (Windows) dans chaque
# copie ; centralisé ici et surchargeable via settings (défaut = ancienne valeur).
WKHTMLTOPDF_PATH = getattr(
    settings, 'WKHTMLTOPDF_PATH', r'C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe',
)


def render_pdf_bytes(template_name, context, options):
    """Kernel : rend un template Django en bytes PDF via pdfkit.

    Lève (pdfkit.from_string) en cas d'erreur — l'appelant décide quoi en faire.
    `options` est le dict d'options wkhtmltopdf propre à l'appelant.
    """
    import pdfkit
    from core.arabe import normaliser_arabe
    # Normalise l'arabe (variantes Farsi/Ourdou -> standard) AVANT wkhtmltopdf :
    # sinon un caractere sans glyphe dans la police serveur (Arial Docker) s'affiche
    # en tofu (□). Non destructif ; n'affecte que des codepoints U+06xx (jamais le
    # HTML/CSS/base64). Vaut pour TOUS les PDF (documents, avancement, vacation...).
    html_string = normaliser_arabe(get_template(template_name).render(context))
    config = pdfkit.configuration(wkhtmltopdf=WKHTMLTOPDF_PATH)
    return pdfkit.from_string(html_string, False, configuration=config, options=options)


def render_pdf_response(template_name, context, filename, orientation='Portrait'):
    """Rend un template -> PDF -> HttpResponse (attachment).

    Injecte le contexte institution + la date d'impression. Renvoie 500 si pdfkit
    est absent ou si la génération échoue. (Comportement avancement/vacation.)
    """
    try:
        import pdfkit  # noqa: F401 — détecte l'absence avant le rendu
    except ImportError:
        return Response({'error': 'pdfkit non installé.'}, status=500)

    from core.pdf_utils import get_institution_context
    context.update(get_institution_context())
    context['date_impression'] = date.today().strftime('%d/%m/%Y')

    options = {
        'footer-center':            'Page [page] / [toPage]',
        'margin-top':               '0.50in',
        'margin-right':             '0.50in',
        'margin-bottom':            '0.75in',
        'margin-left':              '0.50in',
        'orientation':              orientation,
        'enable-local-file-access': '',
        'encoding':                 'UTF-8',
    }
    try:
        pdf_bytes = render_pdf_bytes(template_name, context, options)
    except Exception as exc:
        logger.error('pdfkit error: %s', exc)
        return Response({'error': f'Erreur génération PDF : {exc}'}, status=500)

    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response
