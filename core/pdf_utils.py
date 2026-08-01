"""Utilitaires PDF partagés — logo et contexte institution pour les templates."""
import pdfkit
from django.conf import settings


WKHTMLTOPDF = r'C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe'

PDF_OPTIONS = {
    'page-size':        'A4',
    'orientation':      'Portrait',
    'margin-top':       '1.5cm',
    'margin-bottom':    '1.5cm',
    'margin-left':      '1.2cm',
    'margin-right':     '1.2cm',
    'encoding':         'UTF-8',
    'enable-local-file-access': '',
}


def elision_de(nom: str) -> str:
    """
    Élision française du « de » devant un nom d'institution/sigle.

    - Commence par une voyelle  → « de l'X »  (ex. « de l'ISS », « de l'UNA »)
    - Commence par une consonne  → « de X »    (ex. « de CDD SUP »)

    On n'élide que sur les voyelles (pas le « h », ambigu muet/aspiré) pour éviter
    les faux positifs (« de Hassan » plutôt que « de l'Hassan »).
    """
    d = (nom or '').strip()
    if not d:
        return 'de'
    if d[0].lower() in 'aeiouyàâäéèêëîïôöûü':
        return f'de l’{d}'   # apostrophe typographique (’), comme « de l' »
    return f'de {d}'


def get_institution_context() -> dict:
    """
    Charge l'institution principale et retourne un dict de contexte
    prêt à être passé à render_to_string().
    Compatible avec tous les templates qui incluent suivi_en_tete.html.
    """
    fallback_path = settings.BASE_DIR / 'static' / 'assets' / 'img' / 'logo_iss.png'
    fallback_url  = 'file:///' + str(fallback_path).replace('\\', '/')

    try:
        from apps.parametres.models import Institution
        inst = Institution.objects.filter(est_principale=True).first()
        if inst and inst.logo:
            logo_url = 'file:///' + str(
                settings.BASE_DIR / 'media' / str(inst.logo)
            ).replace('\\', '/')
        else:
            logo_url = fallback_url

        return {
            'image_url':               logo_url,          # compatibilité anciens templates
            'inst_logo_url':           logo_url,
            'inst_groupe_fr':          inst.groupe_fr         if inst else '',
            'inst_groupe_ar':          inst.groupe_ar         if inst else '',
            'inst_nom_fr':             inst.nom_fr            if inst else 'Institut Supérieur de la Statistique',
            'inst_nom_ar':             inst.nom_ar            if inst else 'المعهد العالي للإحصاء',
            'inst_nom_complet_fr':     inst.nom_complet_fr    if inst else '',
            'inst_nom_complet_ar':     inst.nom_complet_ar    if inst else '',
            'inst_sigle':              inst.acronyme          if inst else '',
            # Forme élidée « de l'X » / « de X » — le template n'a plus « de l' » en dur.
            'inst_de_sigle':           elision_de((inst.acronyme or inst.nom_fr) if inst
                                                   else 'Institut Supérieur de la Statistique'),
            'inst_directeur_fr':       inst.directeur_nom_fr    if inst else '',
            'inst_directeur_titre_fr': inst.directeur_titre_fr  if inst else '',
            'inst_directeur_ar':       inst.directeur_nom_ar    if inst else '',
        }
    except Exception:
        return {
            'image_url':               fallback_url,
            'inst_logo_url':           fallback_url,
            'inst_groupe_fr':          '',
            'inst_groupe_ar':          '',
            'inst_nom_fr':             'Institut Supérieur de la Statistique',
            'inst_nom_ar':             'المعهد العالي للإحصاء',
            'inst_nom_complet_fr':     '',
            'inst_nom_complet_ar':     '',
            'inst_sigle':              '',
            'inst_de_sigle':           elision_de('Institut Supérieur de la Statistique'),
            'inst_directeur_fr':       '',
            'inst_directeur_titre_fr': '',
            'inst_directeur_ar':       '',
        }


def html_to_pdf(html: str, landscape: bool = False) -> bytes:
    """Convertit une chaîne HTML en PDF via pdfkit/wkhtmltopdf."""
    config  = pdfkit.configuration(wkhtmltopdf=WKHTMLTOPDF)
    options = dict(PDF_OPTIONS)
    if landscape:
        options['orientation'] = 'Landscape'
    return pdfkit.from_string(html, False, configuration=config, options=options)
