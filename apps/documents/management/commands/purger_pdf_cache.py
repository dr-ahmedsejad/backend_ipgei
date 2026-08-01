"""
Vide les PDFs en cache des DocumentOfficiel pour forcer la regeneration
au prochain telechargement.

Utile apres un changement de logique de calcul (ex: passage a ROUND_HALF_UP)
ou de template, pour que les PDFs deja generes soient rafraichis.

Usage :
    python manage.py purger_pdf_cache --dry-run                     # voit ce qui serait fait
    python manage.py purger_pdf_cache --apply                       # tous les documents
    python manage.py purger_pdf_cache --doc 12 --apply              # un document precis
    python manage.py purger_pdf_cache --etudiant 7 --apply          # tous les docs d'un etudiant
    python manage.py purger_pdf_cache --type releve_semestre --apply  # par type
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Vide les PDFs en cache des DocumentOfficiel."

    def add_arguments(self, parser):
        parser.add_argument('--doc', type=int, default=None, help="ID d'un document precis.")
        parser.add_argument('--etudiant', type=int, default=None, help="ID d'un etudiant (tous ses documents).")
        parser.add_argument('--type', type=str, default=None,
                            help="Type de document (ex: releve_semestre, attestation_inscription).")
        parser.add_argument('--dry-run', action='store_true', default=False)
        parser.add_argument('--apply', action='store_true', default=False)

    def handle(self, *args, **opts):
        from apps.documents.models import DocumentOfficiel

        if not opts['apply'] and not opts['dry_run']:
            opts['dry_run'] = True

        qs = DocumentOfficiel.objects.exclude(fichier_pdf='').exclude(fichier_pdf__isnull=True)
        if opts['doc']:
            qs = qs.filter(pk=opts['doc'])
        if opts['etudiant']:
            qs = qs.filter(etudiant_id=opts['etudiant'])
        if opts['type']:
            qs = qs.filter(type_document=opts['type'])

        nb = qs.count()
        mode = "DRY-RUN" if opts['dry_run'] else "APPLY"
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== PURGE PDF CACHE [{mode}] : {nb} document(s) ==="
        ))

        for doc in qs:
            self.stdout.write(
                f"  #{doc.id} {doc.numero_serie} ({doc.type_document}) "
                f"-> {doc.fichier_pdf.name if doc.fichier_pdf else '(vide)'}"
            )
            if not opts['dry_run']:
                try:
                    doc.fichier_pdf.delete(save=False)
                except Exception as exc:
                    self.stdout.write(self.style.WARNING(f"    (erreur fichier : {exc})"))
                doc.fichier_pdf = None
                doc.save(update_fields=['fichier_pdf'])

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING(
                f"\n[DRY-RUN] Aucun PDF supprime. Relancer avec --apply."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"\n{nb} PDFs supprimes. Ils seront regeneres au prochain telechargement."
            ))
