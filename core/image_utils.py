"""
Optimisation des images uploadees (photos etudiants/preinscriptions, logos &
signatures institution).

Objectif : reduire drastiquement le poids SANS perte visible.
- Photos -> JPEG progressif q88 (compatible ECRAN **et** wkhtmltopdf/PDF ;
  WebP/AVIF ecartes car mal/pas supportes par le moteur PDF).
- Logos / signatures -> PNG optimise (transparence conservee).

Pipeline : orientation EXIF corrigee (photos telephone couchees) -> conversion
RGB/RGBA -> redimensionnement LANCZOS (JAMAIS d'agrandissement) -> re-encodage ->
metadonnees (EXIF/GPS) purgees.

NON BLOQUANT : toute erreur -> aucune modification (le fichier d'origine est
conserve). L'optimisation n'est gardee QUE si elle reduit reellement le poids.

Points d'entree :
  - optimize_image(django_file, ...)        -> (ContentFile, nom) | (None, None)
  - optimize_field_on_upload(instance, ...)  -> a appeler dans un save() de modele
"""
import io
import logging
import os

from django.core.files.base import ContentFile

logger = logging.getLogger('siga')

PHOTO_MAX_DIM = 1280   # cote long max des photos (px) — net a l'ecran ET en PDF
PHOTO_QUALITY = 88     # qualite JPEG (visuellement sans perte)
LOGO_MAX_DIM  = 512    # cote long max des logos / sceaux / signatures (px)


def optimize_image(django_file, *, max_dim=PHOTO_MAX_DIM, quality=PHOTO_QUALITY,
                   keep_transparency=False):
    """
    Optimise une image et renvoie (ContentFile, nom_de_fichier) prets a stocker,
    ou (None, None) s'il n'y a rien a faire (Pillow absent, erreur, ou aucun gain).

    keep_transparency=False -> JPEG progressif q`quality` (transparence aplatie
    sur blanc). keep_transparency=True -> PNG optimise (alpha conserve).
    """
    try:
        from PIL import Image, ImageOps
    except Exception:
        logger.warning('Pillow indisponible — image non optimisee.')
        return None, None

    # Filtre de reechantillonnage haute qualite (compat toutes versions Pillow).
    resample = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS', 1)

    try:
        django_file.seek(0)
        original = django_file.read()
        if not original:
            return None, None
        img = Image.open(io.BytesIO(original))

        # 1. Corrige l'orientation EXIF (sinon photos de telephone couchees).
        img = ImageOps.exif_transpose(img)

        # 2. Redimensionne au cote long `max_dim` (jamais d'agrandissement).
        img.thumbnail((max_dim, max_dim), resample)

        buf = io.BytesIO()
        if keep_transparency:
            if img.mode not in ('RGBA', 'RGB', 'LA', 'L'):
                img = img.convert('RGBA')
            img.save(buf, format='PNG', optimize=True)
            ext, ctype = '.png', 'image/png'
        else:
            # JPEG : aplatir toute transparence sur fond blanc.
            if img.mode in ('RGBA', 'LA', 'P'):
                rgba = img.convert('RGBA')
                bg = Image.new('RGB', rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[-1])
                img = bg
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            img.save(buf, format='JPEG', quality=quality, optimize=True, progressive=True)
            ext, ctype = '.jpg', 'image/jpeg'

        data = buf.getvalue()
        # 3. Ne garder l'optimisation QUE si elle reduit le poids.
        if len(data) >= len(original):
            return None, None

        base = os.path.splitext(os.path.basename(getattr(django_file, 'name', '') or 'image'))[0]
        cf = ContentFile(data)
        cf.content_type = ctype
        return cf, f'{base}{ext}'
    except Exception:
        logger.warning("Optimisation image echouee — fichier d'origine conserve.", exc_info=True)
        return None, None


def optimize_field_on_upload(instance, field_name, *, keep_transparency=False,
                             max_dim=None, quality=PHOTO_QUALITY):
    """
    A appeler dans le save() d'un modele, AVANT super().save() : optimise le champ
    image `field_name` UNIQUEMENT s'il vient d'etre uploade (pas encore commit en
    stockage). Remplace le contenu par la version optimisee sans re-declencher de
    save (save=False). No-op si le champ est vide ou deja stocke.
    """
    field = getattr(instance, field_name, None)
    if not field or getattr(field, '_committed', True):
        return
    md = max_dim if max_dim is not None else (LOGO_MAX_DIM if keep_transparency else PHOTO_MAX_DIM)
    cf, name = optimize_image(field, max_dim=md, quality=quality,
                              keep_transparency=keep_transparency)
    if cf:
        field.save(name, cf, save=False)
