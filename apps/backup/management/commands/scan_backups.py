"""
Commande : detecte les nouveaux fichiers de backup et synchronise leur
presence dans BackupArtifact.

Usage :
  python manage.py scan_backups
  python manage.py scan_backups --base-dir /home/backups

A executer en cron (ex: chaque heure) :
  0 * * * * cd /path/to/siga && python manage.py scan_backups
"""
from django.core.management.base import BaseCommand

from apps.backup.services.scanner import scan_backup_directories


class Command(BaseCommand):
    help = 'Synchronise BackupArtifact avec les fichiers presents sur disque.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--base-dir',
            help='Override BACKUP_BASE_DIR (settings) pour ce run.',
        )

    def handle(self, *args, **opts):
        report = scan_backup_directories(base_dir=opts.get('base_dir'))
        self.stdout.write(self.style.SUCCESS(f'Scan termine : {report}'))
