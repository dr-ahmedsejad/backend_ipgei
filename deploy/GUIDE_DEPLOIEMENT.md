# 🚀 Guide de déploiement — SIGA sur DigitalOcean

Déploiement de la stack **Docker Compose** (MySQL 8 · Redis · phpMyAdmin · Django · Next.js · Nginx) sur un Droplet DigitalOcean, accessible via **`https://ent.iss-gp.mr`**.

---

## 🧭 Architecture

- **1 Droplet** Ubuntu 24.04 (Docker + Compose) — toute la stack tourne dessus.
- **DNS** `ent.iss-gp.mr` (enregistrement `A`) → IP du Droplet, géré sur **cPanel**.
- **Nginx** (dans Docker) sert le frontend Next.js **et** proxifie l'API Django → même origine (cookies JWT httpOnly OK).
- SIGA vit sur **`ent.iss-gp.mr`** ; le futur **site ISS** viendra sur la racine `iss-gp.mr` (même Droplet, routage Nginx par nom de domaine).

## 📌 Valeurs de CE déploiement

| | |
|---|---|
| Domaine | `ent.iss-gp.mr` |
| IP Droplet | `46.101.58.251` |
| Repos GitHub (privés) | `dr-ahmedsejad/siga`, `dr-ahmedsejad/gesafped_frontend` |
| Dossiers serveur | `/opt/siga`, `/opt/gesafped_frontend`, `/opt/backups` |
| Base de données | `gesafped26` (utilisateur `siga`) |

> ⚠️ **Secrets** (`SECRET_KEY`, mots de passe BD) : ne JAMAIS les mettre dans ce fichier ni dans git. Ils vivent uniquement dans `/opt/siga/deploy/.env` sur le serveur.

---

## ✅ Prérequis

- Compte **DigitalOcean**.
- **Nom de domaine** avec accès DNS (cPanel → *Zone Editor*).
- Une **clé SSH** locale : `ssh-keygen -t ed25519` (crée `~/.ssh/id_ed25519` + `.pub`).

---

## Phase 1 — Créer le Droplet

1. DigitalOcean → **Create → Droplets**.
2. **Region** : London (LON1) · **Image** : Ubuntu 24.04 LTS · **Taille** : **4 Go RAM / 2 vCPU**.
3. **Authentication** : *SSH Key* → colle ta clé publique (`cat ~/.ssh/id_ed25519.pub`).
4. **Hostname** : `siga-prod` → **Create Droplet**.
5. Note l'**IPv4** publique.

> **Si `ssh root@<IP>` renvoie `Permission denied (publickey)`** : le Droplet n'a pas ta clé. Ajoute-la via la **console web DO** (Droplet → *Access* → *Launch Droplet Console*) :
> ```bash
> mkdir -p ~/.ssh && chmod 700 ~/.ssh
> echo 'ssh-ed25519 AAAA... toi@machine' >> ~/.ssh/authorized_keys
> chmod 600 ~/.ssh/authorized_keys
> ```

---

## Phase 2 — DNS (cPanel)

cPanel → section **Domains** → **Zone Editor** → à côté de `iss-gp.mr` → **Manage** → **+ Add Record → A** :

| Type | Name | Record / Address | TTL |
|---|---|---|---|
| `A` | `ent` | `<IP du Droplet>` | `3600` |

> ⚠️ Passe par le **Zone Editor**, **PAS** l'outil « Subdomains » (qui ferait pointer `ent` vers l'hébergement cPanel au lieu du Droplet).
> On **ne touche pas** à la racine `@` ni `www` (réservés au futur site ISS).

Vérifier (après quelques minutes) : `nslookup ent.iss-gp.mr` → doit renvoyer l'IP du Droplet.

---

## Phase 3 — Préparer le serveur

Connexion : `ssh root@<IP>`

```bash
# --- Swap 4 Go (le build Next.js est gourmand en RAM) ---
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab

# --- Pare-feu (⚠️ autoriser SSH AVANT d'activer, sinon lock-out !) ---
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable

# --- Docker + Compose (dépôt officiel Docker) ---
apt-get update && apt-get install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker --version && docker compose version   # doit afficher les 2 versions
```

---

## Phase 4 — Récupérer le code + les secrets

### 4.1 Token GitHub (repos privés)
GitHub → **Settings → Developer settings → Personal access tokens → *Tokens (classic)*** → **Generate** → coche le scope **`repo`** → copie le token (`ghp_...`).

