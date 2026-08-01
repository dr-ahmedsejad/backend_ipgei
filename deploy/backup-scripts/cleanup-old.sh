#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════
# SIGA — Nettoyage des vieux backups (cron quotidien matin)
#
# Retention :
#   - daily/    : 7 jours
#   - weekly/   : 90 jours
#   - monthly/  : 5 ans (1825 j)
#   - manual/   : delegue a Django (BACKUP_MANUAL_RETENTION_DAYS, defaut 7)
#
# Apres le cleanup, on demande a Django de rescanner pour marquer les
# artifacts dont le fichier disque a disparu.
# ════════════════════════════════════════════════════════════════════
set -euo pipefail

ROOT_DIR="${SIGA_ROOT_DIR:-/opt}"
DEPLOY_DIR="$ROOT_DIR/siga/deploy"
BACKUP_ROOT="$ROOT_DIR/backups"
LOG="$ROOT_DIR/logs/cleanup-old.log"

mkdir -p "$(dirname "$LOG")"
exec >> "$LOG" 2>&1
echo "── $(date '+%Y-%m-%d %H:%M:%S') Cleanup demarre ──"

cleanup_dir() {
    local dir="$1"
    local days="$2"
    [ -d "$dir" ] || return 0
    local before=$(ls -1 "$dir"/siga_*.sql.gz 2>/dev/null | wc -l)
    find "$dir" -type f -name 'siga_*.sql.gz' -mtime "+${days}" -delete
    find "$dir" -type f -name 'siga_*.sql.gz.sha256' -mtime "+${days}" -delete
    local after=$(ls -1 "$dir"/siga_*.sql.gz 2>/dev/null | wc -l)
    echo "  $(basename "$dir") : $before -> $after (retention ${days}j)"
}

cleanup_dir "$BACKUP_ROOT/daily"   7
cleanup_dir "$BACKUP_ROOT/weekly"  90
cleanup_dir "$BACKUP_ROOT/monthly" 1825

# Manuels : delegue a Django (respecte BACKUP_MANUAL_RETENTION_DAYS)
cd "$DEPLOY_DIR"
docker compose exec -T backend python manage.py cleanup_manual_backups >/dev/null 2>&1 \
    && echo "  manual/  : cleanup Django OK" \
    || echo "  manual/  : cleanup Django KO (non bloquant)"

# Rescan pour mettre a jour disk_available=False sur les disparus
docker compose exec -T backend python manage.py scan_backups >/dev/null 2>&1 || true

# Logs : on garde 30 jours
find "$(dirname "$LOG")" -type f -name '*.log' -mtime +30 -delete 2>/dev/null || true

echo "── Cleanup termine ──"
