"""
Commande : supprime les backups manuels chiffres plus vieux que la retention.

Usage :
  python manage.py cleanup_manual_backups
  python manage.py cleanup_manual_backups --days 14

A executer en cron (ex: chaque nuit) :
  0 3 * * * cd /path/to/siga && python manage.py cleanup_manual_backups
"""
from django.core.management.base import BaseCommand

from apps.backup.services.generator import cleanup_old_manual_backups


class Command(BaseCommand):
    help = 'Supprime les backups manuels chiffres au-dela de la periode de retention.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int,
            help='Override BACKUP_MANUAL_RETENTION_DAYS (settings).',
        )

    def handle(self, *args, **opts):
        deleted = cleanup_old_manual_backups(retention_days=opts.get('days'))
        self.stdout.write(
            self.style.SUCCESS(f'Cleanup termine : {deleted} fichier(s) supprime(s).')
        )