### 4.2 Cloner les 2 repos
```bash
mkdir -p /opt/backups && cd /opt
git clone https://<TOKEN>@github.com/dr-ahmedsejad/siga.git
git clone https://<TOKEN>@github.com/dr-ahmedsejad/gesafped_frontend.git
```
> Le **dump BD** (`deploy/dump.sql.gz`, ~22 Mo) arrive **avec le clone** (versionné, repo privé).

### 4.3 Créer le fichier `.env`
```bash
mkdir -p /opt/siga/secrets /opt/siga/media
cd /opt/siga/deploy

# Générer une SECRET_KEY :  python3 -c "import secrets; print(secrets.token_urlsafe(50))"
# Générer un mot de passe :  python3 -c "import secrets; print(secrets.token_hex(16))"
cat > .env << 'EOF'
DOMAIN=ent.iss-gp.mr
SECRET_KEY=<clé_aléatoire_50_caractères>
DB_PASSWORD=<mot_de_passe_hex_1>
DB_ROOT_PASSWORD=<mot_de_passe_hex_2>
EOF
chmod 600 .env
```

### 4.4 Secrets hors git (depuis TA machine locale, PAS le Droplet)
```bash
# Certificat de signature PDF
scp /c/react_projects/GES/siga/secrets/doc_signing.p12 root@<IP>:/opt/siga/secrets/
# Médias (photos étudiants, logos, signatures)
scp -r /c/react_projects/GES/siga/media/* root@<IP>:/opt/siga/media/
```
> **Non bloquant** pour le 1er lancement : sans certificat → la signature PDF a un *fallback* ; sans médias → photos/logos absents. On peut les ajouter après.

---

## Phase 5 — Lancer 🚀

> ⚠️ **`nginx.conf` attend un certificat SSL** (`/etc/letsencrypt/live/…`). Sur un
> serveur neuf, fais d'abord la **Phase 6.1 + 6.2** (installer certbot + émettre le
> cert, port 80 libre) AVANT ce premier `up` — sinon `siga-nginx` boucle (les
> autres services démarrent quand même). Puis reviens ici.

```bash
cd /opt/siga && git pull                       # récupère la config à jour
cd /opt/siga/deploy && docker compose up -d --build
```
> ⏳ **1er lancement long (~15-25 min)** : build backend (wkhtmltopdf + polices), build frontend (`next build`), import du dump (537 Mo décompressés automatiquement).

Ce qui se fait tout seul :
- MySQL **décompresse + importe** `dump.sql.gz` au 1er démarrage.
- Le backend fait **`migrate` + `collectstatic` + `gunicorn`** au démarrage.

Vérifier :
```bash
docker compose ps        # les 6 services Up, db (healthy)
docker compose logs -f   # suivre (Ctrl+C n'arrête PAS les conteneurs)
```

→ Tester dans le navigateur : **http://ent.iss-gp.mr**

---

## 🛠️ Dépannage

**`siga-nginx` en boucle `Restarting` / erreur `bind host port 0.0.0.0:80: address already in use`**
```bash
# 1. Qui tient le port 80 ?
ss -tlnp | grep ':80'
#    - un service hôte (Apache…)  → systemctl disable --now apache2
#    - rien de visible mais nginx boucle = proxy Docker fantôme → redémarrage propre :
cd /opt/siga/deploy
docker compose down      # ⚠️ SANS -v (garde la BD !)
docker compose up -d
docker compose ps        # siga-nginx doit être Up avec 0.0.0.0:80->80/tcp
```

**Erreur `DisallowedHost` (Django)** → le domaine n'est pas dans `ALLOWED_HOSTS` : vérifier `DOMAIN=ent.iss-gp.mr` dans `.env`, puis `docker compose up -d`.

**Le clone échoue (`Write access to repository not granted`)** → token GitHub sans accès : utiliser un token **classic** avec le scope **`repo`**.

---

## Phase 6 — HTTPS (Let's Encrypt) ✅

`nginx.conf` redirige **80 → 443** et sert l'app en TLS ; le certificat **Let's
Encrypt** est émis sur l'HÔTE par `certbot --standalone` et monté (ro) dans le
conteneur nginx (`/etc/letsencrypt`). `DOCUMENTS_BASE_URL=https://…` (QR en https)
et `DJANGO_COOKIE_SECURE=True` (cookies JWT Secure) sont déjà dans le compose.

