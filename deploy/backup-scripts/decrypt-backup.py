"""
Utilitaire de dechiffrement d'un backup SIGA manuel.

Formats supportes :
  - .7z              : format actuel (contient siga.sql + manifest.json + media/)
  - .sql.gz.enc      : LEGACY openssl AES-256-CBC (anciens backups, BD seule)

Usage :
  python decrypt-backup.py siga_manual_xxx.7z [--mdp MOT_DE_PASSE]
  python decrypt-backup.py siga_manual_xxx.sql.gz.enc [--mdp MOT_DE_PASSE]

Si --mdp absent, demande interactif (pas d'echo terminal).

Sorties :
  - Format .7z   : dossier siga_restaure_<timestamp>/ contenant
                   siga.sql, manifest.json, media/
  - Format legacy: fichier siga_restaure_<timestamp>.sql
"""
from __future__ import annotations

import argparse
import getpass
import gzip
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


# Liste de chemins probables pour openssl sous Windows (legacy)
OPENSSL_CANDIDATES = [
    r'C:\wamp64\bin\apache\apache2.4.58\bin\openssl.exe',
    r'C:\Program Files\Git\mingw64\bin\openssl.exe',
    r'C:\Program Files\Git\usr\bin\openssl.exe',
    'openssl',
]


def find_openssl(override: str | None = None) -> str:
    """Trouve un binaire openssl utilisable (pour le format legacy)."""
    if override:
        if Path(override).exists() or shutil.which(override):
            return override
        sys.exit(f'ERREUR : openssl introuvable a {override}')
    for cand in OPENSSL_CANDIDATES:
        if Path(cand).exists():
            return cand
        if shutil.which(cand):
            return cand
    sys.exit(
        'ERREUR : openssl introuvable. Donne le chemin via --openssl=...'
    )


def decrypt_7z(*, encrypted_path: Path, password: str, output_dir: Path) -> Path:
    """
    Dechiffre + extrait un .7z dans `output_dir/`. Necessite la lib py7zr.

    Le .7z contient :
      - siga.sql       : dump BD
      - manifest.json  : metadonnees
      - media/         : copie des uploads (optionnel selon settings serveur)

    Retourne le chemin du dossier extrait.
    """
    try:
        import py7zr
    except ImportError:
        sys.exit(
            'ERREUR : la lib py7zr est requise pour les fichiers .7z\n'
            '  pip install py7zr'
        )

    print(f'[1/2] Ouverture de {encrypted_path.name}...')

    def _wrong_password():
        sys.exit('ERREUR : mot de passe incorrect (ou archive corrompue).')

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with py7zr.SevenZipFile(str(encrypted_path), 'r', password=password) as archive:
            try:
                names = archive.getnames()
            except Exception:
                _wrong_password()

            if 'siga.sql' not in names:
                sys.exit(
                    f'ERREUR : siga.sql introuvable dans l\'archive. '
                    f'Contenu : {names[:5]}...'
                )

            print(f'[2/2] Extraction vers {output_dir}/ ({len(names)} entree(s))...')
            try:
                archive.extractall(path=output_dir)
            except Exception as e:
                msg = str(e).lower()
                if any(k in msg for k in ('password', 'authentication', 'crc',
                                          'header', 'unknown field', 'invalid')):
                    _wrong_password()
                raise

    except py7zr.exceptions.PasswordRequired:
        sys.exit('ERREUR : mot de passe requis pour cette archive.')
    except py7zr.exceptions.Bad7zFile as e:
        sys.exit(f'ERREUR : archive 7z invalide ou corrompue : {e}')
    except SystemExit:
        raise
    except Exception as e:
        msg = str(e).lower()
        if any(k in msg for k in ('password', 'authentication', 'crc',
                                  'header', 'unknown field', 'invalid')):
            _wrong_password()
        sys.exit(f'ERREUR py7zr : {e}')

    # Affichage manifest si present
    manifest_path = output_dir / 'manifest.json'
    if manifest_path.exists():
        try:
            import json
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            print()
            print('── Manifest ──')
            for k, v in manifest.items():
                if isinstance(v, str) and len(v) > 70:
                    v = v[:67] + '...'
                print(f'  {k:30s} : {v}')
        except Exception:
            pass

    return output_dir


