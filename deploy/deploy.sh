#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════
# SIGA — Deploy/Update sur serveur Linux (Docker stack)
# Usage :
#   ./deploy.sh init          → 1er déploiement (génère .env + dump.sql + start)
#   ./deploy.sh update        → MAJ code uniquement (rebuild backend+frontend)
#   ./deploy.sh reset-db      → Réimporte dump.sql (DESTRUCTIF — supprime db_data)
#   ./deploy.sh backup        → Snapshot SQL gzippé dans /opt/backups
#   ./deploy.sh setup-backup  → Installe scripts + cron de sauvegarde auto
#   ./deploy.sh setup-lan [IP]→ Cert auto-signé + config LAN on-premise (défaut 192.168.0.120)
#   ./deploy.sh logs          → Tail logs des 4 services
#   ./deploy.sh status        → État des containers
# ════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Configuration — A ADAPTER ────────────────────────────────────────
DOMAIN="${DOMAIN:-siga.example.mr}"
# ROOT_DIR = PARENT (arbo a plat) contenant les repos + backups/logs :
#   /opt/siga (repo), /opt/gesafped_frontend (repo), /opt/backups, /opt/logs.
ROOT_DIR="${ROOT_DIR:-/opt}"
DEPLOY_DIR="$ROOT_DIR/siga/deploy"      # = /opt/siga/deploy
BACKUP_DIR="$ROOT_DIR/backups"          # = /opt/backups (dossier scanne par Django)

# ── Helpers ──────────────────────────────────────────────────────────
log()  { echo -e "\033[1;32m▶ $*\033[0m"; }
warn() { echo -e "\033[1;33m⚠ $*\033[0m"; }
err()  { echo -e "\033[1;31m✗ $*\033[0m" >&2; exit 1; }

cd "$DEPLOY_DIR" 2>/dev/null || err "Repertoire $DEPLOY_DIR introuvable"

# ── Moteur BD (cutover PostgreSQL) ───────────────────────────────────
# DB_ENGINE : 'postgresql' (defaut — defaut de la branche PostgreSQL)
# ou 'mysql'. Lisible depuis l'environnement ou depuis deploy/.env.
# En mode postgresql : compose = docker-compose.yml (via COMPOSE_FILE,
# donc toutes les commandes `docker compose` ci-dessous restent identiques),
# et les commandes mysql/mysqldump deviennent psql/pg_dump.
if [ -z "${DB_ENGINE:-}" ] && [ -f .env ]; then
  DB_ENGINE=$(grep '^DB_ENGINE=' .env | cut -d= -f2- || true)
fi
# Normalisation : suppression d'un CR eventuel (deploy/.env edite sous Windows)
# + minuscules, pour que 'postgresql\r' ou 'PostgreSQL' soient reconnus.
DB_ENGINE=$(printf '%s' "${DB_ENGINE:-}" | tr -d '\r' | tr '[:upper:]' '[:lower:]')
DB_ENGINE="${DB_ENGINE:-postgresql}"
COMPOSE_FILE_NAME="docker-compose.yml"
if [ "$DB_ENGINE" = "postgresql" ]; then
  COMPOSE_FILE_NAME="docker-compose.yml"
  export COMPOSE_FILE="$COMPOSE_FILE_NAME"
fi

case "${1:-}" in

# ════════════════════════════════════════════════════════════════════
# init — 1er déploiement
# ════════════════════════════════════════════════════════════════════
init)
  log "Initialisation du stack SIGA pour $DOMAIN"

  # 1. Generer .env si absent
  if [ ! -f .env ]; then
    log "Generation .env (secrets aleatoires)"
    SK=$(openssl rand -base64 48 | tr -d '\n')
    DBP=$(openssl rand -base64 24 | tr -d '\n=/+')
    DBR=$(openssl rand -base64 24 | tr -d '\n=/+')
    # DB_ENGINE est PERSISTE dans .env (valeur active au moment de l'init,
    # defaut postgresql) : deploy.sh et les backup-scripts/*.sh le relisent depuis
    # deploy/.env — sans cette ligne, un cron sans DB_ENGINE dans son env
    # retomberait silencieusement sur mysqldump apres un cutover PostgreSQL.
    cat > .env <<EOF
