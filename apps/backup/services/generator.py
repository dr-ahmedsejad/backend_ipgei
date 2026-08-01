"""
Generator : declenche un dump BD chiffre a la demande (bouton manuel).
Vendor-aware au RUNTIME (connection.vendor) : mysqldump si MySQL, pg_dump si
PostgreSQL — preparation cutover PG, comportement MySQL strictement inchange.

Format : .7z (AES-256 + LZMA2 + header encryption + PBKDF2 ~524K iterations).

Contenu de l'archive :
  siga.sql           : dump complet de la BD (inclut core_audit_log, axes,
                       backup_download_log, et toutes les tables metier)
  manifest.json      : metadonnees (version, date, user, hashes, contenu)
  media/             : copie complete du MEDIA_ROOT (avatars, PDF officiels
                       signes, justificatifs, photos). Optionnel via
                       BACKUP_INCLUDE_MEDIA=False.

Securite :
  - Le mdp n'apparait JAMAIS dans la cmdline (--defaults-extra-file pour MySQL,
    PGPASSWORD injecte dans l'env du subprocess pour PostgreSQL)
  - Le mdp n'est JAMAIS persiste (ni log, ni BD, ni fichier)
  - header_encryption=True cache aussi les noms internes
  - Resistance brute-force : KDF 7z standard (524K iterations PBKDF2-SHA256)
  - Plafond anti-DoS via BACKUP_MANUAL_MAX_SIZE_MB
  - Fichier temporaire .sql efface en try/finally (jamais en clair sur disque)

Pour restaurer :
  - GUI : 7-Zip (Windows), File Roller (Linux), Keka (Mac)
  - CLI : 7z x siga_manual_xxx.7z -pMonMotDePasse
  - Python : deploy/backup-scripts/decrypt-backup.py
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import py7zr
from django.conf import settings
from django.db import connection
from django.utils import timezone

from ..models import BackupArtifact, TYPE_MANUAL
from .utils import (
    compute_sha256, get_directory_size, sanitize_username_for_filename,
)


logger = logging.getLogger(__name__)


class BackupGenerationError(Exception):
    """Erreur dediee aux echecs de generation, pour distinguer du reste."""


def _verify_binary(name: str, path: str) -> None:
    """Echec rapide si le binaire est introuvable (donne un message clair)."""
    if Path(path).exists():
        return
    if os.sep not in path and '/' not in path:
        return
    # 'mysqldump' -> BACKUP_MYSQLDUMP_BIN, 'pg_dump' -> BACKUP_PGDUMP_BIN
    # (l'underscore est retire pour matcher le nom exact du setting).
    raise BackupGenerationError(
        f'Binaire {name} introuvable : {path}. '
        f'Verifie BACKUP_{name.upper().replace("_", "")}_BIN dans .env '
        f'(chemin absolu attendu).',
    )


def _build_manifest(
    *, user, sql_size: int, sql_sha256: str,
    media_size: int, media_files: int,
    include_media: bool,
) -> dict:
    """Construit le manifest.json embarque dans le .7z."""
    try:
        django_version = __import__('django').get_version()
    except Exception:
        django_version = 'unknown'
    try:
        py7zr_version = py7zr.__version__
    except Exception:
        py7zr_version = 'unknown'

    return {
        'siga_backup_format_version': 1,
        'generated_at':                timezone.now().isoformat(),
        'generated_by':                user.username,
        'generated_by_full_name':      getattr(user, 'name', '') or '',
        'generated_by_role':           user.role,
        'db_name':                     settings.DATABASES['default']['NAME'],
        'sql_size_bytes':              sql_size,
        'sql_sha256':                  sql_sha256,
        'media_included':              include_media,
        'media_size_bytes':            media_size,
        'media_file_count':            media_files,
        'django_version':              django_version,
        'py7zr_version':                py7zr_version,
        'compression':                  f'LZMA2:{settings.BACKUP_LZMA2_PRESET}',
        'encryption':                  'AES-256 + header_encryption',
    }


def generate_manual_encrypted_backup(
    *,
    user,
    password: str,
    notes: str = '',
) -> BackupArtifact:
    """
    Genere un .7z chiffre AES-256 contenant siga.sql + media/ + manifest.json.
    L'enregistre comme BackupArtifact et retourne l'objet.

    Raise BackupGenerationError si :
      - mdp trop court
      - probleme de config (db.cnf, mysqldump / pg_dump)
      - dump exit != 0
      - BD + media depassent BACKUP_MANUAL_MAX_SIZE_MB
      - erreur py7zr
    """
    min_len = settings.BACKUP_MANUAL_MIN_PASSWORD_LENGTH
    if len(password) < min_len:
        raise BackupGenerationError(
            f'Mot de passe trop court ({len(password)} < {min_len}).',
        )

    # Verifs prerequis — dependantes du moteur BD detecte au RUNTIME
    # (connection.vendor) : le meme code sert avant et apres le cutover PG.
    vendor = connection.vendor
    if vendor == 'postgresql':
        # PG : pas de fichier credentials — le mdp part via PGPASSWORD dans
        # l'env du subprocess (jamais en argv), lu depuis settings.DATABASES.
        _verify_binary('pg_dump', settings.BACKUP_PGDUMP_BIN)
    elif vendor == 'mysql':
        cnf_path = Path(settings.BACKUP_DB_CONFIG_PATH)
        if not cnf_path.exists():
            raise BackupGenerationError(
                f'Fichier credentials introuvable : {cnf_path}. '
                f'Voir deploy.sh setup-backup (ou .env pour dev local).',
            )
        _verify_binary('mysqldump', settings.BACKUP_MYSQLDUMP_BIN)
    else:
        raise BackupGenerationError(
            f'Moteur BD non supporte pour le backup : {vendor}.',
        )

    # Calcul taille media (pre-check anti-DoS)
    include_media = bool(settings.BACKUP_INCLUDE_MEDIA)
    media_dir = Path(settings.MEDIA_ROOT) if settings.MEDIA_ROOT else None
    media_size, media_files = (0, 0)
    if include_media and media_dir and media_dir.exists():
        media_size, media_files = get_directory_size(media_dir)

    # On accepte une estimation rough : sql vise 30 MB, media reel + 100 MB marge
    estimated_total_mb = (30 + (media_size // (1024 * 1024)) + 100)
    max_mb = settings.BACKUP_MANUAL_MAX_SIZE_MB
    if estimated_total_mb > max_mb:
        raise BackupGenerationError(
            f'Taille estimee ({estimated_total_mb} MB) depasse le plafond '
            f'({max_mb} MB). Augmente BACKUP_MANUAL_MAX_SIZE_MB '
            f'ou utilise les backups cron automatiques.',
        )

    # Preparer les chemins
    ts = timezone.now()
    safe_user = sanitize_username_for_filename(user.username)
    base_name = f'siga_manual_{ts:%Y%m%d_%H%M%S}_{safe_user}'

    manual_dir = Path(settings.BACKUP_BASE_DIR) / 'manual'
    manual_dir.mkdir(parents=True, exist_ok=True)

    temp_sql = manual_dir / f'.tmp_{base_name}.sql'
    output_path = manual_dir / f'{base_name}.7z'

    # Subprocess env minimal
    env = {'PATH': os.environ.get('PATH', '')}
    if os.name == 'nt':
        for k in ('SYSTEMROOT', 'TEMP', 'TMP', 'USERPROFILE'):
            if k in os.environ:
                env[k] = os.environ[k]

    logger.info(
        'Generation backup manuel chiffre (.7z) : user=%s out=%s media=%s (%s fichiers)',
        user.username, output_path.name,
        f'{media_size // (1024*1024)} MB' if include_media else 'exclu',
        media_files,
    )

    db_settings = settings.DATABASES['default']
    db_name = db_settings['NAME']

    try:
        # ── Etape 1 : dump SQL vers fichier temp (mysqldump / pg_dump selon vendor) ──
        if vendor == 'postgresql':
            # Equivalences pg_dump <-> mysqldump :
            #   --single-transaction            -> implicite (snapshot MVCC)
            #   --routines --triggers --events  -> inclus par defaut (schema complet)
            #   --default-character-set=utf8mb4 -> --encoding=UTF8
            #   sortie                          -> --format=plain (SQL brut sur stdout,
            #                                      restaurable par psql, comme siga.sql)
            dump_tool = 'pg_dump'
            dump_bin_var = 'BACKUP_PGDUMP_BIN'
            dump_cmd = [
                settings.BACKUP_PGDUMP_BIN,
                '--host', str(db_settings.get('HOST') or 'localhost'),
                '--port', str(db_settings.get('PORT') or '5432'),
                '--username', str(db_settings.get('USER') or 'postgres'),
                '--no-password',   # jamais de prompt interactif (subprocess)
                '--format=plain',
                '--encoding=UTF8',
                db_name,
            ]
            # Le mdp ne doit JAMAIS apparaitre en argv (visible via ps) :
            # PGPASSWORD est injecte dans l'env minimal du subprocess uniquement.
            if db_settings.get('PASSWORD'):
                env['PGPASSWORD'] = str(db_settings['PASSWORD'])
        else:
            dump_tool = 'mysqldump'
            dump_bin_var = 'BACKUP_MYSQLDUMP_BIN'
            dump_cmd = [
                settings.BACKUP_MYSQLDUMP_BIN,
                f'--defaults-extra-file={settings.BACKUP_DB_CONFIG_PATH}',
                '--single-transaction', '--routines', '--triggers', '--events',
                '--default-character-set=utf8mb4',
                db_name,
            ]
        with open(temp_sql, 'wb') as f:
            try:
                result = subprocess.run(
                    dump_cmd,
                    stdout=f,
                    stderr=subprocess.PIPE,
                    env=env,
                    timeout=600,
                    check=False,
                )
            except FileNotFoundError as e:
                raise BackupGenerationError(
                    f'{dump_tool} introuvable : {e}. Verifie {dump_bin_var}.',
                ) from e
            except subprocess.TimeoutExpired as e:
                raise BackupGenerationError(
                    f'{dump_tool} timeout (>10 min) — operation annulee.',
                ) from e

        if result.returncode != 0:
            err = (result.stderr or b'').decode(errors='replace')[:500]
            raise BackupGenerationError(
                f'{dump_tool} a echoue (exit {result.returncode}). {err}',
            )

        sql_size = temp_sql.stat().st_size
        if sql_size < 1024:
            raise BackupGenerationError(
                f'Fichier SQL suspectement petit ({sql_size} octets).',
            )

        sql_sha256 = compute_sha256(temp_sql)

        # ── Etape 2 : construire le manifest ──
        manifest = _build_manifest(
            user=user, sql_size=sql_size, sql_sha256=sql_sha256,
            media_size=media_size, media_files=media_files,
            include_media=include_media,
        )
        if notes:
            manifest['notes'] = notes
        manifest_bytes = json.dumps(
            manifest, indent=2, ensure_ascii=False,
        ).encode('utf-8')

        # ── Etape 3 : encapsuler dans .7z chiffre ──
        # On utilise les filtres par defaut de py7zr (LZMA2 + 7zAES).
        # Le `preset` controle LZMA2 (1=rapide, 9=max). 5 = bon compromis.
        filters = [
            {'id': py7zr.FILTER_LZMA2, 'preset': settings.BACKUP_LZMA2_PRESET},
            {'id': py7zr.FILTER_CRYPTO_AES256_SHA256},
        ]

        try:
            with py7zr.SevenZipFile(
                str(output_path),
                'w',
                password=password,
                header_encryption=True,
                filters=filters,
            ) as archive:
                # 1. siga.sql (la BD : contient core_audit_log + axes + backup_log)
                archive.write(str(temp_sql), arcname='siga.sql')
                # 2. manifest.json
                archive.writestr(manifest_bytes, 'manifest.json')
                # 3. media/ (si active et present)
                if include_media and media_dir and media_dir.exists() and media_files > 0:
                    archive.writeall(str(media_dir), arcname='media')
        except Exception as e:
            output_path.unlink(missing_ok=True)
            raise BackupGenerationError(
                f'Echec creation archive 7z : {e}',
            ) from e

    finally:
        # TOUJOURS supprimer le fichier .sql clair
        temp_sql.unlink(missing_ok=True)

    # Verifs coherence
    if not output_path.exists():
        raise BackupGenerationError('Archive 7z creee mais fichier absent.')

    size = output_path.stat().st_size
    if size < 256:
        output_path.unlink(missing_ok=True)
        raise BackupGenerationError(
            f'Archive 7z suspectement petite ({size} octets).',
        )

    # Hash + creation de l'artifact
    sha256 = compute_sha256(output_path)
    note_summary = notes or 'Backup manuel chiffre (.7z)'
    if include_media:
        note_summary += f' [BD + media: {media_files} fichiers]'
    else:
        note_summary += ' [BD seule, media exclu]'

    artifact = BackupArtifact.objects.create(
        type=TYPE_MANUAL,
        created_at=ts,
        file_path=str(output_path),
        file_size_bytes=size,
        sha256_hash=sha256,
        is_encrypted=True,
        triggered_by=user,
        notes=note_summary[:200],
        disk_available=True,
    )
    logger.info(
        'Backup manuel cree : artifact_id=%s sql=%s octets media=%s octets archive=%s octets',
        artifact.id, sql_size, media_size, size,
    )
    return artifact


def cleanup_old_manual_backups(retention_days: int | None = None) -> int:
    """
    Supprime les backups manuels (sur disque + artifact) plus vieux que
    BACKUP_MANUAL_RETENTION_DAYS. Retourne le nombre supprime.

    Les artifacts BD restent (marque disk_available=False) pour conserver
    l'historique des telechargements (FK PROTECT depuis BackupDownloadLog).
    """
    days = retention_days or settings.BACKUP_MANUAL_RETENTION_DAYS
    cutoff = timezone.now() - timedelta(days=days)
    deleted = 0
    for artifact in BackupArtifact.objects.filter(
        type=TYPE_MANUAL, created_at__lt=cutoff, disk_available=True,
    ):
        path = Path(artifact.file_path)
        try:
            path.unlink(missing_ok=True)
            artifact.disk_available = False
            artifact.save(update_fields=['disk_available'])
            deleted += 1
        except OSError:
            logger.exception('Echec suppression %s', path)
    logger.info('Cleanup manuels termine : %s fichier(s) supprime(s)', deleted)
    return deleted