> ⚠️ **`nginx.conf` référence le certificat** (`/etc/letsencrypt/live/…`). Il faut
> donc **obtenir le certificat AVANT de démarrer nginx**, sinon il boucle.

### 6.1 Installer certbot
```bash
apt-get update && apt-get install -y certbot
```

### 6.2 Émettre le certificat
Le challenge HTTP-01 a besoin du **port 80 libre** :

```bash
cd /opt/siga/deploy
docker compose stop nginx           # libère le port 80 (sauter si 1er déploiement : rien ne tourne encore)

certbot certonly --standalone \
  -d ent.iss-gp.mr \
  --non-interactive --agree-tos -m <ton-email>

ls -l /etc/letsencrypt/live/ent.iss-gp.mr/   # doit lister fullchain.pem + privkey.pem
```

### 6.3 Démarrer / redémarrer la stack
```bash
docker compose up -d
curl -I  http://ent.iss-gp.mr        # → 301 vers https
curl -sI https://ent.iss-gp.mr | head -1   # → HTTP/2 200
```

### 6.4 Renouvellement automatique (obligatoire)
Cert valable **90 j**, renouvelé ~30 j avant expiration par `certbot.timer`. En
standalone, le renouvellement a besoin du port 80 → hooks qui stoppent/redémarrent
nginx le temps de l'émission :

```bash
mkdir -p /etc/letsencrypt/renewal-hooks/pre /etc/letsencrypt/renewal-hooks/post
printf '#!/bin/sh\ndocker compose -f /opt/siga/deploy/docker-compose.yml stop nginx\n' \
  > /etc/letsencrypt/renewal-hooks/pre/stop-nginx.sh
printf '#!/bin/sh\ndocker compose -f /opt/siga/deploy/docker-compose.yml start nginx\n' \
  > /etc/letsencrypt/renewal-hooks/post/start-nginx.sh
chmod +x /etc/letsencrypt/renewal-hooks/pre/stop-nginx.sh \
         /etc/letsencrypt/renewal-hooks/post/start-nginx.sh

certbot renew --dry-run   # doit finir par « all simulated renewals succeeded »
```
> Les lignes rouges « Hook ran with error output » lors du dry-run ne sont PAS des
> erreurs : c'est la sortie normale de `docker compose` (Stopping/Started) que
> certbot affiche sur stderr. Seul compte « all simulated renewals succeeded ».

---

## Phase 7 — Ajouter médias + certificat (si sautés en Phase 4.4)

Depuis ta machine locale :
```bash
scp /c/react_projects/GES/siga/secrets/doc_signing.p12 root@<IP>:/opt/siga/secrets/
scp -r /c/react_projects/GES/siga/media/* root@<IP>:/opt/siga/media/
```
Puis sur le serveur : `cd /opt/siga/deploy && docker compose restart backend nginx`.

---

## 🔧 Maintenance

### Mettre à jour le code (sans toucher à la BD)
```bash
cd /opt/siga && git pull
cd /opt/gesafped_frontend && git pull
cd /opt/siga/deploy && docker compose build backend frontend && docker compose up -d
```

### Mettre à jour la BD (nouveau dump)
```bash
# En local, SANS --databases (tables seules → base gesafped26) :
mysqldump --single-transaction --routines --triggers --default-character-set=utf8mb4 siga \
  | gzip > deploy/dump.sql.gz
# commit + push, puis sur le serveur :
cd /opt/siga && git pull
cd deploy && docker compose down -v && docker compose up -d   # ⚠️ -v EFFACE la BD → réimport du dump
```

### Sauvegardes automatiques (cron) — À ACTIVER une fois

Les sauvegardes planifiées (quotidienne 2h/14h, hebdo, mensuelle, test de restauration,
purge de rétention) vivent dans `deploy/backup-scripts/` + `deploy/cron/siga-backup`.
⚠️ **Elles ne tournent QUE si le cron est installé** — étape à faire une fois :