DOMAIN=$DOMAIN
SECRET_KEY=$SK
DB_PASSWORD=$DBP
DB_ROOT_PASSWORD=$DBR
DB_ENGINE=${DB_ENGINE}
EOF
    chmod 600 .env
    warn "Credentials générés dans .env — note-les en lieu sûr !"
    cat .env
  else
    log ".env existant conserve"
  fi

  # 2. Verifier dump.sql
  if [ ! -f dump.sql ]; then
    if [ "$DB_ENGINE" = "postgresql" ]; then
      err "dump.sql absent. Transfere-le depuis le dev (pg_dump gesafped26 > dump.sql)"
    else
      err "dump.sql absent. Transfere-le depuis le dev (mysqldump gesafped26 > dump.sql)"
    fi
  fi
  DUMP_SIZE=$(stat -c%s dump.sql)
  log "dump.sql trouvé ($((DUMP_SIZE / 1024 / 1024)) MB)"

  # 3. Adapter docker-compose si besoin (COMPOSE_FILE_NAME = fichier actif
  #    selon DB_ENGINE : docker-compose.yml ou docker-compose.yml)
  if grep -q '"8090:80"' "$COMPOSE_FILE_NAME"; then
    warn "Port 8090 -> 80 (sed)"
    sed -i 's|"8090:80"|"80:80"|' "$COMPOSE_FILE_NAME"
  fi

  # Mettre a jour ALLOWED_HOSTS et CORS pour le domaine prod
  log "Mise a jour ALLOWED_HOSTS et CORS pour $DOMAIN"
  sed -i "s|ALLOWED_HOSTS:.*|ALLOWED_HOSTS: $DOMAIN,localhost,backend|" "$COMPOSE_FILE_NAME"
  sed -i "s|CORS_ALLOWED_ORIGINS:.*|CORS_ALLOWED_ORIGINS: http://$DOMAIN,https://$DOMAIN|" "$COMPOSE_FILE_NAME"

  # 4. Build + Up
  log "Build des images (5-10 min)"
  docker compose build --no-cache

  log "Demarrage du stack"
  docker compose up -d

  log "Attente de l'import du dump.sql (60 sec)"
  sleep 60

  log "Etat final :"
  docker compose ps
  echo ""
  log "Logs backend (verifier les migrations) :"
  docker compose logs backend --tail 30

  log "DEPLOIEMENT TERMINE — http://$DOMAIN devrait etre accessible"
  ;;

# ════════════════════════════════════════════════════════════════════
# update — Mise à jour du code (sans toucher à la DB)
# ════════════════════════════════════════════════════════════════════
update)
  log "Mise a jour du code (git pull + rebuild)"
  # deploy/ etant un sous-dossier de siga/, le git pull de siga met aussi a jour
  # le compose, le deploy.sh et le dump. Pas besoin d'un 3e pull.
  cd "$ROOT_DIR/siga" && git pull
  cd "$ROOT_DIR/gesafped_frontend" && git pull
  cd "$DEPLOY_DIR"

  log "Rebuild des images"
  docker compose build backend frontend

  log "Redemarrage backend + frontend"
  docker compose up -d backend frontend

  log "Application des nouvelles migrations"
  docker compose exec backend python manage.py migrate --no-input

  log "Etat :"
  docker compose ps
  ;;

# ════════════════════════════════════════════════════════════════════
# reset-db — Réimporte dump.sql (DESTRUCTIF)
# ════════════════════════════════════════════════════════════════════
reset-db)
  warn "ATTENTION : cette operation supprime le volume db_data."
  read -p "Continuer ? Tape RESET pour confirmer : " confirm
  [ "$confirm" = "RESET" ] || err "Abandonne."

  if [ ! -f dump.sql ]; then
    err "dump.sql absent. Place-le dans $DEPLOY_DIR avant de continuer."
  fi

  log "Stop + suppression du volume db_data"
  docker compose down -v

  log "Redemarrage (import auto du dump.sql)"
  docker compose up -d

  sleep 60
  log "Logs db :"
  docker compose logs db --tail 20
  log "Logs backend :"
  docker compose logs backend --tail 30
  ;;

# ════════════════════════════════════════════════════════════════════
# backup — Snapshot SQL gzippé
# ════════════════════════════════════════════════════════════════════
backup)
  mkdir -p "$BACKUP_DIR"
  DATE=$(date +%Y%m%d_%H%M%S)
  FILE="$BACKUP_DIR/gesafped26_$DATE.sql.gz"

  if [ "$DB_ENGINE" = "postgresql" ]; then
    # PG : pg_dump dans le conteneur. Mdp via PGPASSWORD dans l'env du exec
    # (jamais en argv). Equivalences : --single-transaction implicite (MVCC),
    # routines/triggers inclus par defaut, encoding UTF8.
    DB_PWD=$(grep '^DB_PASSWORD=' .env | cut -d= -f2-)

    log "Backup vers $FILE"
    docker compose exec -T -e PGPASSWORD="$DB_PWD" db pg_dump \
      -U siga --format=plain --encoding=UTF8 gesafped26 \
      | gzip > "$FILE"
  else
    ROOT_PWD=$(grep DB_ROOT_PASSWORD .env | cut -d= -f2)

    log "Backup vers $FILE"
    docker compose exec -T db mysqldump -u root -p"$ROOT_PWD" \
      --routines --triggers --events --single-transaction --quick \
      --default-character-set=utf8mb4 gesafped26 \
      | gzip > "$FILE"
  fi

  log "Taille : $(du -h $FILE | cut -f1)"

  # Garde les 30 derniers
  ls -t $BACKUP_DIR/gesafped26_*.sql.gz 2>/dev/null | tail -n +31 | xargs -r rm
  log "Backups conserves : $(ls $BACKUP_DIR/gesafped26_*.sql.gz | wc -l)"
  ;;

