#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════
# SIGA — Test de restauration mensuel (1er du mois 04h)
#
# Restaure le dernier backup quotidien dans une BD JETABLE et verifie
# que les donnees essentielles sont presentes. Supprime la BD jetable
# a la fin. AUCUN effet sur la prod.
#
# En cas d'echec : envoie un ping "fail" a healthchecks.io / Uptime Kuma
# et exit 1.
# ════════════════════════════════════════════════════════════════════
set -euo pipefail

ROOT_DIR="${SIGA_ROOT_DIR:-/opt}"
DEPLOY_DIR="$ROOT_DIR/siga/deploy"
BACKUP_ROOT="$ROOT_DIR/backups"
LOG="$ROOT_DIR/logs/restore-test.log"

HC_URL="${SIGA_RESTORE_TEST_HC_URL:-}"

mkdir -p "$(dirname "$LOG")"
exec >> "$LOG" 2>&1
echo "── $(date '+%Y-%m-%d %H:%M:%S') Test restauration demarre ──"

fail() {
    echo "ECHEC : $*"
    [ -n "$HC_URL" ] && curl -fsS --max-time 10 "${HC_URL}/fail" >/dev/null 2>&1 || true
    exit 1
}

# 1. Trouver le dernier backup quotidien
LATEST=$(ls -1t "$BACKUP_ROOT"/daily/siga_daily_*.sql.gz 2>/dev/null | head -1)
[ -z "$LATEST" ] && fail "Aucun backup quotidien trouve dans $BACKUP_ROOT/daily/"

echo "Source : $LATEST"

# 2. Verifier hash si present
if [ -f "${LATEST}.sha256" ]; then
    (cd "$(dirname "$LATEST")" && sha256sum -c "$(basename "$LATEST").sha256" >>"$LOG" 2>&1) \
        || fail "Hash SHA-256 invalide"
    echo "Hash OK"
fi

# 3. Verifier gzip integrite
gunzip -t "$LATEST" || fail "gzip corrompu"
echo "gzip OK"

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

DB_NAME="${SIGA_DB_NAME:-gesafped26}"
DRILL_DB="siga_drill_$(date +%Y%m)"

