# Plan — Refonte cohérence Départements SIGA

> Fichier de plan d'évolution. Date : 2026-05-13.
> Auteur : Dr. Ahmed SEJAD (assisté par Claude Code).

## Contexte

Le modèle `Departement` souffre d'une **redondance structurelle** : le champ
`nom` (saisi à la main, ex `"SEA L1 - G1"`) duplique de manière texte ce qui
est déjà encodé via les FK `filiere`, `niveau` et le `groupe` CharField. Cela
provoque plusieurs problèmes observés en BD prod :

1. **Préfixe "SEA" obsolète** : la filière s'appelle désormais "Statistiques"
   (`code='LPSTAT'`), mais les départements gardent l'ancien préfixe "SEA"
   (~6 départements concernés).
2. **9 départements avec `filiere=NULL`** : certains logiquement attendus
   (HE, ST = modules transversaux), d'autres potentiellement à corriger
   (`SEA L2`, `SDID*`).
3. **Doublon visible** : 2 entrées `"HE"` (id=12 et id=30).
4. **Format de nom incohérent** : variantes possibles (`SEA L1 G1`,
   `SEA-L1-G1`, etc.) — pas de garantie d'uniformité.
5. **Renommage non propagé** : si l'admin renomme une filière, les
   départements liés gardent leur ancien nom indéfiniment.
6. **Hardcode `['HE', 'ST']`** dans 8 fichiers frontend (filtres dans
   `/absences/*` et `/emplois/gerer`). Tout renommage de ces codes
   transverses casse 8 pages.

### Objectif

Garantir la **cohérence par construction** :
- Le `nom` d'un département devient **calculé automatiquement** depuis sa
  filière + son niveau + son groupe (formule `{filiere.code} - {niveau} - {groupe}`).
- L'admin ne saisit plus le nom — UI épurée avec aperçu temps réel.
- Renommage filière propagé automatiquement aux départements liés.
- Préfixe "SEA" remplacé par "LPSTAT" partout dans la donnée existante.
- Investigation du doublon HE avant toute action destructive.
- Remplacement du hardcode `['HE', 'ST']` par un flag `is_transversal`.

### Garanties analysées

**Aucune régression** sur scolarité / notes / PV / délibérations / documents :
- Les modules sensibles (`apps/evaluations/*`, `apps/inscriptions/*`,
  `apps/documents/*`) utilisent UNIQUEMENT des FK numériques
  (`filiere_id`, `niveau_id`, `etudiant_id`) — jamais `departement.nom`.