```bash
cd /opt/siga/deploy
./deploy.sh setup-backup           # installe /etc/cron.d/siga-backup + crée /opt/backups/{daily,weekly,monthly,manual}
cat /etc/cron.d/siga-backup        # vérifier les entrées
# test immédiat (sans attendre 02h) :
SIGA_ROOT_DIR=/opt ./backup-scripts/backup-daily.sh ; ls -lh /opt/backups/daily/
```
> Les scripts lisent `SIGA_ROOT_DIR=/opt` (arbo à plat : `/opt/siga`, `/opt/gesafped_frontend`,
> `/opt/backups`) et écrivent dans `/opt/backups/…` — le dossier scanné par Django.

### Sauvegarde de la BD (manuelle ponctuelle)
```bash
cd /opt/siga/deploy
docker compose exec db sh -c 'mysqldump -u root -p"$MYSQL_ROOT_PASSWORD" gesafped26' \
  | gzip > /opt/backups/gesafped26_$(date +%Y%m%d_%H%M).sql.gz
```

### Commandes utiles
```bash
docker compose ps                     # état
docker compose logs -f backend        # logs backend
docker compose logs -f nginx          # logs nginx
docker compose restart backend nginx  # redémarrer
docker compose exec backend python manage.py shell     # shell Django
docker compose exec db mysql -u siga -p gesafped26      # shell MySQL
docker compose down                   # tout arrêter (garde les volumes/BD)
```

### phpMyAdmin (admin BD)
Bindé sur `127.0.0.1:8091` (pas exposé). Depuis ta machine :
```bash
ssh -L 8091:127.0.0.1:8091 root@<IP>   # puis ouvrir http://localhost:8091
```

---

## 🖥️ Déploiement LAN on-premise (serveur local, HTTPS auto-signé)

Faire tourner **la même stack** sur un serveur du réseau local (ex. `192.168.0.120`),
**indépendant** du droplet (sa propre BD + ses propres secrets). Le droplet prod n'est
**pas** touché — la sélection se fait uniquement par le `.env` local.

```bash
# Sur le serveur LAN (Docker + Compose installés, repos clonés dans /opt) :
cd /opt/siga/deploy

# 1. Certificat auto-signé (CN + SAN sur l'IP) + rappel de la config
bash deploy.sh setup-lan 192.168.0.120       # crée deploy/certs/selfsigned.{crt,key}

# 2. .env de CE serveur (secrets propres) :
cat > .env << 'EOF'
DOMAIN=192.168.0.120
SIGA_NGINX_CONF=./nginx.lan.conf
SECRET_KEY=<clé_aléatoire_50c>
DB_PASSWORD=<mdp>
DB_ROOT_PASSWORD=<mdp>
EOF
chmod 600 .env

# 3. Démarrer
docker compose up -d --build
```
→ Accès : **`https://192.168.0.120`** (avertissement de certificat auto-signé à accepter
une fois). Le login fonctionne : les cookies `Secure` passent car on est bien en **HTTPS**.

**Pourquoi ça ne casse pas la prod :**
- `SIGA_NGINX_CONF=./nginx.lan.conf` sélectionne la conf LAN (cert auto-signé, `server_name _`,
  pas de HSTS). Le droplet n'a pas cette variable → il garde `nginx.conf` (ent.iss-gp.mr + Let's Encrypt).
- `DOMAIN=192.168.0.120` alimente `ALLOWED_HOSTS` + `CORS` (déjà en `${DOMAIN}` dans le compose).
- Le frontend est **la même image** : le placeholder `__SIGA_API_HOST__` est réécrit par Nginx
  en `https://192.168.0.120` → XHR même origine, **aucun rebuild spécifique**.
- QR des documents : défaut `DOCUMENTS_BASE_URL=https://ent.iss-gp.mr` (scannable partout). Pour
  pointer les QR vers le LAN, ajouter `DOCUMENTS_BASE_URL=https://192.168.0.120` au `.env`.
- `deploy/certs/` est **gitignoré** → le cert auto-signé ne part jamais sur GitHub.

---

## 📋 Récapitulatif express

```text
1. Droplet (Ubuntu 24.04, 4 Go, London, clé SSH)      → IP
2. DNS cPanel : A  ent → IP
3. Serveur : swap + ufw + Docker
4. Token GitHub (repo) → git clone siga + gesafped_frontend
5. .env (DOMAIN, SECRET_KEY, DB_PASSWORD, DB_ROOT_PASSWORD)
6. certbot --standalone (cert AVANT le 1er up) + hooks de renouvellement
7. docker compose up -d --build   → https://ent.iss-gp.mr 🔒
8. (scp cert de signature + media — Phase 7)
```
