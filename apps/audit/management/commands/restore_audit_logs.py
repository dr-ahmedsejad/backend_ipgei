"""
Restaure les fichiers audit_YYYY-MM.jsonl.gz dans AuditLogArchive.

Use case : recherche forensique sur evenement vieux > 1 an.
Apres consultation, le mois peut etre re-purge avec purge_audit_logs.

Usage :
  python manage.py restore_audit_logs --month 2024-08
  python manage.py restore_audit_logs --month 2024-08 --dry-run
  python manage.py restore_audit_logs --file /path/to/file.jsonl.gz
"""
import gzip
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import AuditLogArchive


COPY_FIELDS = (
    'user_id', 'action', 'model_name', 'object_id', 'changes',
    'ip_address', 'user_agent', 'timestamp',
    'institution_id', 'request_id', 'label', 'endpoint', 'http_method',
    'keep_forever',
)


class Command(BaseCommand):
    help = 'Restaure un fichier audit_YYYY-MM.jsonl.gz dans AuditLogArchive.'

    def add_arguments(self, parser):
        g = parser.add_mutually_exclusive_group(required=True)
        g.add_argument('--month', type=str,
                       help='YYYY-MM (cherche dans EXPORT_PATH/audit_YYYY-MM.jsonl.gz)')
        g.add_argument('--file', type=str, help='Chemin direct vers un .jsonl.gz')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--batch', type=int, default=2000)

    def handle(self, *args, **opts):
        cfg = settings.AUDIT_RETENTION
        if opts['month']:
            month = opts['month']
            try:
                # Validation simple
                int(month[:4]); int(month[5:7])
                assert month[4] == '-'
            except Exception:
                raise CommandError(f'Format invalide (attendu YYYY-MM) : {month}')
            path = Path(cfg.get('EXPORT_PATH')) / f'audit_{month}.jsonl.gz'
        else:
            path = Path(opts['file'])

        if not path.exists():
            raise CommandError(f'Fichier introuvable : {path}')

        self.stdout.write(self.style.NOTICE(f'Restore depuis : {path}'))

        rows: list[dict] = []
        with gzip.open(path, 'rb') as gz:
            for line in gz:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))

        self.stdout.write(f'  {len(rows)} entrees lues')
        if not rows:
            return
        if opts['dry_run']:
            self.stdout.write(self.style.WARNING('--dry-run : pas d\'INSERT'))
            return

        batch = max(100, opts['batch'])
        inserted = 0
        ts_field = AuditLogArchive._meta.get_field('timestamp')
        prev_auto = ts_field.auto_now_add
        ts_field.auto_now_add = False
        try:
            with transaction.atomic():
                buffer = []
                for r in rows:
                    kw = {k: r.get(k) for k in COPY_FIELDS}
                    obj = AuditLogArchive(**kw)
                    buffer.append(obj)
                    if len(buffer) >= batch:
                        AuditLogArchive.objects.bulk_create(buffer, batch_size=batch)
                        inserted += len(buffer)
                        buffer = []
                if buffer:
                    AuditLogArchive.objects.bulk_create(buffer, batch_size=batch)
                    inserted += len(buffer)
        finally:
            ts_field.auto_now_add = prev_auto

        self.stdout.write(self.style.SUCCESS(
            f'Restore termine : {inserted} entrees ajoutees a core_audit_log_archive.'
        ))