- Le `Departement.nom` est purement décoratif (libellé d'affichage).
- Aucun hardcode `'SEA'` n'existe dans le code (backend ou frontend).
- Le hardcode `['HE', 'ST']` (8 fichiers frontend) est traité Phase 6.

### Décisions utilisateur validées (2026-05-13)

1. **Préfixe cible** = `LPSTAT` (code complet, pas le raccourci `STAT`)
2. **SDID** (5 départements id=13,14,19,20,27) = **restent `filiere=NULL`**,
   pas de rattachement forcé à la filière LPSTAT
3. **Doublon HE** (id=12, id=30) = **investigation préalable obligatoire**
   en Phase 2, **pas de fusion automatique**. Décision DA après lecture
   du rapport.

---

## Architecture cible

```
┌─────────────────────────────────────────────────────────────────┐
│ BD                                                              │
│   Filiere (1)      ──┐                                          │
│   Niveau  (4-5)    ──┴── Departement                            │
│                          │                                       │
│                          ├─ nom (calculé auto à chaque save())  │
│                          ├─ filiere (FK)                        │
│                          ├─ niveau  (FK)                        │
│                          ├─ groupe  (CharField "G1")            │
│                          ├─ is_container (existant)             │
│                          └─ is_transversal (nouveau)            │
└─────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│ BACKEND                                                         │
│   • Departement.save() : compose nom = "{filiere.code}-{niveau} │
│     -{groupe}" si filiere+niveau, sinon préserve nom saisi      │
│   • Signal post_save sur Filiere : recalcule nom de tous les   │
│     départements liés (propagation renommage)                   │
│   • Migration data : recalcule nom des dept existants           │
└─────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│ FRONTEND                                                        │
│   • Page /departements/ajouter : champ "nom" supprimé, aperçu  │
│     temps réel généré côté frontend                             │
│   • Page /departements/[id] : nom en lecture seule              │
│   • Remplace ['HE','ST'] (8 fichiers) par d.is_transversal      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Phases

### Phase 0 — Audit data initial (0.5j)

**Objectif** : Capturer l'état exact de la BD avant tout changement.

**Actions** (lecture seule) :
- Script Django shell qui exporte les 32 départements dans
  `tests/data_audit/dept_state_before.json` (id, nom, filiere_id,
  filiere_code, niveau_id, niveau_label, groupe, annee, is_container).
- Identification des 3 catégories :
  - Dept "métier" avec filière (~6 à renommer SEA → LPSTAT)
  - Dept transversaux (HE, ST — à marquer `is_transversal=True`)
  - Dept admin/container (Scolarité, STATL1 — à laisser intact)
- Compte les `filiere_id = NULL` (~9 départements).

**Livrable** : `docs/audit_departements_2026-05-13.md` avec mapping
décision par décision.

**Critères de succès** :
- Snapshot JSON complet exporté
- Liste validée par la DA des départements à conserver, renommer, fusionner

---

### Phase 1 — Renommage SEA → LPSTAT (0.25j)

**Décision validée** : préfixe cible = **`LPSTAT`** (code complet de la
filière, pas le raccourci `STAT`).

**Objectif** : Harmoniser les noms historiques "SEA*" en "LPSTAT*".

**Fichier modifié** : aucun code, juste data (script one-shot).

**Action** : Script Django shell avec preview + confirmation + apply :
```python
candidats = Departement.objects.filter(nom__startswith='SEA')
# Preview diff (ex: "SEA L1 - G1" -> "LPSTAT L1 - G1")
# Confirmation interactive
# Apply : nom.replace('SEA', 'LPSTAT', 1)
```

**Sécurité** : `LIKE 'SEA%'` garantit qu'on ne touche pas à HE, ST, SDID,
Scolarité, STATL1.

**Critères de succès** :
- `SELECT COUNT(*) FROM departement WHERE nom LIKE 'SEA%'` = 0 après
- Aucun département non concerné modifié
- Log diff sauvegardé dans `docs/rename_sea_to_lpstat_2026-05-13.log`

**Réversibilité** : `UPDATE departement SET nom = REPLACE(nom, 'LPSTAT', 'SEA') WHERE nom LIKE 'LPSTAT%'`

---

### Phase 2 — Audit FK NULL + investigation doublon HE (0.5j)

**Décisions validées** :
- **SDID** (id=13, 14, 19, 20, 27) : **laisser avec filiere=NULL**
  (considérés comme administratifs/transversaux). Aucune liaison forcée
  à la filière LPSTAT.
- **Doublon HE** (id=12, id=30) : **NE PAS fusionner automatiquement**.
  Investigation préalable obligatoire avant toute décision.

**Objectif** : Investigation + documentation, pas d'action destructive.

**Actions** :

#### 2a — Investigation doublon HE (lecture seule)
Script Django shell qui liste pour chaque id (12 et 30) :
- Date de création
- Toutes les FK qui pointent vers cet id (Vacation, SuiviePointage,
  Etudiant, InscriptionAdministrative, et tous les modèles avec FK
  vers Departement)
- Volume de données (nb vacations, nb suivis, nb étudiants liés)
- Année universitaire dominante
- Différences de champs (niveau, groupe, annee, decalage, code)

Livrable : `docs/investigation_he_doublon_2026-05-13.md` avec
recommandation (fusion, renommage, ou statu quo). La décision finale
sera prise par la DA après lecture de ce rapport.

#### 2b — Confirmation que SDID restent NULL
Validation que les pages /absences/* et /emplois/gerer **n'affichent
pas** les SDID dans les listes de département (sinon ce serait
incohérent puisqu'ils n'ont pas de filière). Si elles les affichent,
décision : les marquer `is_transversal=True` en phase 6.

**Aucune modification BD dans cette phase** (sauf décision explicite de
la DA après lecture du rapport).

**Critères de succès** :
- Rapport investigation HE complet
- Mapping FK→id_30 et FK→id_12 exhaustif
- Décision DA documentée
- Comportement SDID dans les UIs caractérisé

---

### Phase 3 — Auto-calcul `Departement.nom` (1j)

**Objectif** : Le `nom` devient calculé automatiquement.

**Fichier modifié** : `apps/departement/models.py`

**Changement** :
```python
class Departement(models.Model):
    # ... champs existants

    def save(self, *args, **kwargs):
        """Génère nom auto si filière + niveau disponibles, sinon préserve."""
        if self.filiere_id and self.niveau_id:
            code = self.filiere.code or self.filiere.intitule_fr
            parts = [code, self.niveau.niveau]
            if self.groupe:
                parts.append(self.groupe)
            self.nom = ' - '.join(parts)
        # Sinon : conserve self.nom (cas admin/container/transversal)
        super().save(*args, **kwargs)
```

**Migration data idempotente** : recalculer `nom` pour tous les
départements avec filière+niveau (déclenche save() override).

**Tests pytest** (`tests/test_departement_auto_nom.py`) :
- `test_dept_nom_auto_calcule_avec_filiere_niveau_groupe`
- `test_dept_nom_preserve_si_filiere_null` (cas HE/ST/container)
- `test_dept_nom_preserve_si_niveau_null`
- `test_dept_save_idempotent` (2 appels = même résultat)
- `test_dept_groupe_vide_omis_du_nom`

**Critères de succès** :
- Tous les "LPSTAT*" auront format uniforme "LPSTAT - L1 - G1"
- HE, ST, STATL1, Scolarité, SDID gardent leur nom saisi
- Suite pytest verte (104 existants + 5 nouveaux)

---

### Phase 4 — Signal post_save Filiere → propagation (0.5j)

**Objectif** : Renommer une filière recalcule automatiquement le nom de
tous les départements liés.

**Fichiers** :
- Nouveau : `apps/scolarite/signals.py`
- Modifié : `apps/scolarite/apps.py` (charger le signal au ready())

**Logique** :
```python
@receiver(post_save, sender=Filiere)
def _propager_renommage_filiere(sender, instance, created, **kwargs):
    if created:
        return
    for d in Departement.objects.filter(filiere=instance, niveau__isnull=False):
        d.save()  # déclenche le save() override → recalcule nom
```

**Tests pytest** :
- `test_renommage_filiere_propage_aux_dept`
- `test_creation_filiere_ne_change_pas_dept_existants`
- `test_renommage_filiere_ne_touche_pas_dept_transversal`

**Critères de succès** :
- Modifier `Filiere.code` "LPSTAT" → autre valeur : tous les départements liés
  passent au nouveau préfixe
- HE/ST/container/SDID non impactés

---

### Phase 5 — Refonte UI ajout/édition département (1j)

**Objectif** : L'admin ne saisit plus le nom à la main.

**Fichiers modifiés** :
- `app/dashboard/departements/ajouter/page.tsx`
- `app/dashboard/departements/[id]/page.tsx` (si existe)

**Changements** :
- Suppression de l'input "Nom du département"
- Champs requis : Filière (select), Niveau (select), Groupe (input "G1")
- Aperçu temps réel calculé côté frontend qui affiche le futur `nom`
  ("LPSTAT - L1 - G1") avec note "généré automatiquement"
- Validation client : filière + niveau requis (groupe optionnel selon
  type de département)

**Critères de succès** :
- Création d'un nouveau dept en 3 sélections (vs 4 + texte aujourd'hui)
- Aperçu temps réel cohérent avec le futur `nom` en BD
- Édition : le champ `nom` est en read-only (affiché mais non éditable)

---

### Phase 6 — Refactor `is_transversal` (HE/ST + SDID) (0.75j)

**Objectif** : Supprimer le hardcode `['HE', 'ST']` dans 8 fichiers
frontend en passant à un flag BD `is_transversal`. Étendre aux SDID si
la phase 2 confirme qu'ils doivent être traités comme transversaux.

**Fichiers modifiés** :
- Backend :
  - `apps/departement/models.py` : ajout `is_transversal = BooleanField(default=False)`
  - `apps/departement/serializers.py` : exposer `is_transversal`
  - Nouvelle migration auto-générée + data migration qui marque :
    - HE (id=12 et éventuellement id=30 selon décision phase 2)
    - ST (id à confirmer phase 0)
    - SDID (id=13, 14, 19, 20, 27) — **si la phase 2 confirme** qu'ils
      doivent être exclus des listes département "métier"
- Frontend (8 fichiers) :
  - `app/dashboard/absences/fiches/page.tsx:126`
  - `app/dashboard/absences/importer/page.tsx:46`
  - `app/dashboard/absences/rapport/page.tsx:96`
  - `app/dashboard/absences/saisir/page.tsx:113`
  - `app/dashboard/absences/saisir/salle/page.tsx:81`
  - `app/dashboard/absences/stats/page.tsx:111`
  - `app/dashboard/emplois/gerer/page.tsx:200, 245`

**Diff type** :
```diff
- list.filter(d => !['HE', 'ST'].includes(d.nom))
+ list.filter(d => !d.is_transversal)
```

**Note importante** : actuellement les SDID sont AFFICHÉS dans les listes
de département (le frontend n'exclut que HE/ST). Si la DA confirme que
les SDID sont transversaux/administratifs (cohérent avec filiere=NULL),
les marquer `is_transversal=True` les masquera également de ces pages.
**À valider explicitement avant phase 6.**

**Tests pytest** :
- `test_dept_transversal_exclu_des_listes_absences`
- `test_dept_transversal_inclu_dans_duplication_emplois`
- `test_dept_sdid_traite_comme_transversal` (si décidé)

**Critères de succès** :
- 0 `grep "'HE'" app/` dans le frontend
- Migration data OK (HE, ST, optionnellement SDID, marqués `is_transversal=True`)
- Toutes les pages /absences/* fonctionnent identiquement
- Page /emplois/gerer : duplication transversale fonctionne

---

### Phase 7 — Tests + déploiement (0.5j)

**Tests automatisés** :
```bash
# Backend
cd /opt/siga/siga && .venv/Scripts/pytest tests/ --cov=apps.departement --cov=apps.scolarite

# Frontend
cd /opt/siga/gesafped_frontend && npx tsc --noEmit
```

**Tests manuels critiques (15 min)** :
1. Liste `/dashboard/departements` : noms harmonisés "LPSTAT - L1 - G1"
2. Création nouveau dept : aperçu temps réel fonctionne
3. Édition dept existant : nom recalculé automatiquement
4. Page `/dashboard/emplois/gerer` : sélecteur dept, duplication OK
5. Page `/dashboard/payement/attestation` : génération PDF cohérente
6. Page `/dashboard/absences/saisir` : HE et ST exclus correctement
7. Renommer une filière → vérifier propagation aux dept liés
8. Saisie note → ResultatElement créé → délibération OK (régression test)
9. Génération PV PDF → étudiants présents (régression test)

**Déploiement** :
```bash
./deploy.sh backup
git pull
docker compose build backend frontend
docker compose up -d --force-recreate backend frontend
docker compose exec backend python manage.py migrate --no-input
```

---

## Fichiers critiques

### Backend
| Fichier | Modification |
|---|---|
| `apps/departement/models.py` | + `save()` override (phase 3) + `is_transversal` field (phase 6) |
| `apps/departement/serializers.py` | + expose `is_transversal` |
| `apps/departement/migrations/` | 2 nouvelles migrations (auto-calcul + is_transversal) |
| `apps/scolarite/signals.py` | Nouveau — signal post_save Filiere |
| `apps/scolarite/apps.py` | + import signals au ready() |
| `tests/test_departement_auto_nom.py` | Nouveau — 8 tests pytest |

### Frontend
| Fichier | Modification |
|---|---|
| `app/dashboard/departements/ajouter/page.tsx` | Suppression champ "nom", aperçu temps réel |
| `app/dashboard/departements/[id]/page.tsx` | Idem édition + nom en read-only |
| `app/dashboard/absences/fiches/page.tsx` | `['HE','ST']` → `is_transversal` |
| `app/dashboard/absences/importer/page.tsx` | Idem |
| `app/dashboard/absences/rapport/page.tsx` | Idem |
| `app/dashboard/absences/saisir/page.tsx` | Idem |
| `app/dashboard/absences/saisir/salle/page.tsx` | Idem |
| `app/dashboard/absences/stats/page.tsx` | Idem |
| `app/dashboard/emplois/gerer/page.tsx` | Idem (2 occurrences) |

---

## Estimation

| Phase | Durée |
|---|---|
| 0 — Audit data | 0.5j |
| 1 — Rename SEA → LPSTAT | 0.25j |
| 2 — Audit FK NULL + investigation HE | 0.5j |
| 3 — Auto-calcul nom | 1j |
| 4 — Signal Filiere | 0.5j |
| 5 — Refonte UI ajout/edit | 1j |
| 6 — Refactor is_transversal | 0.75j |
| 7 — Tests + déploiement | 0.5j |
| **Total** | **5j** |

---

## Risques et mitigation

| Risque | Mitigation |
|---|---|
| Régression suite à recalcul auto du nom | Phase 0 audit + tests pytest exhaustifs phase 3 |
| Doublon HE : transfert FK incomplet (si fusion décidée) | Lister exhaustivement les modèles avec FK vers Departement avant fusion (Vacation, SuiviePointage, Etudiant, InscriptionAdmin) ; tester en preview |
| Renommage filière propage en cascade non voulue | Filtre `niveau__isnull=False` dans le signal pour ne pas toucher aux dept admin/transversaux |
| Décision SDID transversal ou non | Phase 2 caractérise le comportement actuel avant décision phase 6 |
| Migration is_transversal sur 32 dept | Migration data idempotente avec `update_or_create` |

---

## Plan de rollback

### Phases 1-2 (data only)
```bash
gunzip -c /opt/siga/backups/gesafped26_BEFORE.sql.gz | \
  docker compose exec -T db mysql -u root -p"$DB_ROOT_PASSWORD" gesafped26
```

### Phases 3-6 (code + migrations)
```bash
cd /opt/siga/siga
git revert <commit>
docker compose exec backend python manage.py migrate departement <previous>
docker compose up -d --force-recreate backend frontend
```

---

## Vérification end-to-end après déploiement

### SQL sanity checks
```sql
-- 1. Aucun "SEA*" résiduel
SELECT COUNT(*) FROM departement WHERE nom LIKE 'SEA%';   -- attendu : 0

-- 2. Format uniforme "LPSTAT - L*"
SELECT nom FROM departement WHERE filiere_id=1 ORDER BY nom;

-- 3. Pas de doublon (si fusion HE décidée)
SELECT nom, COUNT(*) FROM departement GROUP BY nom HAVING COUNT(*) > 1;

-- 4. SDID restent filiere=NULL (décision validée)
SELECT id, nom, filiere_id, is_transversal FROM departement WHERE nom LIKE 'SDID%';

-- 5. HE et ST marqués transversaux après phase 6
SELECT nom, is_transversal FROM departement WHERE nom IN ('HE', 'ST');  -- attendu : tous is_transversal=1
```

### Code sanity checks
```bash
# Aucun hardcode 'HE'/'ST' restant
grep -r "'HE'" app/ ; grep -r "'ST'" app/    # attendu : 0

# Tests pytest verts
pytest tests/ -v

# TypeScript sans erreur
npx tsc --noEmit
```

### Tests fonctionnels (régression scolarité)
1. Saisir 1 note → calcul ResultatElement OK
2. Peupler PV délibération → étudiants du dept renommé présents
3. Clore PV → progression générée
4. Générer relevé étudiant → données cohérentes
5. Générer attestation prof → filière "Statistiques" affichée

---

## Hors-périmètre

- Renommage de `Filiere.code` "LPSTAT" → "STAT" (sujet séparé, à valider DA)
- Création d'une filière SDID dédiée (si pertinent métier)
- Rattachement forcé des départements SDID à la filière LPSTAT
  (décision : ils restent filiere=NULL)
- Multilangue arabe des noms de départements (nécessite refactor i18n
  séparé)
- Refonte du modèle pour scinder "Departement annuel" vs "GroupeFiliereStable"
  (refonte architecturale plus ambitieuse, sprint dédié)