if [ "$DB_ENGINE" = "postgresql" ]; then
    # ── Variante PostgreSQL ──────────────────────────────────────────
    # Equivalences : mysql -N -e -> psql -tA -c ; la qualification
    # cross-database (db.table) n'existe pas en PG -> on change de base
    # via -d. Mdp via PGPASSWORD dans l'env du exec (jamais en argv).
    DB_PWD=$(grep '^DB_PASSWORD=' .env | cut -d= -f2-)

    pgexec() {  # pgexec <base> [options psql...] : psql via le conteneur db
        local base="$1"; shift
        docker compose exec -T -e PGPASSWORD="$DB_PWD" db \
            psql -U siga -d "$base" "$@"
    }

    # 4. Creer BD jetable (WITH (FORCE) coupe les connexions residuelles ;
    #    DROP/CREATE DATABASE ne supportent pas la transaction implicite
    #    multi-ordres de psql -c -> deux appels separes)
    echo "Creation BD jetable : $DRILL_DB"
    pgexec postgres -c "DROP DATABASE IF EXISTS ${DRILL_DB} WITH (FORCE);" \
        || fail "Impossible de creer ${DRILL_DB}"
    pgexec postgres -c "CREATE DATABASE ${DRILL_DB} ENCODING 'UTF8' TEMPLATE template0;" \
        || fail "Impossible de creer ${DRILL_DB}"

    cleanup() {
        echo "Cleanup : DROP DATABASE ${DRILL_DB}"
        pgexec postgres -c "DROP DATABASE IF EXISTS ${DRILL_DB} WITH (FORCE);" \
            2>/dev/null || true
    }
    trap cleanup EXIT

    # 5. Restaurer dedans (ON_ERROR_STOP=1 : exit != 0 a la 1re erreur,
    #    meme semantique que le client mysql)
    echo "Restauration en cours..."
    if ! gunzip -c "$LATEST" | pgexec "$DRILL_DB" -q -v ON_ERROR_STOP=1 2>>"$LOG"; then
        fail "Restauration KO"
    fi

    # 6. Verifications metier (schema par defaut 'public' de la base courante)
    COUNT_TABLES=$(pgexec "$DRILL_DB" -tA -c \
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';")
    COUNT_USERS=$(pgexec "$DRILL_DB" -tA -c \
        "SELECT COUNT(*) FROM authentication_customuser;" 2>/dev/null || echo 0)

    # Reference : meme requete sur la BD prod
    COUNT_USERS_PROD=$(pgexec "$DB_NAME" -tA -c \
        "SELECT COUNT(*) FROM authentication_customuser;" 2>/dev/null || echo 0)
else
    # ── Variante MySQL (comportement historique inchange) ────────────
    DB_ROOT_PWD=$(grep '^DB_ROOT_PASSWORD=' .env | cut -d= -f2-)

    # 4. Creer BD jetable
    echo "Creation BD jetable : $DRILL_DB"
    docker compose exec -T db mysql -u root -p"$DB_ROOT_PWD" -e \
        "DROP DATABASE IF EXISTS ${DRILL_DB}; CREATE DATABASE ${DRILL_DB} CHARACTER SET utf8mb4;" \
        || fail "Impossible de creer ${DRILL_DB}"

    cleanup() {
        echo "Cleanup : DROP DATABASE ${DRILL_DB}"
        docker compose exec -T db mysql -u root -p"$DB_ROOT_PWD" -e \
            "DROP DATABASE IF EXISTS ${DRILL_DB};" 2>/dev/null || true
    }
    trap cleanup EXIT

    # 5. Restaurer dedans
    echo "Restauration en cours..."
    if ! gunzip -c "$LATEST" | docker compose exec -T db mysql \
            -u root -p"$DB_ROOT_PWD" "$DRILL_DB" 2>>"$LOG"; then
        fail "Restauration KO"
    fi

    # 6. Verifications metier
    COUNT_TABLES=$(docker compose exec -T db mysql -u root -p"$DB_ROOT_PWD" -N -e \
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='${DRILL_DB}';")
    COUNT_USERS=$(docker compose exec -T db mysql -u root -p"$DB_ROOT_PWD" -N -e \
        "SELECT COUNT(*) FROM ${DRILL_DB}.authentication_customuser;" 2>/dev/null || echo 0)

    # Reference : meme requete sur la BD prod
    COUNT_USERS_PROD=$(docker compose exec -T db mysql -u root -p"$DB_ROOT_PWD" -N -e \
        "SELECT COUNT(*) FROM ${DB_NAME}.authentication_customuser;" 2>/dev/null || echo 0)
fi

echo "Tables restaurees     : $COUNT_TABLES"
echo "Users restaures       : $COUNT_USERS"
echo "Users prod (compare)  : $COUNT_USERS_PROD"

# Seuils
[ "$COUNT_TABLES" -lt 50 ] && fail "Seulement $COUNT_TABLES tables (attendu > 50) — backup partiel ?"
[ "$COUNT_USERS" -lt 1 ]   && fail "0 user restaure — backup vide ?"

# Tolerance : la prod a pu evoluer depuis la nuit (nouveau user). Tolerance 5%.
if [ "$COUNT_USERS_PROD" -gt 0 ]; then
    DIFF=$(( COUNT_USERS_PROD - COUNT_USERS ))
    DIFF=${DIFF#-}  # abs
    TOLERANCE=$(( COUNT_USERS_PROD / 20 ))   # 5%
    [ "$TOLERANCE" -lt 2 ] && TOLERANCE=2
    if [ "$DIFF" -gt "$TOLERANCE" ]; then
        echo "WARN : ecart users $COUNT_USERS vs $COUNT_USERS_PROD (toleance: $TOLERANCE)"
    fi
fi

echo "── Test restauration REUSSI ──"
[ -n "$HC_URL" ] && curl -fsS --max-time 10 "${HC_URL}" >/dev/null 2>&1 || true
