# Plan — Migration MySQL 8.0 → PostgreSQL (SIGA) — 0 perte / 0 régression

## Context

SIGA (Django/DRF, système académique en production : notes, délibérations, documents officiels QR/signés, paie) tourne sur **MySQL 8.0** (`gesafped26` prod / `siga` local, 91 tables InnoDB, utf8mb4). On veut migrer vers **PostgreSQL** — socle plus adapté à Django, **DDL transactionnelle** (fini les migrations à moitié appliquées et le drift MyISAM/InnoDB déjà rencontrés), et surtout **réplication logique native** qui servira l'archi LAN→droplet read-only (les deux bouts doivent être PG ; on migre d'abord la BD source, puis on reconstruit le droplet sur PG).

**Le risque central à comprendre :** les ~276 tests tournent sur **sqlite `:memory:` avec `--no-migrations`** (`siga/settings/test.py`, `pytest.ini`). Ils prouvent la logique Python mais sont **aveugles au moteur** (collation/casse, arrondis `Decimal`, tables `managed=False`, SQL brut). **Une suite verte ≠ migration propre.** Le vrai filet de non-régression est un **harnais golden-dataset** qui compare MySQL vs PG sur un **snapshot prod réel**.

---

## Décision d'architecture (approche retenue)

**Copie des DONNÉES par `pgloader` ; SCHÉMA de vérité par les migrations Django ; réconciliation par `migrate --fake` ; garde-fou par un gate de fidélité de schéma.**

