"""
Migre les AuditLog HOT (> HOT_DAYS jours) vers AuditLogArchive.

Strategie :
  - Copie par lots (CHUNK_SIZE) : pas de monstre transaction
  - INSERT en bulk_create + DELETE par PK liste pour eviter les races
  - Idempotent : recommencer apres une coupure ne duplique rien (les PKs deja
    archives sont supprimees du HOT a la meme volee)

Usage :
  python manage.py archive_audit_logs              # exec normale (utilise AUDIT_RETENTION)
  python manage.py archive_audit_logs --days 60    # override explicite
  python manage.py archive_audit_logs --dry-run    # ne touche rien
  python manage.py archive_audit_logs --batch 5000 # taille du batch
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import AuditLog, AuditLogArchive


CHUNK_DEFAULT = 2000

COPY_FIELDS = (
    'user_id', 'action', 'model_name', 'object_id', 'changes',
    'ip_address', 'user_agent', 'timestamp',
    'institution_id', 'request_id', 'label', 'endpoint', 'http_method',
    'keep_forever',
)


class Command(BaseCommand):
    help = 'Deplace les AuditLog plus vieux que HOT_DAYS vers AuditLogArchive.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=None,
                            help='Override AUDIT_RETENTION.HOT_DAYS')
        parser.add_argument('--batch', type=int, default=CHUNK_DEFAULT,
                            help=f'Taille du lot (defaut {CHUNK_DEFAULT})')
        parser.add_argument('--dry-run', action='store_true',
                            help='Ne fait rien, affiche juste le compteur')

    def handle(self, *args, **opts):
        days = opts['days'] or settings.AUDIT_RETENTION.get('HOT_DAYS', 90)
        batch = max(100, opts['batch'])
        cutoff = timezone.now() - timedelta(days=days)

        qs_total = AuditLog.objects.filter(timestamp__lt=cutoff)
        total = qs_total.count()
        self.stdout.write(self.style.NOTICE(
            f'Cutoff = {cutoff.isoformat()} (HOT_DAYS={days})\n'
            f'Total a archiver : {total}'
        ))
        if total == 0:
            self.stdout.write(self.style.SUCCESS('Rien a faire.'))
            return

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING('--dry-run : aucune modification.'))
            return

        # Desactive auto_now_add le temps de la copie pour preserver
        # le timestamp original de chaque event.
        ts_field = AuditLogArchive._meta.get_field('timestamp')
        prev_auto = ts_field.auto_now_add
        ts_field.auto_now_add = False

        archived = 0
        try:
            while True:
                chunk = list(
                    AuditLog.objects.filter(timestamp__lt=cutoff)
                                    .values('id', *COPY_FIELDS)[:batch]
                )
                if not chunk:
                    break
                with transaction.atomic():
                    AuditLogArchive.objects.bulk_create(
                        [AuditLogArchive(**{k: row[k] for k in COPY_FIELDS}) for row in chunk],
                        batch_size=batch,
                    )
                    ids = [row['id'] for row in chunk]
                    AuditLog._base_manager.filter(pk__in=ids).delete()
                archived += len(chunk)
                self.stdout.write(f'  ... {archived}/{total}')
                if len(chunk) < batch:
                    break
        finally:
            ts_field.auto_now_add = prev_auto

        self.stdout.write(self.style.SUCCESS(
            f'Termine : {archived} entrees deplacees vers core_audit_log_archive'
        ))
