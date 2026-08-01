# Revision ANALYSE_SIGA_BACKEND.md — Compatibilite DB de l'approche Filiere/Groupe

## Contexte

Le fichier [ANALYSE_SIGA_BACKEND.md](ANALYSE_SIGA_BACKEND.md) (section 7) propose une refonte ou `Departement` encode aujourd'hui 3 dimensions (filiere + niveau + groupe) et serait complete par un nouveau modele `Filiere` (app `scolarite`) + un champ `groupe` sur `Departement`. Le besoin : verifier que cette evolution ne casse pas les donnees existantes, et consigner l'analyse directement dans le MD pour qu'elle serve de guide a la migration.

Il s'agit **uniquement d'une revision documentaire** — aucune modification de modele, migration, ou code d'app. Le seul fichier edite sera `ANALYSE_SIGA_BACKEND.md`.

## Verdict compatibilite (a inserer dans le MD)

**Resume** : l'approche est compatible avec la BD existante **au niveau schema** (toutes les additions sont nullable/blank), mais presente **3 risques concrets** qu'il faut expliciter dans le document.

### Ce qui ne casse PAS (additif pur)

Toutes les FK vers `Departement` restent valides puisque la table `departement` n'est ni renommee ni recree — on lui ajoute seulement `institution`, `filiere`, `groupe` (tous nullable/blank). Les FK concernees :