1. Construire un **schéma canonique PG** : sur une base vide, `migrate` (après correction des migrations bloquantes) → `pg_dump --schema-only` = schéma de référence.
2. **`pgloader`** copie schéma+données depuis un snapshot MySQL → PG neuve. Il capture **les 91 tables dont les 2 `managed=False`**, stream `core_audit_log` via `COPY`, fait les casts (tinyint→bool, json→jsonb, char(32)→uuid), et **contourne l'historique de migrations cassé**.
3. `manage.py migrate --fake` → marque toutes les migrations appliquées **sans exécuter** le DDL MySQL-only (donc les bloquantes ne s'exécutent jamais au cutover).
4. Réimplémenter les 2 triggers append-only en plpgsql ; **reset des séquences**.
5. **Gate fidélité schéma** : `pg_dump --schema-only` de la base migrée, normalisé, diffé contre le schéma canonique → doit être **vide** (sinon ajuster les règles de cast pgloader).

*Pourquoi pas `dumpdata/loaddata` :* bloqué par l'historique de migrations cassé + les 2 tables `managed=False`, lent et gourmand sur `core_audit_log`. *Fallback si un cast pgloader déraille :* `migrate` d'abord (schéma exact) + pgloader **data-only** dans le schéma pré-créé (avec `session_replication_role=replica`), ou `loaddata` ciblé table par table.

---

## Phase 1 — Durcissement AVANT de toucher la BD

### 1.1 Combler les trous de tests unitaires sur le chemin de calcul des notes (sur la suite sqlite rapide, via `tests/factories/` + `tests/conftest.py`)
- **`apps/evaluations/services/calcul_module.py` `ResultatModuleService`** — plus gros trou de calcul non testé : moyenne pondérée `Σ(note×coeff)/Σcoeff` + `quantize(0.01, ROUND_HALF_UP)`, précédence des coefficients, éliminatoire, `total_coeff==0`, matrice de codes E/V/VCI/VCS/NV, compensation `rafraichir_codes_apres_semestre`.
- **`_consolider_ies_semestre`** (`apps/documents/services.py:859`) — testé seulement indirectement ; tests directs de la consolidation dette/dernière-note.
- **Workflow `RachatNote`** — non testé.
Ces tests **figent les valeurs Decimal exactes** → oracle pour le run PG.

### 1.2 Réglages de test PostgreSQL + preuve « suite complète AVEC migrations »
- Créer **`siga/settings/test_pg.py`** (miroir de `test.py` mais `ENGINE=postgresql`, DB test via env, **sans** `OPTIONS.init_command`).
- Fixture conftest spécifique PG qui, **après migrations**, crée `prof_type_history` et `suivi_pointage_departements` en DDL PG (l'actuelle `tests/conftest.py:22` ne crée que la 1re, en sqlite).
- Commande de preuve (dépend de la Phase 2) :
  ```
  pytest --migrations --ds=siga.settings.test_pg   # rejoue TOUTES les migrations sur un vrai PG + tous les tests en sémantique PG
  ```
  À câbler en CI avec un service `postgres:16` (garder aussi le job sqlite rapide).

### 1.3 Pré-scan des collisions de casse (audit read-only)
MySQL `_ci` = insensible casse/accents ; PG = sensible → collisions d'unicité potentielles au load + changement de comportement des lookups. Commande **read-only** `core/management/commands/case_collision_scan.py` : pour chaque champ/`unique_together` texte, `GROUP BY LOWER(x) HAVING COUNT(*)>1`.
- Champs : `CustomUser.email`/`.username`, `Etudiant.matricule`, `DocumentOfficiel.numero_serie`, `RegistreDiplome.numero_diplome`, tous les `code` (modules/scolarite/auth), parametres (niveau/type_seance/creneau/jour/acronyme), `banque.nom`, `salle.nom` ; `unique_together` : `evaluations_session`, `suivi_chargeinstitution`, `em`, `paiement`.
- **Lookups exacts** devenus sensibles à corriger : `emplois/views.py:134 get(jour=)`, `inscriptions/views.py:83,344,646,754 filter(matricule=)`, `filter(username=)` (login : `authentication/views.py:434`, `prof/services.py:189`, `absence/views.py:392,452`), `Module/Action.objects.get(code=)` (migrations auth 0004/0007-0010).
- **Stratégie de casse retenue** (voir section dédiée plus bas).

---

## Phase 2 — Rendre TOUTES les migrations PostgreSQL-propres

Rendre vendor-neutres (brancher sur `schema_editor.connection.vendor`) les 5 bloquantes :

| Migration | Problème MySQL-only | Correctif |
|---|---|---|
| `apps/backup/migrations/0002_immutable_download_log_triggers.py` | triggers `SIGNAL SQLSTATE … BEGIN…END` | `RunPython` branché : plpgsql `RAISE EXCEPTION` + 2 triggers `BEFORE UPDATE/DELETE` sur PG ; texte MySQL sur mysql ; no-op sqlite |
| `apps/suivi/migrations/0005_create_chargeinstitution_table.py` | `CREATE TABLE` backticks/`AUTO_INCREMENT`/`ENGINE`/`CHARSET` | `state_operations` (CreateModel) + `RunPython` branché (`bigserial` sur PG) |
| `apps/departement/migrations/0007_fix_niveau_nullable.py` | `information_schema`+`DATABASE()`+`ALTER … MODIFY`, non gardé | ajouter `if conn.vendor != 'mysql': return` (drift MySQL-legacy inexistant sur PG neuf) |
| `apps/evaluations/migrations/0013_add_institution_fk.py` | `reverse_sql` `ALTER … DROP INDEX` | reverse vendor-neutre (`DROP CONSTRAINT` sur PG) ; le forward `ADD CONSTRAINT UNIQUE` passe déjà |
| `apps/modules/migrations/0004_myisam_to_innodb.py` | MySQL-only mais **déjà gardé** | vérifier no-op sur PG (aucun changement) |

**`managed=False`** (`prof_type_history`, `suivi_pointage_departements`) : copiées par pgloader (chemin primaire) ; créées par la fixture test_pg et par le fallback. **Commandes de migration one-shot MySQL-only** (`schema_diff`, `add_seance_jour_fk`, `backfill_legacy_fks`, etc.) : déjà exécutées sur le schéma live → **retirer ou garder derrière `vendor=='mysql'`** ; elles ne sont pas sur le chemin de cutover.

**Acceptation Phase 2** : `migrate` sur PG vide atteint la fin sans erreur ; `pg_dump --schema-only` = `canonical_schema.sql` ; premier run vert de `pytest --migrations --ds=siga.settings.test_pg`.

---

## Phase 3 — Harnais golden-dataset (cœur du 0-régression)

Deux commandes read-only sous `core/management/commands/` (tout recalcul dans une transaction **rollback**) :

### `golden_extract.py --out <f.json> --label <mysql|pg>`
**(A) Invariants métier** (via les **vrais services**, pas re-dérivés — capture les diffs de calcul, pas seulement de stockage) :
- **Par (`matricule`, `code_semestre`, session)** : `moyenne_semestre` ; par module `moyenne`+`code_statut`(V/VCI/VCS/NV/E)+`credits_valides` ; par EM `me`/`note_finale` ; `credits_valides_em/_total` ; `est_admis` ; `has_eliminatoire` ; `mention` ; `decision` — **valeur persistée ET recalculée** (`ResultatModuleService`/`calculer_resultat_semestre_consolide` en transaction annulée).
- **Par PV/`LigneDeliberation`** : `decision`, `decision_annuelle`, `credits_annuels`, `moyenne_annuelle`, `taux_capitalisation`, `verrou_passage`, `rang` + set `ObligationRattrapage`.
- **Par `DocumentOfficiel`** (clé = `numero_serie`) : `numero_serie`, `hash_sha256` (déterministe : `sha256(f'{numero}{matricule}{type}{annee}')`, services.py:1680), `token_verification`, **+ table de notes régénérée via `_build_context_releve`** + URL QR via `_get_qr_base64`. **Ne pas** appeler `_generer_pdf` (octets non déterministes : signature/horodatage).
- **`RegistreDiplome`** : liste ordonnée `(matricule, numero_diplome, mention, moyenne_generale, date_delivrance)`.
- **emplois/suivi/absence** : digests ligne-à-ligne (B) **+ signatures d'ordre** sur les requêtes triant du texte (collation PG ≠ MySQL).

**(B) Digests ligne-à-ligne** pour les **91 tables** : count + liste triée de `sha256(tuple_canonique(colonnes))`.

**Canonicalisation** (clé pour un diff propre inter-moteurs) : Decimal `quantize(0.0001)`→str ; bool→0/1 ; datetime→UTC ISO µs (attention `USE_TZ=True`/`Africa/Nouakchott`) ; UUID→forme tiretée minuscule ; JSON→`dumps(sort_keys=True)` ; texte **brut** (on VEUT voir les diffs de casse) ; NULL→sentinelle ; collections **triées par clé naturelle**.

### `golden_diff.py <mysql.json> <pg.json>`
Deep-diff, imprime chaque delta avec sa clé naturelle, **exit ≠ 0 sur tout écart**. Exécution sur **snapshot prod complet, jamais des factories**.

---

## Phase 4 — Changements ops/déploiement (préparés sur branche, inertes jusqu'au cutover)

- `requirements.txt:7` : `mysqlclient` → `psycopg[binary]`.
- `siga/settings/base.py` : `108` ENGINE→`postgresql` ; **supprimer `OPTIONS.init_command` (114-116)** ; `DB_PORT` 3306→5432. (`docker.py`/`production.py` héritent → 1 seul edit couvre tous les envs.)
- `apps/backup/services/generator.py:177-207` : `mysqldump`→`pg_dump` (creds via `PGPASSWORD`/env) ; `_verify_binary('mysqldump')`→`pg_dump` ; renommer `BACKUP_MYSQLDUMP_BIN`→`BACKUP_PGDUMP_BIN` (base.py:259). Empaquetage `.7z`/manifest/média inchangés.
- `deploy/docker-compose.yml` : `db.image mysql:8.0`→`postgres:16` ; env `MYSQL_*`→`POSTGRES_*` ; volume `/var/lib/mysql`→`/var/lib/postgresql/data` ; remplacer `command:` charset/collation par `POSTGRES_INITDB_ARGS`/locale ; healthcheck `mysql … SELECT 1 FROM semestre`→`pg_isready` + `psql -c 'SELECT 1 FROM semestre'` ; seed `dump.sql.gz` (postgres importe aussi `/docker-entrypoint-initdb.d/*.sql.gz`, garder **SQL brut gzippé**) ; `DB_PORT`→5432 ; **retirer `phpmyadmin`** (ou → `adminer`/pgAdmin).
- `deploy/deploy.sh` (`backup`/`init`/`reset-db`) + `deploy/backup-scripts/*.sh` (daily/weekly/monthly/cleanup/restore-test) : `mysqldump`→`pg_dump`, `mysql`→`psql`, `PGPASSWORD` ; `gzip`/`sha256`/`scan_backups`/cron inchangés. **Préserver les correctifs `.gitattributes` + cron backup déjà faits.**
- Regénérer `deploy/dump.sql.gz` en `pg_dump | gzip` (SQL brut) post-migration.

---

## Stratégie de casse (recommandation)
- **Champs d'identité** (`email`, `username`, `matricule`, `numero_serie`, `code`…) → **`citext`** (`CREATE EXTENSION citext`) : les lookups exacts existants **restent insensibles à la casse sans toucher au code applicatif** → 0 régression au niveau appli. Reflété dans modèles/migrations **avant** le schéma canonique (Phase 2).
- **Autres** → contrainte unique fonctionnelle `UniqueConstraint(Lower('champ'))` + normaliser les lookups en `__iexact` là où identifié.
- Valider par test_pg (login/matricule/jour/code insensibles à la casse OK).

---

## Phase 5 — Runbook d'exécution (dry-run staging PUIS cutover prod)

**Env** : PostgreSQL **16**, `ENCODING=UTF8`, collation **ICU** (locale FR) ou `C` (byte-exact) — les signatures d'ordre du harnais valident l'absence de régression d'ordre. Extension `citext` si retenue. `datetime` MySQL naïfs castés **en `Africa/Nouakchott`** vers `timestamptz` (piège majeur : mauvais tz = tous les horodatages décalés).

**Règles de cast pgloader (`siga.load`)** : `tinyint(1)`→`boolean` (46 col.) ; `unsigned`→`integer`/`bigint` (16 col., vérifier bornes) ; `json`→`jsonb` ; `char(32)`→`uuid` avec insertion des tirets (**cast le plus risqué, tester**) ; `datetime`→`timestamptz` (tz source) ; `decimal`→`numeric` (exact) ; **préserver la casse d'identifiant** pour `db_table='Seance'` (`\dt "Seance"`). Hooks : `session_replication_role=replica` pendant le load, puis **reset séquences** (`setval` sur toutes les PK serial).

**Dry-run staging (snapshot prod complet, répétable, obligatoire)** :
1. Restaurer le dernier backup prod dans MySQL staging → `case_collision_scan` → remédier.
2. `golden_extract --label mysql`.
3. Construire `canonical_schema.sql` (migrate sur PG vide).
4. `pgloader siga.load` → créer/confirmer les 2 tables managed=False + triggers plpgsql → `migrate --fake` → reset séquences.
5. **Intégrité** : counts par table MySQL==PG ; `VALIDATE CONSTRAINT` toutes FK (0 orphelin) ; chaque séquence `> MAX(id)` ; triggers append-only rejettent UPDATE/DELETE.
6. **Gate fidélité schéma** : diff `pg_dump --schema-only` vs canonique = vide.
7. `golden_extract --label pg` → `golden_diff` = **vide**.
8. `pytest --migrations --ds=siga.settings.test_pg` vert.
9. Checklist scénarios manuels (Phase 6).
10. **Chronométrer** (durée pgloader sur `core_audit_log` = fenêtre de maintenance). Répéter jusqu'à 5-9 propres.

**Cutover prod** : maintenance/read-only → backup MySQL final archivé → pgloader validé + gates 5-7 → **snapshot du volume PG** → basculer `DATABASES` (via env, Phase 4) + `migrate --fake` + restart → smoke test + checklist → lever la maintenance. **Garder l'ancien MySQL gelé N jours** (source de rollback).

> **Bascule env (implémentation retenue, ≠ « branche » du plan initial)** : la config est **pilotée par `DB_ENGINE`** (défaut `mysql` → comportement inchangé). Au cutover, poser **`DB_ENGINE=postgresql`** :
> - dans l'env applicatif (settings) **ET dans `deploy/.env`** — `deploy/deploy.sh init` persiste désormais `DB_ENGINE` dans le `.env` généré ;
> - ⚠️ **impératif backups** : les crons `backup-*.sh` relisent `DB_ENGINE` depuis l'env **ou `deploy/.env`**. Sans cette variable posée, ils retombent sur `mysqldump` dans un conteneur PostgreSQL → **tous les backups automatiques échouent silencieusement**. Vérifier une exécution `backup-daily.sh` juste après le cutover.
> - Valeur inconnue de `DB_ENGINE` → `ImproperlyConfigured` (refus explicite, pas de retombée silencieuse sur MySQL). Fichier `.env` édité sous Windows : le `\r` est nettoyé côté settings et scripts.

**Rollback** : PG est une *copie*, MySQL reste **intact et gelé** → rollback = revert des settings/deps vers MySQL + restart (aucune reconstruction si avant écritures PG). Règle : **tous les gates verts avant de lever la maintenance** (on ne cutover jamais sur un gate rouge).

---

## Phase 6 — Critères d'acceptation « 0 perte / 0 régression »
1. `pytest` (sqlite) vert **ET** `pytest --migrations --ds=siga.settings.test_pg` vert.
2. **golden-diff vide** sur snapshot prod complet (invariants + 91 digests).
3. Counts par table égaux MySQL==PG.
4. FK validées (0 orphelin) ; séquences `> MAX(id)` ; triggers append-only actifs.
5. Gate fidélité schéma vide.
6. **Checklist manuelle** identique à l'avant-migration : générer un relevé + scanner le QR (token/`hash_sha256`/`numero_serie` OK) ; recalcul délibération (semestre+annuelle) sur un PV connu ; attribuer un diplôme (numérotation append-only continue) ; backup chiffré manuel (`pg_dump`) qui se restaure ; count paie/charge (`ChargeInstitution`).

---

## Fichiers critiques
- `siga/settings/base.py` (ENGINE/OPTIONS — l'edit qui bascule tous les envs) + nouveau `siga/settings/test_pg.py`
- `apps/backup/migrations/0002_immutable_download_log_triggers.py` (triggers → plpgsql ; représentatif des 5 bloquantes)
- `apps/evaluations/services/calcul_module.py` (plus gros trou de test + invariants notes du harnais)
- `apps/documents/services.py` (`numero_serie`/`hash_sha256`/`token`/`_build_context_releve`/`_get_qr_base64` pour l'extract)
- `deploy/docker-compose.yml` + `apps/backup/services/generator.py` (mysqldump→pg_dump + stack `postgres:16`)
- Nouveaux : `core/management/commands/{case_collision_scan,golden_extract,golden_diff}.py`

## Vérification (commandes)
```bash
# Phase 2 : schéma canonique + preuve migrations sur PG
# (test_pg lit PG_TEST_DB/USER/PASSWORD/HOST/PORT dans l'env — mot de passe NON
#  codé en dur : exporter PG_TEST_PASSWORD avant de lancer.)
PG_TEST_DB=siga_canon PG_TEST_PASSWORD=… python manage.py migrate --settings=siga.settings.test_pg
pg_dump --schema-only --no-owner --no-privileges siga_canon > deploy/canonical_schema.sql  # versionné (whitelisté dans deploy/.gitignore)
PG_TEST_PASSWORD=… pytest --migrations --ds=siga.settings.test_pg

# Phase 5 : dry-run non-régression
python manage.py case_collision_scan
python manage.py golden_extract --label mysql --out mysql.json   # sur MySQL staging
pgloader siga.load                                               # MySQL -> PG
python manage.py migrate --fake                                 # aligner l'état Django
python manage.py golden_extract --label pg --out pg.json         # sur PG
python manage.py golden_diff mysql.json pg.json                 # DOIT être vide
```

> **Écarts d'ordre (collation) au golden_diff** : MySQL trie en `utf8mb4_*_ci`, PostgreSQL en ICU `fr-FR` (cf. `docker-compose.postgres.yml`) — tri linguistique proche mais **égalité stricte d'ordre non garantie**. `golden_diff` isole ces écarts dans une section **« ÉCARTS D'ORDRE (collation) »**, bloquante par défaut. Les examiner **un par un** ; s'ils sont tous des réordonnancements attendus (et non des lignes manquantes), relancer avec **`--allow-order-diff`** (exit 0 s'ils sont les seuls écarts) et **documenter la validation**. Les invariants métier et les digests de données restent, eux, en gate strict.

## Séquence d'exécution
1. Phase 1.1/1.3 (tests calcul + scan casse + remédiation)
2. Phase 2 (5 bloquantes PG-propres + choix citext/index)
3. Phase 1.2 (test_pg + 1er run vert `--migrations` PG + `canonical_schema.sql` + CI PG)
4. Phase 3 (golden_extract/diff)
5. Phase 4 (deps/settings/compose/backup sur branche, inertes)
6. Phase 5.3 (dry-run staging jusqu'à tous gates verts + timing connu)
7. Phase 5.4 (cutover prod + rollback safety)
8. Phase 6 (acceptation ; geler MySQL ; puis reconstruire le droplet sur PG + réplication logique)
