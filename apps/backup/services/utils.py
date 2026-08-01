"""
Helpers partages par les services backup.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Optional


# Format de nom de fichier canonique :
#   siga_<TYPE>_YYYYMMDD_HHMMSS[_<EXTRA>]<EXT>
#
# Extensions supportees :
#   .sql.gz           : dump clair compresse (cron daily/weekly/monthly)
#   .sql.gz.enc       : LEGACY — chiffrement openssl AES-256-CBC (obsolete, conserve
#                       pour relire d'anciens fichiers ; le bouton manuel ne le
#                       produit plus, voir .7z)
#   .7z               : archive 7z chiffree AES-256 (bouton manuel actuel)
#
# Exemples :
#   siga_daily_20260530_020000.sql.gz
#   siga_manual_20260530_143012_ahmed.7z
#   siga_manual_20260530_143012_ahmed.sql.gz.enc  (legacy)
FILENAME_RE = re.compile(
    r'^siga_(?P<type>daily|weekly|monthly|manual)_'
    r'(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})_'
    r'(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})'
    r'(?:_(?P<extra>[a-zA-Z0-9_-]+))?'
    r'(?:'
    r'(?P<sql_gz>\.sql\.gz)(?P<enc_legacy>\.enc)?'  # clear OR openssl legacy
    r'|'
    r'(?P<sevenz>\.7z)'                              # nouvelle archive 7z
    r')$'
)


def parse_backup_filename(filename: str) -> Optional[dict]:
    """
    Extrait type, timestamp et chiffrement d'un nom de fichier canonique.
    Retourne None si le nom ne matche pas.

    Sous-type quotidien : 'daily_2h' ou 'daily_14h' selon heure (proche).
    """
    m = FILENAME_RE.match(filename)
    if not m:
        return None
    g = m.groupdict()
    try:
        ts = datetime(
            int(g['year']), int(g['month']), int(g['day']),
            int(g['hour']), int(g['minute']), int(g['second']),
        )
    except ValueError:
        return None

    base_type = g['type']
    if base_type == 'daily':
        # Decoupage horaire : pres de 2h -> daily_2h, pres de 14h -> daily_14h
        # Tolerance large pour absorber un cron qui demarre tard (jusqu'a 4h
        # d'ecart) — au-dela on tombe sur l'autre crenneau.
        backup_type = 'daily_2h' if ts.hour < 8 else 'daily_14h'
    else:
        backup_type = base_type  # weekly | monthly | manual

    # Encrypte si .enc (legacy openssl) ou .7z
    is_encrypted = g.get('enc_legacy') is not None or g.get('sevenz') is not None

    return {
        'type':         backup_type,
        'created_at':   ts,
        'is_encrypted': is_encrypted,
        'extra':        g.get('extra') or '',
    }


def compute_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """SHA-256 d'un fichier, calcule par chunks pour eviter charger en RAM."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(chunk_size), b''):
            h.update(chunk)
    return h.hexdigest()


def get_client_ip(request) -> str:
    """
    Recupere l'IP du client. Respecte X-Forwarded-For uniquement si on est
    derriere un reverse proxy de confiance (TRUSTED_PROXY_IPS).
    """
    from django.conf import settings
    trusted = getattr(settings, 'TRUSTED_PROXY_IPS', set())
    remote = request.META.get('REMOTE_ADDR', '')
    if remote in trusted:
        xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
        if xff:
            # Premier IP de la chaine = client reel
            return xff.split(',')[0].strip()
    return remote or '0.0.0.0'


def sanitize_username_for_filename(username: str) -> str:
    """Ne garde que [a-zA-Z0-9_-] (max 20 chars) pour usage dans un nom de fichier."""
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', username)
    return safe[:20] or 'user'


def get_directory_size(directory: Path) -> tuple[int, int]:
    """
    Calcule la taille totale d'un dossier et le nombre de fichiers (recursif).
    Retourne (size_bytes, file_count). Retourne (0, 0) si le dossier n'existe pas.
    """
    if not directory.exists() or not directory.is_dir():
        return 0, 0
    total_size = 0
    file_count = 0
    for item in directory.rglob('*'):
        try:
            if item.is_file():
                total_size += item.stat().st_size
                file_count += 1
        except OSError:
            # fichier supprime entre rglob et stat, on ignore
            continue
    return total_size, file_count