# ════════════════════════════════════════════════════════════════════
# setup-backup — Installe scripts + cron des sauvegardes automatiques
# ════════════════════════════════════════════════════════════════════
setup-backup)
  log "Installation du systeme de sauvegarde automatique"

  # 1. Permissions executables sur les scripts
  log "Permissions +x sur backup-scripts/"
  chmod +x "$DEPLOY_DIR"/backup-scripts/*.sh

  # 2. Creer arborescence backups + logs
  log "Creation $ROOT_DIR/{backups/{daily,weekly,monthly,manual},logs}"
  mkdir -p "$ROOT_DIR"/backups/{daily,weekly,monthly,manual}
  mkdir -p "$ROOT_DIR"/logs
  chmod 700 "$ROOT_DIR"/backups

  # 3. Installer le fichier cron
  log "Installation /etc/cron.d/siga-backup"
  install -m 644 "$DEPLOY_DIR"/cron/siga-backup /etc/cron.d/siga-backup

  # 4. Installer logrotate (optionnel mais recommande)
  if [ -d /etc/logrotate.d ]; then
    log "Installation /etc/logrotate.d/siga-backup"
    install -m 644 "$DEPLOY_DIR"/cron/logrotate-siga-backup /etc/logrotate.d/siga-backup
  fi

  # 5. Recharger cron
  if systemctl list-units --type=service | grep -q cron; then
    log "Reload service cron"
    systemctl reload cron 2>/dev/null || systemctl restart cron
  fi

  # 6. Test : un scan immediat pour creer les BackupArtifact existants
  log "Premier scan_backups (creation BackupArtifact pour les fichiers existants)"
  docker compose exec -T backend python manage.py scan_backups 2>/dev/null || \
    warn "scan_backups : impossible. Le backend est-il demarre ?"

  log "Setup termine."
  log ""
  log "Crons installes :"
  cat /etc/cron.d/siga-backup | grep -E '^[0-9]'
  log ""
  log "Pour activer le monitoring (optionnel), defini dans .env :"
  log "  SIGA_BACKUP_HC_URL=https://hc-ping.com/<uuid>"
  log "Et redemarre cron."
  ;;

# ════════════════════════════════════════════════════════════════════
# setup-lan — Certificat auto-signé + config pour un déploiement LAN
# ════════════════════════════════════════════════════════════════════
setup-lan)
  LAN_IP="${2:-192.168.0.120}"
  log "Configuration LAN on-premise pour https://$LAN_IP (cert auto-signe)"

  # 1. Certificat auto-signe (CN + SAN sur l'IP), valide 10 ans
  mkdir -p "$DEPLOY_DIR/certs"
  if [ -f "$DEPLOY_DIR/certs/selfsigned.crt" ]; then
    warn "certs/selfsigned.crt existe deja — conserve (supprime-le pour regenerer)."
  else
    log "Generation certs/selfsigned.{crt,key}"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
      -keyout "$DEPLOY_DIR/certs/selfsigned.key" \
      -out    "$DEPLOY_DIR/certs/selfsigned.crt" \
      -subj   "/CN=$LAN_IP" \
      -addext "subjectAltName=IP:$LAN_IP"
    chmod 600 "$DEPLOY_DIR/certs/selfsigned.key"
  fi

  log ""
  log "Ajoute/verifie dans $DEPLOY_DIR/.env :"
  log "  DOMAIN=$LAN_IP"
  log "  SIGA_NGINX_CONF=./nginx.lan.conf"
  log "  (SECRET_KEY / DB_PASSWORD / DB_ROOT_PASSWORD : propres a CE serveur)"
  log ""
  log "Puis :  docker compose up -d --build"
  log "-> l'app repond sur https://$LAN_IP (avertissement de cert a accepter une fois)."
  ;;

# ════════════════════════════════════════════════════════════════════
# logs / status
# ════════════════════════════════════════════════════════════════════
logs)
  docker compose logs -f --tail 100 "${2:-}"
  ;;

status)
  docker compose ps
  echo ""
  log "Volumes :"
  docker volume ls | grep siga || true
  echo ""
  log "Disque :"
  df -h /var/lib/docker | tail -1
  ;;

*)
  echo "Usage: $0 {init|update|reset-db|backup|setup-backup|setup-lan|logs|status}"
  exit 1
  ;;
esac
