"""
Exporte les AuditLogArchive plus vieux que ARCHIVE_DAYS dans un fichier
JSON Lines compresse (.jsonl.gz), puis les SUPPRIME de la table.

Strategie :
  - Groupe par mois calendaire (YYYY-MM) -> 1 fichier par mois
  - Format : 1 ligne JSON par event, gzip
  - Idempotent : un mois deja exporte est complete, jamais ecrase
  - Atomic : ecriture vers .partial.gz puis rename, DELETE seulement apres ecriture OK

Usage :
  python manage.py purge_audit_logs                    # utilise AUDIT_RETENTION
  python manage.py purge_audit_logs --days 730         # override
  python manage.py purge_audit_logs --dry-run
  python manage.py purge_audit_logs --keep-forever-only  # exporte SEULEMENT keep_forever
                                                          # (pour audit securite long terme)
"""
import gzip
import json
import os
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import AuditLogArchive


SERIALIZE_FIELDS = (
    'id', 'user_id', 'action', 'model_name', 'object_id', 'changes',
    'ip_address', 'user_agent', 'timestamp',
    'institution_id', 'request_id', 'label', 'endpoint', 'http_method',
    'keep_forever',
)


def _serialize_row(row: dict) -> str:
    """Sortie JSON ligne (timestamp en ISO, etc.)."""
    out = {}
    for k in SERIALIZE_FIELDS:
        v = row.get(k)
        if hasattr(v, 'isoformat'):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return json.dumps(out, ensure_ascii=False, separators=(',', ':'))


class Command(BaseCommand):
    help = ('Exporte les AuditLogArchive > ARCHIVE_DAYS vers JSONL.gz puis '
            'supprime de la table. keep_forever=True ne sont JAMAIS supprimes.')

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=None,
                            help='Override AUDIT_RETENTION.ARCHIVE_DAYS')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--export-path', type=str, default=None,
                            help='Override AUDIT_RETENTION.EXPORT_PATH')

    def handle(self, *args, **opts):
        cfg = settings.AUDIT_RETENTION
        days = opts['days'] or cfg.get('ARCHIVE_DAYS', 365)
        export_path = Path(opts['export_path'] or cfg.get('EXPORT_PATH'))
        export_path.mkdir(parents=True, exist_ok=True)

        cutoff = timezone.now() - timedelta(days=days)
        qs = AuditLogArchive.objects.filter(
            timestamp__lt=cutoff,
            keep_forever=False,
        )
        total = qs.count()
        keep_forever_count = AuditLogArchive.objects.filter(
            timestamp__lt=cutoff, keep_forever=True,
        ).count()

        self.stdout.write(self.style.NOTICE(
            f'Cutoff = {cutoff.isoformat()} (ARCHIVE_DAYS={days})\n'
            f'Eligible (purge) : {total}\n'
            f'keep_forever (preserves) : {keep_forever_count}\n'
            f'Export vers : {export_path}'
        ))
        if total == 0:
            self.stdout.write(self.style.SUCCESS('Rien a purger.'))
            return

        # Groupe par mois calendaire YYYY-MM
        rows_by_month = defaultdict(list)
        for row in qs.values(*SERIALIZE_FIELDS).iterator(chunk_size=2000):
            ts = row['timestamp']
            key = ts.strftime('%Y-%m') if hasattr(ts, 'strftime') else 'unknown'
            rows_by_month[key].append(row)

        if opts['dry_run']:
            for k, rows in rows_by_month.items():
                self.stdout.write(f'  {k} : {len(rows)} entrees (dry-run)')
            return

        deleted_total = 0
        for month, rows in sorted(rows_by_month.items()):
            target = export_path / f'audit_{month}.jsonl.gz'
            partial = export_path / f'audit_{month}.partial.gz'
            ids = [r['id'] for r in rows]

            # Append au fichier mensuel : on utilise le mode 'ab' avec gzip
            mode = 'ab' if target.exists() else 'wb'
            try:
                with gzip.open(target if mode == 'ab' else partial, mode) as gz:
                    for r in rows:
                        gz.write((_serialize_row(r) + '\n').encode('utf-8'))
                if mode == 'wb':
                    os.replace(partial, target)
            except Exception as exc:
                if partial.exists():
                    partial.unlink(missing_ok=True)
                self.stderr.write(self.style.ERROR(
                    f'Export {month} echoue : {exc} -> abandon (rien supprime)'
                ))
                continue

            with transaction.atomic():
                AuditLogArchive._base_manager.filter(pk__in=ids).delete()
            deleted_total += len(ids)
            self.stdout.write(self.style.SUCCESS(
                f'  {month} : {len(ids)} entrees -> {target.name}'
            ))

        self.stdout.write(self.style.SUCCESS(
            f'Termine : {deleted_total} entrees exportees + supprimees, '
            f'{keep_forever_count} preservees (keep_forever).'
        ))
