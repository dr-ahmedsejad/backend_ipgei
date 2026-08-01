#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════
# SIGA — Backup quotidien (2h et 14h via cron)
#
# Produit :  /opt/backups/daily/siga_daily_YYYYMMDD_HHMMSS.sql.gz
# Retention : 7 jours (sera nettoye par cleanup-old.sh)
# ════════════════════════════════════════════════════════════════════
set -euo pipefail

ROOT_DIR="${SIGA_ROOT_DIR:-/opt}"
DEPLOY_DIR="$ROOT_DIR/siga/deploy"
BACKUP_ROOT="$ROOT_DIR/backups"
DEST="$BACKUP_ROOT/daily"
TS=$(date +%Y%m%d_%H%M%S)
FILE="$DEST/siga_daily_${TS}.sql.gz"
LOG="$ROOT_DIR/logs/backup-daily.log"

# Healthcheck URL (Uptime Kuma LAN ou healthchecks.io). Optionnel.
HC_URL="${SIGA_BACKUP_HC_URL:-}"

mkdir -p "$DEST" "$(dirname "$LOG")"
exec >> "$LOG" 2>&1
echo "── $(date '+%Y-%m-%d %H:%M:%S') Backup quotidien demarre ──"

# Ping start si HC configure
if [ -n "$HC_URL" ]; then
    curl -fsS --max-time 10 "${HC_URL}/start" >/dev/null 2>&1 || true
fi

cd "$DEPLOY_DIR"

# Moteur BD (cutover PostgreSQL) : 'postgresql' (defaut — comportement historique
# inchange) ou 'mysql'. Lisible depuis l'env (cron) ou depuis deploy/.env.
# En mode postgresql le compose actif devient docker-compose.yml.
if [ -z "${DB_ENGINE:-}" ]; then
    DB_ENGINE=$(grep '^DB_ENGINE=' .env 2>/dev/null | cut -d= -f2- || true)
fi
# Normalisation : suppression d'un CR eventuel (deploy/.env edite sous Windows)
# + minuscules, pour que 'postgresql\r' ou 'PostgreSQL' soient reconnus.
DB_ENGINE=$(printf '%s' "${DB_ENGINE:-}" | tr -d '\r' | tr '[:upper:]' '[:lower:]')
DB_ENGINE="${DB_ENGINE:-postgresql}"
[ "$DB_ENGINE" = "postgresql" ] && export COMPOSE_FILE="docker-compose.yml"

# Recup mdp root depuis .env (chmod 600) — MySQL uniquement (PG n'a pas de
# root password separe : DB_PASSWORD suffit, lu dans la branche pg_dump).
DB_ROOT_PWD=""
if [ "$DB_ENGINE" != "postgresql" ]; then
    DB_ROOT_PWD=$(grep '^DB_ROOT_PASSWORD=' .env | cut -d= -f2-)
fi
DB_NAME=$(grep '^MYSQL_DATABASE\|^DB_NAME' docker-compose.yml | head -1 | awk -F: '{print $2}' | tr -d ' ' || echo gesafped26)
DB_NAME="${DB_NAME:-gesafped26}"

# Dump via container db (single-transaction = pas de lock + coherence)
if [ "$DB_ENGINE" = "postgresql" ]; then
    # pg_dump : single-transaction implicite (snapshot MVCC), routines/triggers
    # inclus par defaut. Mdp via PGPASSWORD dans l'env du exec (jamais en argv).
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

# Verifs basiques
SIZE=$(stat -c%s "$FILE" 2>/dev/null || stat -f%z "$FILE")
if [ "$SIZE" -lt 100000 ]; then
    echo "ERREUR : backup suspect ($SIZE octets < 100 KB)"
    rm -f "$FILE"
    [ -n "$HC_URL" ] && curl -fsS "${HC_URL}/fail" >/dev/null 2>&1 || true
    exit 1
fi

if ! gunzip -t "$FILE" 2>>"$LOG"; then
    echo "ERREUR : gzip corrompu"
    rm -f "$FILE"
    [ -n "$HC_URL" ] && curl -fsS "${HC_URL}/fail" >/dev/null 2>&1 || true
    exit 1
fi

# Hash SHA-256 pour registre d'integrite
sha256sum "$FILE" > "${FILE}.sha256"

# Notifier Django qui scanne pour mettre a jour les BackupArtifact
docker compose exec -T backend python manage.py scan_backups >/dev/null 2>&1 || \
    echo "WARN : scan_backups a echoue (non bloquant)"

echo "OK : $(basename "$FILE") ($(numfmt --to=iec --suffix=B "$SIZE" 2>/dev/null || echo "$SIZE octets"))"

# Ping success
[ -n "$HC_URL" ] && curl -fsS "${HC_URL}" >/dev/null 2>&1 || true

echo "── Backup quotidien termine ──"
