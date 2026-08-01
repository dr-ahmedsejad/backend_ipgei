# SIGA — Déploiement Docker

Stack de production : **MySQL 8 + Django + Next.js + Nginx**.

## Pré-requis

- Docker 24+ et Docker Compose v2
- Le code source de :
  - `../siga/` (Django backend)
  - `../gesafped_frontend/` (Next.js frontend)
- Un dump MySQL initial : `dump.sql` à la racine (généré avec `mysqldump`)

## Multi-host (127.0.0.1 / 192.168.0.120 / localhost)

L'application est accessible via **3 origines** simultanément :
- http://localhost:8090
- http://127.0.0.1:8090
- http://192.168.0.120 (IP réseau, accessible aux autres machines du LAN)

**Comment ça marche** : le frontend est buildé avec un placeholder
`http://__SIGA_API_HOST__` comme valeur de `NEXT_PUBLIC_API_URL`. Nginx
réécrit ce placeholder à la volée (sub_filter) en `$scheme://$http_host` selon
le Host d'arrivée du navigateur. Résultat : les XHR du frontend tombent toujours
sur le **même origin** que la page → cookies JWT httpOnly transmis correctement
(SameSite=Lax fonctionne).

`server_name _` côté nginx accepte n'importe quel Host. `ALLOWED_HOSTS` Django
inclut les 3 hostnames + `backend` (nom interne du container).

## CSP (Content-Security-Policy)

Headers de sécurité ajoutés par Nginx :
- `Content-Security-Policy: default-src 'self'; ...` — limite le contenu au
  même origin (compatible avec le sub_filter qui assure le same-origin)
- `X-Frame-Options: SAMEORIGIN` — anti-clickjacking
- `X-Content-Type-Options: nosniff` — anti-MIME sniffing
- `Referrer-Policy: strict-origin-when-cross-origin`

Si tu utilises Google Fonts (Cairo), la CSP autorise déjà
`https://fonts.googleapis.com` et `https://fonts.gstatic.com`.

## Premier déploiement (BD vierge)

```bash
# 1. Configurer
cp .env.example .env
# Éditer .env : DOMAIN, SECRET_KEY, DB_PASSWORD, DB_ROOT_PASSWORD

# 2. Le dump BD (dump.sql.gz, versionné dans git — repo privé) est décompressé
#    ET importé AUTOMATIQUEMENT par MySQL au 1er démarrage (l'image gère les
#    .sql.gz du dossier initdb). Rien à faire ici.

# 3. Build + lancement
docker compose up -d --build

# 4. Vérifier
docker compose ps
docker compose logs -f
```

Accès depuis n'importe lequel des 3 URLs ci-dessus (Multi-host).

## Réimporter un nouveau dump (mise à jour BD)

Le fichier `dump.sql.gz` est importé **uniquement** au tout premier démarrage
(quand le volume `db_data` est vide). Pour forcer une réimportation :

```bash
# 1. Generer un dump frais depuis la BD active (WAMP local), SANS --databases
#    (tables seules -> s'importe dans gesafped26), puis compresser :
"C:/wamp64/bin/mysql/mysql8.X.X/bin/mysqldump.exe" -u root \
  --single-transaction --routines --triggers \
  --default-character-set=utf8mb4 \
  siga | gzip > deploy/dump.sql.gz
#    (commit + push dump.sql.gz, puis `git pull` sur le serveur)

# 2. Reset du volume DB (⚠️ supprime les donnees du container)
docker compose down -v

# 3. Relancer le stack — MySQL decompresse et importe dump.sql.gz automatiquement
docker compose up -d --build
```

## Mises à jour code (sans toucher à la BD)

```bash
# Pull du nouveau code
cd ../siga && git pull
cd ../gesafped_frontend && git pull
cd ../siga-deploy

# Rebuild + redémarrage SANS toucher au volume db_data
docker compose build backend frontend
docker compose up -d
```

## Sauvegarde BD du container

```bash
docker compose exec db mysqldump -u root -p$DB_ROOT_PASSWORD gesafped26 \
  | gzip > backups/gesafped26_$(date +%Y%m%d_%H%M).sql.gz
```

## Restauration

```bash
gunzip < backups/gesafped26_XXXXXX.sql.gz \
  | docker compose exec -T db mysql -u root -p$DB_ROOT_PASSWORD gesafped26
```

## Commandes utiles

```bash
# Logs en temps reel
docker compose logs -f backend
docker compose logs -f nginx

# Shell Django
docker compose exec backend python manage.py shell

# Shell MySQL (du container)
docker compose exec db mysql -u siga -p gesafped26

# Redemarrer un service
docker compose restart backend nginx

# Tout arreter (les donnees restent dans les volumes)
docker compose down

# Tout reset (⚠️ supprime aussi la BD container !)
docker compose down -v
```

## Diagnostic CSP / multi-host

Si l'app ne se charge pas correctement sur l'un des 3 URLs :

1. **Vérifier que le placeholder est bien réécrit** :
   ```bash
   curl -s http://localhost:8090 | grep -c "__SIGA_API_HOST__"
   # Doit retourner 0 (le placeholder a ete reecrit)
   ```

2. **Vérifier qu'aucun gzip ne bloque sub_filter** :
   ```bash
   curl -sI -H "Accept-Encoding: identity" http://localhost:8090/_next/static/chunks/main.js \
     | grep -i content-encoding
   # Ne doit PAS contenir "gzip"
   ```

3. **Vérifier la CSP** :
   ```bash
   curl -sI http://localhost:8090 | grep -i content-security-policy
   ```

4. **Vérifier ALLOWED_HOSTS** : ouvrir la console navigateur, regarder les
   erreurs Django (DisallowedHost). Si présent, l'IP n'est pas dans
   `ALLOWED_HOSTS` du `docker-compose.yml`.
