# Déploiement IPGEI — VPS Vultr, HTTP sur le port 9090

Cible : **http://208.85.19.82:9090**, sans TLS, sur une machine qui héberge
**déjà** deux piles en service (SIGA et mrtpay). Rien de ce qui suit ne les
touche.

---

## Ce que la machine porte déjà

| | |
|---|---|
| Système | Ubuntu 22.04.5 LTS · 2 vCPU · 3,8 Go RAM (2,2 libres) · swap 4,8 Go |
| Disque | 75 Go, 32 Go libres |
| Docker | 29.5.2 · Compose v5.1.4 |
| Occupé | `siga-*` (6 conteneurs, dont PostgreSQL 18.4) · `mrtpay-*` (5) |
| Ports pris | 22, 80 (nginx hôte), 5000, 5252, 8080, 8088 |
| **9090** | **libre** |

Le port 9090 n'étant pris par personne, la pile IPGEI le publie directement :
le Nginx de l'hôte n'a pas à être touché.

---

## Avant de commencer — les deux points à ne pas sauter

1. **Le dump de 146 étudiants est encore dans l'historique git.** Le purger
   (`git filter-repo --path deploy/dump.sql.gz --invert-paths`) avant de cloner
   quoi que ce soit sur un serveur exposé.
2. **`SECRET_KEY` a une valeur de repli** dans `siga/settings/base.py`. La
   retirer (`config('SECRET_KEY')` sans `default`), sinon un `.env` incomplet
   démarre en silence avec une clé publique.

---

## 1. Poser le code sur le serveur

Les images se construisent **sur le VPS** : les deux dépôts doivent donc y être,
côte à côte, dans l'arborescence attendue par le compose.

```bash
ssh iss@208.85.19.82
sudo mkdir -p /opt/ipgei /opt/backups && sudo chown -R $USER:$USER /opt/ipgei /opt/backups
cd /opt
git clone <depot-backend> ipgei
git clone <depot-frontend> frontend_ipgei
mkdir -p /opt/ipgei/media /opt/ipgei/secrets
```

Arborescence attendue — les chemins relatifs du compose en dépendent :

```
/opt/ipgei/deploy/     ← docker-compose.ipgei.yml, nginx.ipgei.conf, .env
/opt/ipgei/media/      ← fichiers déposés (photos, justificatifs)
/opt/ipgei/secrets/    ← certificat de signature des PDF
/opt/frontend_ipgei/   ← le dépôt Next.js
/opt/backups/          ← sauvegardes, lues aussi par l'écran Django
```

---

## 2. Construire les images — sur le VPS, une à la fois

**2 vCPU et 2,2 Go libres, partagés avec SIGA et mrtpay.** Un build Next.js
réclame à lui seul près de 2 Go : lancé en même temps que celui de Django, il
part en swap et peut se faire tuer par le noyau (`exit code 137`).

D'où la règle : **un service à la fois**, jamais les deux ensemble.

```bash
cd /opt/ipgei/deploy
free -h                                                   # état avant
docker compose -f docker-compose.ipgei.yml build backend  # ~5 min
docker compose -f docker-compose.ipgei.yml build frontend # ~10 min, le gourmand
docker images | grep ipgei
```

Si le build du frontend est tué malgré tout :

```bash
# Bornez le tas de Node sous la mémoire réellement libre
docker compose -f docker-compose.ipgei.yml build \
  --build-arg NODE_OPTIONS=--max-old-space-size=1536 frontend
```

Et si cela ne suffit toujours pas, ajoutez du swap le temps du build :

```bash
sudo fallocate -l 4G /swapfile2 && sudo chmod 600 /swapfile2
sudo mkswap /swapfile2 && sudo swapon /swapfile2
# … build …
sudo swapoff /swapfile2 && sudo rm /swapfile2
```

Après coup, récupérez la place prise par les couches intermédiaires :

```bash
docker builder prune -f      # ne touche à aucune image en service
```

> **Ne lancez jamais `docker system prune -a`** sur cette machine : il
> supprimerait aussi les images de SIGA et de mrtpay.

---

## 3. Le fichier de configuration

```bash
cd /opt/ipgei/deploy
cp .env.ipgei.example .env && chmod 600 .env
python3 -c "import secrets; print(secrets.token_urlsafe(64))"   # → SECRET_KEY
openssl rand -base64 24                                          # → DB_PASSWORD
nano .env
```

`PUBLIC_IP` et `PUBLIC_PORT` sont déjà remplis. Les deux secrets sont à coller.

---

## 4. Les données — la base de l'atelier est reprise

Le dump est **déjà produit et vérifié** :

```
dump-ipgei.sql.gz   765 647 octets
sha256              947af4114e47a5a1dc28a2bb52de0b63…
```

Il a été rechargé dans une base jetable, comparée table par table à la base
réelle : **125 tables des deux côtés, 27 154 lignes de part et d'autre, aucun
écart**, puis la jetable a été supprimée. Une sauvegarde qu'on n'a jamais
restaurée n'en est pas une.