- [apps/absence/models.py:15](apps/absence/models.py#L15) — `Etudiant.departement` CASCADE
- [apps/em/models.py:10](apps/em/models.py#L10) — `EM.departement` CASCADE (+ `unique_together('code_em','departement')` [apps/em/models.py:13-16](apps/em/models.py#L13-L16))
- [apps/emplois/models.py:22](apps/emplois/models.py#L22) — `Emplois.departement` SET_NULL
- [apps/emplois/models.py:80](apps/emplois/models.py#L80) — `EmploisArchive.departement` SET_NULL
- [apps/suivi/models.py:24](apps/suivi/models.py#L24) — `Suivie.departement` SET_NULL
- [apps/vacation/models.py:7](apps/vacation/models.py#L7) — `Surveillance.departement` CASCADE
- [apps/vacation/models.py:19](apps/vacation/models.py#L19) — `Vacation.departements` M2M
- [apps/authentication/models.py:109](apps/authentication/models.py#L109) — `UserPermission.departement` SET_NULL

Les extensions `Etudiant` (+12), `EM` (+8), `Semestre` (+3), `Year` (+4) sont toutes nullable/default → zero impact sur les lignes existantes. Le nouveau modele `Filiere` et l'app `scolarite` creent de nouvelles tables isolees.

### Risque 1 — Donnees heterogenes dans `departement.nom`

Les lignes actuelles (section 7.1 du MD) melangent 3 schemas de nommage :

```
"SEA L1 - G1"    -> filiere=SEA, niveau=L1, groupe=G1      (format tiret + espace)
"SDID L2 G1"     -> filiere=SDID, niveau=L2, groupe=G1    (format sans tiret)
"SDID L2"        -> filiere=SDID, niveau=L2, groupe=?     (AMBIGU : tous les etudiants ou G0 ?)
"SDID"           -> filiere=SDID, niveau=?, groupe=?      (sans niveau — il y en a sur annee 2025-2026)
"HE" / "ST"      -> transversal, filiere=None, groupe=''  (OK)
"Statistique"    -> filiere=Stat, niveau=L3, groupe=''    (OK)
```

**Consequence** : une migration de donnees naive (regex `(\w+)\s*(L\d)?\s*-?\s*(G\d)?`) va **mal** classer les lignes "SDID" et "SDID L2". Il faut **auditer manuellement** ces lignes avant d'executer le backfill et decider case par case si une ligne represente un groupe ou l'ensemble d'une promotion.

**A ajouter dans le MD** : section "Pre-requis migration" avec la liste des lignes ambigues a corriger avant le backfill.

### Risque 2 — Rename `Institution.nom` -> `Institution.nom_fr`

Le MD section 7.2 indique `nom CharField(200)  # RENOMMER -> nom_fr`. Ce rename est **la seule modification non-additive** de la proposition.

Impact verifie :
- Aucun code applicatif (vues, serializers manuels) ne lit `institution.nom` directement.
- [apps/avancement/views.py:464](apps/avancement/views.py#L464) utilise `ci.institution.acronyme` — non affecte.
- [apps/parametres/serializers.py:61](apps/parametres/serializers.py#L61) expose `Institution` via `fields='__all__'` → le champ `nom` disparaitra de l'API automatiquement des le rename, et le frontend qui lit `institution.nom` cassera silencieusement.

**A ajouter dans le MD** : remplacer la formulation "RENOMMER" par une strategie en 2 temps :
1. **Ajouter** `nom_fr` comme nouveau champ et le backfiller depuis `nom` via data migration.
2. **Deprecation** : garder `nom` en `@property` pointant sur `nom_fr` le temps que le frontend migre, puis supprimer.

Cela evite toute rupture d'API.

### Risque 3 — CASCADE sur Etudiant/EM si des Departements sont fusionnes

Si, pendant la migration, l'equipe decidait de **fusionner** (ex: consolider "SDID" et "SDID L2" en une seule ligne), le `on_delete=CASCADE` sur `Etudiant.departement` et `EM.departement` supprimerait toutes les references liees. Et `unique_together('code_em','departement')` sur EM pourrait bloquer la reassignation si des doublons de `code_em` apparaissent apres reassignation.

**A ajouter dans le MD** : regle explicite "**ne jamais supprimer un Departement existant** pendant la migration — ajouter des colonnes, reassigner les FK via UPDATE, jamais DELETE". La strategie doit etre **in-place** : enrichir les lignes existantes avec `filiere` et `groupe`, pas les recreer.

## Modifications a apporter au fichier

Le fichier [ANALYSE_SIGA_BACKEND.md](ANALYSE_SIGA_BACKEND.md) sera edite ainsi :

### 1. Nouvelle sous-section **7.1.1 — Compatibilite avec la BD existante** (apres ligne 1066)

Inserer un bloc "Verdict + 3 risques" reprenant le contenu ci-dessus de maniere concise, avec :
- Tableau des FK vers `Departement` et leur `on_delete`
- Liste des lignes `departement.nom` ambigues a auditer
- Regle "in-place uniquement, pas de DELETE"

### 2. Correction section **7.2** (Institution.nom rename, ligne ~1079)

Remplacer le commentaire `# RENOMMER -> nom_fr` par la strategie additive en 2 temps (ajouter `nom_fr` + garder `nom` via @property pendant la transition).

### 3. Nouvelle sous-section **7.3.bis — Plan de migration de donnees** (apres ligne 1273)

Expliciter l'ordre des operations :
1. `makemigrations` additif (schema only, tous champs nullable)
2. Data migration : extraire les `code` distincts de `departement` -> creer les `Filiere`
3. Audit manuel des lignes ambigues ("SDID", "SDID L2")
4. Data migration : UPDATE `departement` SET filiere_id, groupe (jamais DELETE)
5. Data migration : Institution → copier `nom` vers `nom_fr`
6. Migration structurelle des `UserPermission.filiere` (nullable, safe)

Chaque etape est idempotente et reversible (sauf la suppression finale de `Institution.nom` qui arrive apres que le frontend ait migre).

## Fichiers critiques consultes

- [ANALYSE_SIGA_BACKEND.md](ANALYSE_SIGA_BACKEND.md) — sections 7.1, 7.2, 7.3
- [apps/departement/models.py](apps/departement/models.py) — etat actuel de `Departement` (6 champs, aucune contrainte unique)
- [apps/em/models.py](apps/em/models.py#L13-L16) — contrainte `unique_together`
- [apps/parametres/serializers.py:61](apps/parametres/serializers.py#L61) — serialiseur Institution auto (`fields='__all__'`)

## Verification

Une fois le MD mis a jour :
1. Relire la nouvelle section 7.1.1 pour verifier qu'elle repond bien a la question "est-ce que ca casse ?" avec un verdict clair et des risques traces par fichier:ligne.
2. Verifier que la formulation "RENOMMER" a disparu partout dans le MD.
3. Verifier que le plan de migration 7.3.bis est coherent avec l'ordre d'ajout nullable → backfill → enforcement.
4. Aucun code ni migration Django n'est execute — la revision reste purement documentaire.
