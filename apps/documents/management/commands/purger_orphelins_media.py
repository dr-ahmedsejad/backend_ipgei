"""Supprime les PDF ORPHELINS de media/documents/officiels/.

Un orphelin = un fichier present sur le disque mais reference par AUCUN
DocumentOfficiel.fichier_pdf en base. Ils s'accumulent via :
  - les regenerations (Django ne supprime pas l'ancien fichier -> suffixe _xxxxxxx) ;
  - les suppressions de lignes en base (FileField ne supprime pas le fichier).

Contrairement a `purger_pdf_cache` (qui vide le cache des lignes EXISTANTES),
cette commande recupere l'espace des fichiers SANS ligne associee.

Usage :
    python manage.py purger_orphelins_media               # dry-run (defaut)
    python manage.py purger_orphelins_media --apply        # supprime reellement
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand


SOUS_DOSSIER = os.path.join('documents', 'officiels')


class Command(BaseCommand):
    help = "Supprime les PDF orphelins de media/documents/officiels/ (non references en base)."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help="Liste sans supprimer (defaut si --apply absent).")
        parser.add_argument('--apply', action='store_true', default=False,
                            help="Supprime reellement les orphelins.")

    def handle(self, *args, **opts):
        from apps.documents.models import DocumentOfficiel

        dry = not opts['apply']

        base_dir = os.path.join(settings.MEDIA_ROOT, SOUS_DOSSIER)
        if not os.path.isdir(base_dir):
            self.stdout.write(f"Dossier inexistant : {base_dir} (rien a faire).")
            return

        # Fichiers references en base -> chemins absolus normalises.
        referenced = (
            DocumentOfficiel.objects
            .exclude(fichier_pdf='').exclude(fichier_pdf__isnull=True)
            .values_list('fichier_pdf', flat=True)
        )
        referenced_abs = {
            os.path.normpath(os.path.join(settings.MEDIA_ROOT, r)) for r in referenced
        }

        orphans = []
        total = 0
        for root, _dirs, files in os.walk(base_dir):
            for name in files:
                path = os.path.normpath(os.path.join(root, name))
                if path not in referenced_abs:
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        size = 0
                    orphans.append((path, size))
                    total += size

        mode = "DRY-RUN" if dry else "APPLY"
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== PURGE ORPHELINS media/{SOUS_DOSSIER} [{mode}] : "
            f"{len(orphans)} fichier(s), {total / 1024 / 1024:.1f} Mo ==="
        ))
        for path, size in orphans:
            self.stdout.write(f"  {os.path.basename(path)} ({size / 1024:.0f} Ko)")
            if not dry:
                try:
                    os.remove(path)
                except OSError as exc:
                    self.stdout.write(self.style.WARNING(f"    (erreur : {exc})"))

        if dry:
            self.stdout.write(self.style.WARNING(
                "\nDRY-RUN : rien supprime. Relancer avec --apply pour purger."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"\n{len(orphans)} orphelin(s) supprime(s) "
                f"({total / 1024 / 1024:.1f} Mo recuperes)."
            ))
