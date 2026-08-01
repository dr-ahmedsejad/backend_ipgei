"""
Scanner : detecte les nouveaux fichiers de backup sur disque et cree des
BackupArtifact. Marque comme `disk_available=False` ceux dont le fichier
n'existe plus (rotation).

Appele :
  - par cron (management command `scan_backups`) toutes les heures
  - en debut de chaque endpoint de listing (rafraichissement opportuniste)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import BackupArtifact
from .utils import parse_backup_filename


logger = logging.getLogger(__name__)


# Sous-dossiers attendus dans BACKUP_BASE_DIR. Le scanner les parcourt tous.
SUBDIRS = ('daily', 'weekly', 'monthly', 'manual')


@dataclass
class ScanReport:
    new_artifacts: int = 0
    marked_missing: int = 0
    skipped_invalid: int = 0
    errors: int = 0

    def __str__(self) -> str:
        return (
            f'{self.new_artifacts} nouveau(x), '
            f'{self.marked_missing} disparu(s), '
            f'{self.skipped_invalid} fichier(s) ignore(s), '
            f'{self.errors} erreur(s)'
        )


def _iter_backup_files(base_dir: Path) -> Iterable[Path]:
    """Yield tous les fichiers candidats dans les sous-dossiers connus."""
    for sub in SUBDIRS:
        d = base_dir / sub
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_file():
                yield f


def _index_existing_artifacts() -> set[str]:
    """Set des file_path deja en BD pour eviter un SELECT par fichier."""
    return set(
        BackupArtifact.objects.values_list('file_path', flat=True)
    )


@transaction.atomic
def scan_backup_directories(base_dir: Path | str | None = None) -> ScanReport:
    """
    Parcourt BACKUP_BASE_DIR/{daily,weekly,monthly,manual}/ et :
      - cree un BackupArtifact pour chaque nouveau .sql.gz[.enc]
      - met disk_available=False sur les artifacts dont le fichier a disparu

    Idempotent : peut etre relance autant qu'on veut.
    """
    base_dir = Path(base_dir or settings.BACKUP_BASE_DIR)
    report = ScanReport()

    if not base_dir.exists():
        logger.warning('BACKUP_BASE_DIR introuvable : %s', base_dir)
        return report

    existing_paths = _index_existing_artifacts()
    seen_on_disk: set[str] = set()

    for file in _iter_backup_files(base_dir):
        seen_on_disk.add(str(file))

        if str(file) in existing_paths:
            continue  # deja en BD

        parsed = parse_backup_filename(file.name)
        if not parsed:
            logger.debug('Ignore (nom non canonique) : %s', file.name)
            report.skipped_invalid += 1
            continue

        try:
            size = file.stat().st_size
        except OSError as e:
            logger.warning('Impossible de stat %s: %s', file, e)
            report.errors += 1
            continue

        try:
            BackupArtifact.objects.create(
                type=parsed['type'],
                created_at=timezone.make_aware(parsed['created_at']),
                file_path=str(file),
                file_size_bytes=size,
                sha256_hash='',  # calcule a la demande (au download)
                is_encrypted=parsed['is_encrypted'],
                triggered_by=None,
                notes='Detecte par scanner',
                disk_available=True,
            )
            report.new_artifacts += 1
        except Exception:
            logger.exception('Erreur creation BackupArtifact pour %s', file)
            report.errors += 1

    # Marquer comme disparus ceux qui etaient dispo et ne le sont plus
    to_mark_missing = BackupArtifact.objects.filter(disk_available=True).exclude(
        file_path__in=seen_on_disk,
    )
    report.marked_missing = to_mark_missing.update(disk_available=False)

    logger.info('Scan termine : %s', report)
    return report


def refresh_artifact_status(artifact: BackupArtifact) -> bool:
    """
    Verifie que le fichier d'un artifact precis existe toujours.
    Met a jour disk_available si necessaire. Retourne l'etat final.
    """
    exists = Path(artifact.file_path).exists()
    if exists != artifact.disk_available:
        artifact.disk_available = exists
        artifact.save(update_fields=['disk_available'])
    return exists
