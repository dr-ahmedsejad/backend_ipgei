#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# sync-receive.sh — RECEVEUR de synchronisation, côté VPS (miroir).
#
# Ne se lance JAMAIS à la main : uniquement par SSH à commande forcée, depuis le
# serveur local. Dans authorized_keys du compte de dépôt :
#
#   command="/opt/ipgei/bin/sync-receive.sh",no-pty,no-port-forwarding,\
#   no-agent-forwarding,no-X11-forwarding <clé publique du local>
#
# La clé ainsi bridée n'ouvre pas de shell : volée, elle ne permet que de
# pousser un dump — et un dump dont l'empreinte est vérifiée avant tout.
#
#   • l'empreinte attendue arrive dans $SSH_ORIGINAL_COMMAND  ("sync <sha256>")
#   • le dump gzippé arrive sur l'entrée standard.
#
# Déroulé : vérifier l'empreinte → sauvegarder (point de retour) → restaurer →
# redémarrer le backend. En cas d'échec de la restauration, retour en arrière
# depuis la sauvegarde : le portail ne reste jamais à moitié restauré.
#
# La BOÎTE DE RÉCEPTION n'est pas dans le dump — elle traverse donc l'opération
# intacte. C'est tout l'intérêt du `--exclude-table` côté local.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DB_CONTAINER="${IPGEI_DB_CONTAINER:-ipgei-db}"
BACKEND_CONTAINER="${IPGEI_BACKEND_CONTAINER:-ipgei-backend}"
DB_NAME="${IPGEI_DB_NAME:-IPGEI}"
DB_USER="${IPGEI_DB_USER:-postgres}"
BACKUP_DIR="${IPGEI_BACKUP_DIR:-/opt/ipgei/backups}"

log(){ echo "[sync-receive] $*" >&2; }

# 1) L'empreinte attendue, extraite de la commande forcée ---------------------
EXPECTED_SHA="$(printf '%s' "${SSH_ORIGINAL_COMMAND:-}" | awk '{print $2}')"
[ -n "$EXPECTED_SHA" ] || { log "ERREUR: empreinte sha256 absente de la commande"; exit 2; }

TMP="$(mktemp /tmp/ipgei-sync.XXXXXX.sql.gz)"
trap 'rm -f "$TMP"' EXIT
cat > "$TMP"                                    # le dump gzippé arrive sur stdin

# 2) Intégrité — AVANT de toucher à la base -----------------------------------
ACTUAL_SHA="$(sha256sum "$TMP" | awk '{print $1}')"
if [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
  log "ERREUR: empreinte invalide (attendue=$EXPECTED_SHA reçue=$ACTUAL_SHA) — abandon"
  exit 3
fi
log "intégrité vérifiée ($ACTUAL_SHA)"

# 3) Sauvegarde : le point de retour ------------------------------------------
mkdir -p "$BACKUP_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP="$BACKUP_DIR/pre-sync-$TS.sql.gz"
docker exec "$DB_CONTAINER" pg_dump --no-owner --no-privileges -U "$DB_USER" "$DB_NAME" \
  | gzip > "$BACKUP"
log "sauvegarde : $BACKUP ($(du -h "$BACKUP" | cut -f1))"

# 4) Restauration, backend arrêté ---------------------------------------------
# `ON_ERROR_STOP=1` est capital : sans lui, psql poursuit après une erreur et
# laisse une base à moitié restaurée, que rien ne signalerait.
docker stop "$BACKEND_CONTAINER" >/dev/null
if ! gunzip -c "$TMP" | docker exec -i "$DB_CONTAINER" \
       psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>/tmp/ipgei-restore.err; then
  log "ERREUR de restauration — retour en arrière depuis $BACKUP"
  gunzip -c "$BACKUP" | docker exec -i "$DB_CONTAINER" \
    psql -U "$DB_USER" -d "$DB_NAME" >/dev/null || true
  docker start "$BACKEND_CONTAINER" >/dev/null
  log "$(tail -3 /tmp/ipgei-restore.err 2>/dev/null)"
  exit 4
fi
docker start "$BACKEND_CONTAINER" >/dev/null

echo "OK synchronisation appliquée ${TS} (sauvegarde ${BACKUP##*/})"