def decrypt_openssl_legacy(
    *, encrypted_path: Path, password: str, output_path: Path, openssl: str,
) -> int:
    """Dechiffre + decompresse un .sql.gz.enc (format legacy openssl)."""
    tmp_gz = output_path.with_suffix('.sql.gz.tmp')

    cmd = [
        openssl, 'aes-256-cbc', '-d', '-salt', '-pbkdf2',
        '-in', str(encrypted_path),
        '-out', str(tmp_gz),
        '-pass', f'pass:{password}',
    ]
    print(f'[1/2] Dechiffrement openssl de {encrypted_path.name}...')
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        tmp_gz.unlink(missing_ok=True)
        stderr = result.stderr.decode(errors='replace')[:300]
        sys.exit(
            f'ERREUR openssl (exit {result.returncode}): {stderr}\n'
            f'Cause probable : mauvais mot de passe.'
        )

    print(f'[2/2] Decompression gzip vers {output_path.name}...')
    try:
        with gzip.open(tmp_gz, 'rb') as gz_in, open(output_path, 'wb') as sql_out:
            shutil.copyfileobj(gz_in, sql_out, length=1024 * 1024)
    except gzip.BadGzipFile:
        tmp_gz.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        sys.exit(
            'ERREUR : gzip invalide. Cela peut indiquer un mot de passe '
            'incorrect (le dechiffrement a donne des octets aleatoires).'
        )

    tmp_gz.unlink(missing_ok=True)
    return output_path.stat().st_size


def main():
    p = argparse.ArgumentParser(description='Dechiffre un backup SIGA (.7z ou .sql.gz.enc).')
    p.add_argument('input', help='Fichier .7z ou .sql.gz.enc a dechiffrer')
    p.add_argument('--mdp', help='Mot de passe (sinon demande interactif)')
    p.add_argument('--output',
                   help='Dossier de sortie (.7z) ou fichier .sql (legacy)')
    p.add_argument('--openssl',
                   help='[legacy] Chemin vers openssl.exe pour les .sql.gz.enc')
    args = p.parse_args()

    inp = Path(args.input)
    if not inp.exists():
        sys.exit(f'ERREUR : fichier introuvable : {inp}')

    password = args.mdp or getpass.getpass('Mot de passe : ')
    if not password:
        sys.exit('ERREUR : mot de passe vide.')

    # Detection format selon extension
    name_lower = inp.name.lower()
    if name_lower.endswith('.7z'):
        out_dir = Path(args.output) if args.output else Path(
            f'siga_restaure_{datetime.now():%Y%m%d_%H%M%S}'
        )
        if out_dir.exists() and any(out_dir.iterdir()):
            sys.exit(
                f'ERREUR : {out_dir} existe et n\'est pas vide. '
                f'Choisis un autre nom (--output) ou supprime-le.'
            )

        result_dir = decrypt_7z(
            encrypted_path=inp, password=password, output_dir=out_dir,
        )

        sql_path = result_dir / 'siga.sql'
        media_dir = result_dir / 'media'

        print()
        print(f'OK : extrait dans {result_dir}/')
        if sql_path.exists():
            print(f'  siga.sql       : {sql_path.stat().st_size:,} octets')
        if media_dir.exists():
            # Compter recursivement
            count = sum(1 for _ in media_dir.rglob('*') if _.is_file())
            print(f'  media/         : {count} fichier(s)')
        print()
        print('Pour restaurer dans une BD test :')
        print(f'  mysql -u root -e "CREATE DATABASE siga_restore_test;"')
        print(f'  mysql -u root siga_restore_test < "{sql_path}"')
        if media_dir.exists():
            print('Pour restaurer les media :')
            print(f'  cp -r "{media_dir}"/* /chemin/vers/siga/media/')

    elif name_lower.endswith('.sql.gz.enc'):
        out_file = Path(args.output) if args.output else Path(
            f'siga_restaure_{datetime.now():%Y%m%d_%H%M%S}.sql'
        )
        if out_file.exists():
            sys.exit(f'ERREUR : {out_file} existe deja.')

        openssl = find_openssl(args.openssl)
        print(f'(format legacy openssl) openssl utilise : {openssl}')
        size = decrypt_openssl_legacy(
            encrypted_path=inp, password=password,
            output_path=out_file, openssl=openssl,
        )
        print()
        print(f'OK : {out_file} ({size:,} octets)')
        print()
        print('Pour restaurer dans une BD test :')
        print(f'  mysql -u root -e "CREATE DATABASE siga_restore_test;"')
        print(f'  mysql -u root siga_restore_test < {out_file}')

    else:
        sys.exit(
            f'ERREUR : format non reconnu. Attendu .7z ou .sql.gz.enc, '
            f'recu {inp.suffix}'
        )


if __name__ == '__main__':
    main()
