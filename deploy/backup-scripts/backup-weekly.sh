#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════
# SIGA — Backup hebdomadaire (dimanche 23h via cron)
#
# Produit : /opt/backups/weekly/siga_weekly_YYYYMMDD_HHMMSS.sql.gz
# Retention : 90 jours
# ════════════════════════════════════════════════════════════════════
set -euo pipefail

ROOT_DIR="${SIGA_ROOT_DIR:-/opt}"
DEPLOY_DIR="$ROOT_DIR/siga/deploy"
BACKUP_ROOT="$ROOT_DIR/backups"
DEST="$BACKUP_ROOT/weekly"
TS=$(date +%Y%m%d_%H%M%S)
FILE="$DEST/siga_weekly_${TS}.sql.gz"
LOG="$ROOT_DIR/logs/backup-weekly.log"

HC_URL="${SIGA_BACKUP_WEEKLY_HC_URL:-}"

mkdir -p "$DEST" "$(dirname "$LOG")"
exec >> "$LOG" 2>&1
echo "── $(date '+%Y-%m-%d %H:%M:%S') Backup hebdomadaire demarre ──"

[ -n "$HC_URL" ] && curl -fsS --max-time 10 "${HC_URL}/start" >/dev/null 2>&1 || true

cd "$DEPLOY_DIR"

# Moteur BD (cutover PostgreSQL) : 'postgresql' (defaut — comportement historique
# inchange) ou 'mysql'. Lisible depuis l'env (cron) ou depuis deploy/.env.
if [ -z "${DB_ENGINE:-}" ]; then
    DB_ENGINE=$(grep '^DB_ENGINE=' .env 2>/dev/null | cut -d= -f2- || true)
fi
# Normalisation : suppression d'un CR eventuel (deploy/.env edite sous Windows)
# + minuscules, pour que 'postgresql\r' ou 'PostgreSQL' soient reconnus.
DB_ENGINE=$(printf '%s' "${DB_ENGINE:-}" | tr -d '\r' | tr '[:upper:]' '[:lower:]')
DB_ENGINE="${DB_ENGINE:-postgresql}"
[ "$DB_ENGINE" = "postgresql" ] && export COMPOSE_FILE="docker-compose.yml"

DB_ROOT_PWD=""
if [ "$DB_ENGINE" != "postgresql" ]; then
    DB_ROOT_PWD=$(grep '^DB_ROOT_PASSWORD=' .env | cut -d= -f2-)
fi
DB_NAME="${SIGA_DB_NAME:-gesafped26}"

if [ "$DB_ENGINE" = "postgresql" ]; then
    # pg_dump : single-transaction implicite (MVCC), routines/triggers inclus.
    # Mdp via PGPASSWORD dans l'env du exec (jamais en argv).
    DB_PWD=$(grep '^DB_PASSWORD=' .env | cut -d= -f2-)
    if ! docker compose exec -T -e PGPASSWORD="$DB_PWD" db pg_dump \
            -U siga --format=plain --encoding=UTF8 \
            "$DB_NAME" 2>>"$LOG" | gzip -9 > "$FILE"; then
        echo "ERREUR : pg_dump a echoue"
        rm -f "$FILE"
        [ -n "$HC_URL" ] && curl -fsS "${HC_URL}/fail" >/dev/null 2>&1 || true
        exit 1
    fi
elif ! docker compose exec -T db mysqldump \
        -u root -p"$DB_ROOT_PWD" \
        --single-transaction --routines --triggers --events \
        --default-character-set=utf8mb4 \
        "$DB_NAME" 2>>"$LOG" | gzip -9 > "$FILE"; then
    echo "ERREUR : mysqldump a echoue"
    rm -f "$FILE"
    [ -n "$HC_URL" ] && curl -fsS "${HC_URL}/fail" >/dev/null 2>&1 || true
    exit 1
fi

SIZE=$(stat -c%s "$FILE" 2>/dev/null || stat -f%z "$FILE")
[ "$SIZE" -lt 100000 ] && { echo "ERREUR : backup trop petit"; rm -f "$FILE"; exit 1; }
gunzip -t "$FILE" 2>>"$LOG" || { echo "ERREUR gzip"; rm -f "$FILE"; exit 1; }

sha256sum "$FILE" > "${FILE}.sha256"

docker compose exec -T backend python manage.py scan_backups >/dev/null 2>&1 || true

echo "OK : $(basename "$FILE") ($(numfmt --to=iec --suffix=B "$SIZE" 2>/dev/null || echo "$SIZE octets"))"
[ -n "$HC_URL" ] && curl -fsS "${HC_URL}" >/dev/null 2>&1 || true
echo "── Termine ──"
