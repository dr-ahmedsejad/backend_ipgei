import os

from django.core.exceptions import ValidationError

# Validateur partagé pour les documents joints (PJ préinscription, justificatif
# d'absence, CV/diplôme prof). Autorise PDF en plus des images, contrairement à
# `authentication.models.validate_avatar` qui est image-only.
ALLOWED_DOC_EXTENSIONS    = {'.pdf', '.jpg', '.jpeg', '.png'}
ALLOWED_DOC_CONTENT_TYPES = {'application/pdf', 'image/jpeg', 'image/png'}
MAX_DOC_SIZE_MB           = 5


def validate_document(value):
    """Valide un document joint : taille <= 5 Mo, extension ET Content-Type
    sur whitelist PDF/JPEG/PNG. À brancher comme `validators=[validate_document]`
    sur tout FileField/ImageField acceptant un fichier utilisateur."""
    if value.size > MAX_DOC_SIZE_MB * 1024 * 1024:
        mb = value.size // (1024 * 1024)
        raise ValidationError(
            f"Le fichier ne peut pas dépasser {MAX_DOC_SIZE_MB} Mo (reçu : {mb} Mo)."
        )

    ext = os.path.splitext(value.name)[1].lower()
    if ext not in ALLOWED_DOC_EXTENSIONS:
        raise ValidationError(
            "Format non supporté. Seuls les fichiers PDF, JPEG et PNG sont acceptés."
        )

    content_type = getattr(value, 'content_type', None)
    if content_type and content_type not in ALLOWED_DOC_CONTENT_TYPES:
        raise ValidationError("Type de fichier non autorisé.")
