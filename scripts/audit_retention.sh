#!/usr/bin/env bash
# =============================================================================
# SIGA — Job de retention audit log (Linux/macOS via cron)
# =============================================================================
# Crontab suggere :
#   0 3 * * *  /opt/siga/scripts/audit_retention.sh >> /var/log/siga/audit_retention.log 2>&1
#
# Pre-requis :
#   - Variables d'env via /etc/siga/env (sourcees ci-dessous)
#   - venv Python active a /opt/siga/venv
# =============================================================================

set -euo pipefail

PROJECT_DIR="${SIGA_PROJECT_DIR:-/opt/siga}"
cd "$PROJECT_DIR"

# Source env file si present
if [[ -f /etc/siga/env ]]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/siga/env
    set +a
fi

# Venv
if [[ -f "$PROJECT_DIR/venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$PROJECT_DIR/venv/bin/activate"
fi

export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-siga.settings.production}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

echo "=== [$(ts)] DEBUT retention audit ==="

# 1. Archive : HOT > HOT_DAYS -> ARCHIVE
python manage.py archive_audit_logs

# 2. Purge : ARCHIVE > ARCHIVE_DAYS -> JSONL.gz + DELETE
python manage.py purge_audit_logs

echo "=== [$(ts)] FIN retention audit ==="
echo