```bash
scp dump-ipgei.sql.gz iss@208.85.19.82:/opt/ipgei/deploy/

# ── CONTRÔLER L'ARCHIVE, PAS SEULEMENT SA TAILLE ────────────────────────
# Leçon d'un échec réel : une première archive avait été compressée en
# redirigeant la sortie sous PowerShell, qui réencode le flux et détruit le
# binaire. Elle pesait un poids crédible, son empreinte était stable, et
# l'import s'est cassé sur « not in gzip format ». Pire : `restart: always`
# a relancé le conteneur, qui a trouvé un répertoire non vide et affiché
# « Skipping initialization » — l'échec a disparu derrière un conteneur sain,
# avec des tables créées par migrate et zéro ligne dedans.
#
# Sous Windows, compresser depuis Git Bash (`gzip -9 -c x.sql > x.sql.gz`),
# jamais avec la redirection `>` de PowerShell.
sha256sum /opt/ipgei/deploy/dump-ipgei.sql.gz   # 947af411…
gzip -t   /opt/ipgei/deploy/dump-ipgei.sql.gz && echo "ARCHIVE VALIDE"
```

Le compose le monte déjà. **Il doit être en place avant le premier `up`** : il
n'est lu qu'une fois, quand le volume est vierge.

> Ce qu'il contient : 2 038 notes, 166 inscriptions, 8 délibérations, 143
> étudiants. Ce qu'il ne contient pas : le calendrier, les emplois du temps, le
> suivi et le pointage — purgés à votre demande.
>
> **Ce fichier ne doit pas entrer dans git** : il porte des données réelles.

---

## 5. Démarrer

```bash
cd /opt/ipgei/deploy
docker compose -f docker-compose.ipgei.yml config --quiet   # syntaxe
docker compose -f docker-compose.ipgei.yml up -d
docker compose -f docker-compose.ipgei.yml ps
docker logs -f ipgei-backend                                # migrate puis gunicorn
```

Le premier démarrage prend une minute : la base s'initialise, `migrate` crée le
schéma, `collectstatic` rassemble les statiques, puis gunicorn prend la main.

**Vérifier que SIGA n'a pas bougé** — c'est la raison d'être des préfixes :

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | sort
# siga-* et mrtpay-* doivent afficher les mêmes Up qu'avant.
```

---

## 6. Le premier administrateur

```bash
docker exec -it ipgei-backend python manage.py createsuperuser
```

Sur une base reprise d'un dump, ce compte existe déjà : **changez son mot de
passe** plutôt que d'en créer un second.

---

## 7. Vérifications avant d'ouvrir aux utilisateurs

```bash
docker exec ipgei-backend python manage.py check --deploy
```

Sur ce déploiement, les avertissements `SECURE_SSL_REDIRECT`,
`SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` et `SECURE_HSTS_SECONDS` sont
**attendus** : c'est la conséquence directe du choix HTTP. Tout autre
avertissement, en revanche, mérite un examen.

À faire à la main, dans cet ordre :

- [ ] `http://208.85.19.82:9090` affiche l'écran de connexion
- [ ] Connexion, puis navigation dans le tableau de bord
- [ ] Onglet réseau : les appels partent bien vers `http://208.85.19.82:9090/api/…`
      — si vous y voyez `__SIGA_API_HOST__`, le `sub_filter` n'a pas opéré
- [ ] **Un PV de délibération en PDF et en Excel** — c'est le chemin qui met en
      jeu wkhtmltopdf, les polices et le sceau
- [ ] Un relevé de notes, une attestation, le classement S3
- [ ] Le logo de l'institution s'affiche **avant** connexion
- [ ] Cinq mots de passe faux → blocage 15 min et code 429
- [ ] `sudo reboot`, puis tout remonte seul (`restart: always`)

---

## 8. Sauvegardes

Le service n'a pas de valeur sans elles.

```bash
docker exec ipgei-db pg_dump -U ipgei --no-owner --no-privileges ipgei \
  | gzip > /opt/backups/ipgei-$(date +%F).sql.gz
```

À mettre en cron quotidien, puis — le point qui compte — **restaurer une fois
pour de vrai** dans une base jetable, et **copier les sauvegardes hors du VPS**.
Un instantané Vultr n'est pas une sauvegarde : il tombe avec la machine.

---

## Mettre à jour, plus tard

```bash
cd /opt/ipgei && git pull
cd /opt/frontend_ipgei && git pull

# Sauvegarder D'ABORD — une migration ne se reprend pas d'un clic
docker exec ipgei-db pg_dump -U ipgei --no-owner --no-privileges ipgei \
  | gzip > /opt/backups/avant-maj-$(date +%F).sql.gz

cd /opt/ipgei/deploy
docker compose -f docker-compose.ipgei.yml build backend    # un service
docker compose -f docker-compose.ipgei.yml build frontend   # puis l'autre
docker compose -f docker-compose.ipgei.yml up -d
docker builder prune -f
```

---

## Ce que ce déploiement n'a pas

À savoir, et à assumer :

- **Aucun chiffrement.** Mots de passe et cookies de session circulent en clair.
  Un tiers sur le réseau peut les lire et rejouer une session.
- **Pas de pare-feu ni de fail2ban** sur cette machine, et SSH est aux réglages
  par défaut. À reprendre — mais un `ufw enable` mal réglé couperait SIGA et
  mrtpay en même temps : à faire avec elles en tête.
- **Pas de supervision** : `lib/monitor.ts` est muet en production.
