# Plan d'implémentation — Système de traçabilité (AuditLog)

> **Statut** : Approuvé — option C de rétention validée le 2026-05-01.
> **Effort total estimé** : ~13 heures, réparties en 3 sprints.
> **Périmètre** : Backend Django (siga) + Frontend Next.js (gesafped_frontend).

---

## 1. Contexte

L'application possède déjà des fondations partielles :
- Modèle `AuditLog` append-only ([core/models.py:22](../core/models.py#L22))
- `AuditMixin` DRF qui log dans des fichiers ([core/mixins.py:9](../core/mixins.py#L9))
- Signals branchés sur 6 modèles `evaluations`/`documents` ([core/signals.py](../core/signals.py))

**Ce qui manque** :
1. Un middleware pour capturer l'utilisateur courant (sinon `user=None` partout)
2. La capture du **diff** `old → new` (le champ `changes` JSON est vide)
3. Les signals sur les modèles métier prioritaires (Suivie, SuiviePointage, Emplois, ChargeInstitution, Presence, Departement, Filiere, Prof, EM, Vacation, Institution, Etudiant, Inscription*, Reclamation, CustomUser)
4. Une politique de rétention (sinon la table grossit indéfiniment)
5. Une page frontend pour consulter l'historique
6. Une API REST pour exposer les données

**Objectif** : pouvoir répondre instantanément à des questions comme :
- *Qui a créé l'emploi du temps de la filière X ?*
- *Quelle est l'ancienne valeur de cette note avant modification ?*
- *Qui a marqué cette séance comme « Fait » et quand ?*
- *Quelles ont été les actions de l'utilisateur Y le 15 janvier ?*

---

## 2. Périmètre

### Modèles tracés (16 critiques)

| Domaine | Modèles | Actions |
|---------|---------|---------|
| **Emplois du temps** | `Emplois`, `EmploisArchive` | Create / Update / Delete + bulk archiver/restaurer |
| **Suivi pédagogique** | `Suivie`, `SuiviePointage` | Toggle commentaire, modifs, réclamations |
| **Charges horaires** | `ChargeInstitution`, `Vacation`, `Surveillance` | Create / Update / Delete |
| **Absences** | `Presence` | Statut, justificatif |
| **Évaluations** (déjà câblé, à enrichir) | `Note`, `PVDeliberation`, `LigneDeliberation`, `RachatNote` | Create / Update / Delete |
| **Référentiel** | `Departement`, `Filiere`, `Prof`, `EM`, `Etudiant` | Modifications structurelles |
| **Inscriptions** | `InscriptionAdministrative`, `InscriptionPedagogique`, `InscriptionElement` | Create / Update / Delete |
| **Réclamations** | `Reclamation` | Workflow complet |
| **Configuration** | `Institution` | Modifications globales |
| **Documents** (déjà câblé) | `DocumentOfficiel`, `RegistreDiplome` | Create / Update |
| **Sécurité** | `CustomUser` | Login / Logout / Password change / Permission denied |

### Hors-scope
- Tables référentielles immuables : `Jour`, `Creneau`, `Seance`, `Niveau`, `Annee`, `Semestre`
- Lectures (GET) — uniquement les mutations sont tracées
- `Notification`, `AuditLog` lui-même
- `Semaine` (généré en batch, audit agrégé suffit)

---

## 3. Architecture technique

### 3.1 Capture en 3 couches

```
┌──────────────────────────────────────────────────────────────────┐
│  Couche 1 : AuditMiddleware (NEW)                                │
│  Capture par requête : user, IP, user_agent, request_id          │
│  Stockage thread-local accessible par les signals                │
└──────────────────┬───────────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────────┐
│  Couche 2 : pre_save signal (NEW)                                │
│  Charge l'instance actuelle depuis la DB                         │
│  Stocke dans thread-local : _old_values[(model, pk)] = {...}     │
└──────────────────┬───────────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────────┐
│  Couche 3 : post_save / post_delete signals (ENRICHIR)           │
│  Calcule diff = {champ: {old, new}}                              │
│  Filtre les champs blacklistés                                   │
│  Écrit AuditLog via transaction.on_commit() → 0 latence          │
└──────────────────────────────────────────────────────────────────┘
```

**Garantie de performance** : `transaction.on_commit()` → l'INSERT audit s'exécute **après** le commit de la transaction principale. L'utilisateur ne perçoit aucune latence ajoutée.

### 3.2 Schéma `AuditLog` étendu

Migration pour ajouter 4 colonnes :

```python
class AuditLog(models.Model):
    # === Existants ===
    user        = FK(CustomUser, SET_NULL, null=True)
    action      = CharField(max_length=20, choices=ACTION_CHOICES)
    model_name  = CharField(max_length=100)
    object_id   = CharField(max_length=50)
    changes     = JSONField(default=dict)
    ip_address  = GenericIPAddressField(null=True, blank=True)
    user_agent  = TextField(blank=True, default='')
    timestamp   = DateTimeField(auto_now_add=True, db_index=True)

    # === À AJOUTER ===
    institution  = FK(Institution, SET_NULL, null=True)   # scoping multi-institution
    request_id   = CharField(max_length=36, blank=True)   # corrélation requête HTTP
    label        = CharField(max_length=200, blank=True)  # libellé humain
    endpoint     = CharField(max_length=200, blank=True)  # /api/v1/suivi/...
    http_method  = CharField(max_length=10, blank=True)   # PATCH, POST...
    keep_forever = BooleanField(default=False)            # immune à la purge
```

**Actions élargies** :
- `CREATE`, `UPDATE`, `DELETE` (existants)
- `BULK_CREATE`, `BULK_UPDATE`, `BULK_DELETE` (nouveau, pour opérations massives)
- `LOGIN_SUCCESS`, `LOGIN_FAILED`, `LOGOUT`, `PASSWORD_CHANGED`, `PASSWORD_RESET`
- `PERMISSION_DENIED`
- `ARCHIVE`, `RESTORE` (pour archiver_emplois / restaurer_depuis_archive)

**Index ajoutés** :
```python
indexes = [
    Index(fields=['model_name', 'object_id', '-timestamp']),  # historique entité
    Index(fields=['user', '-timestamp']),                     # par utilisateur
    Index(fields=['action', '-timestamp']),                   # par action
    Index(fields=['institution', '-timestamp']),              # scoping
]
```

### 3.3 Format JSON du diff

```json
{
  "commentaire": {"old": "Non fait", "new": "Fait"},
  "taux_paiement": {"old": 0, "new": 750},
  "prof": {
    "old": {"id": 42, "label": "Ahmed BAKAR"},
    "new": {"id": 17, "label": "Salem BAMBA"}
  }
}
```

**Champs FK** : on stocke aussi le label humain (`__str__` du modèle référencé) pour affichage immédiat sans nouvelle requête.

**Blacklist globale** (jamais dans le diff) :
- `id`, `pk`
- `created_at`, `updated_at`, `date_creation`, `date_modification`
- Mots de passe, hashs
- Champs binaires (juste le nom du fichier)

### 3.4 Agrégation pour les bulk

Décorateur `@audit_aggregate(label='...')` qui :
- Désactive temporairement les signals pendant le bloc
- Génère **un seul** AuditLog avec un résumé

**Cas d'usage** :

| Opération | Avant (signals naïfs) | Après (avec agrégation) |
|-----------|----------------------|--------------------------|
| `ajouter_suivie` (génère 200 Suivie + 30 SuiviePointage + M2M) | 230+ AuditLog | **1** AuditLog `BULK_CREATE` `label='Génération suivi semaine 3 — ISS'` `changes={count_suivies: 200, count_pointages: 30}` |
| `bulk_update_commentaires` (toggle 50 séances) | 50 AuditLog | **1** AuditLog `BULK_UPDATE` `changes={count: 50, transition: 'Non fait → Fait'}` |
| `archiver_emplois` (88 archives) | 88 AuditLog | **1** AuditLog `ARCHIVE` |
| `restaurer_depuis_archive` (88 emplois) | 88 AuditLog | **1** AuditLog `RESTORE` |

---

## 4. Rétention — Option C (validée)

### 4.1 Schéma à 3 paliers

```
┌──────────────────────┐         ┌──────────────────────┐         ┌──────────────────────┐
│  HOT (0 à 90 jours)  │ ──cron▶│  ARCHIVE (90-365 j)  │ ──cron▶│  JSON sur disque     │
│  core_audit_log      │ hebdo   │  core_audit_log_     │ mensuel │  /backups/audit/     │
│                      │         │  archive             │         │  YYYY-MM.json.gz     │
│  ~100K lignes        │         │  ~275K lignes        │         │  ~80 MB / an         │
│  ~135 MB             │         │  ~415 MB             │         │  Compressé           │
│  Recherche < 50 ms   │         │  Recherche < 500 ms  │         │  Manuel (grep, jq)   │
│  Indexable, paginé   │         │  Indexable, paginé   │         │                      │
└──────────────────────┘         └──────────────────────┘         └──────────────────────┘
```

### 4.2 Configuration paramétrable

`siga/settings/base.py` :
```python
AUDIT_RETENTION = {
    'HOT_DAYS':     90,
    'ARCHIVE_DAYS': 365,    # total = 90 hot + 275 archive
    'EXPORT_PATH':  BASE_DIR / 'backups' / 'audit',
    'EXPORT_FORMAT': 'jsonl.gz',  # 1 ligne JSON par événement, compressé gzip
}
```

### 4.3 Commandes de maintenance

#### `archive_audit_logs` — hebdomadaire (lundi 03h)
```bash
python manage.py archive_audit_logs
```

**Logique** :
1. `SELECT *` lignes de `core_audit_log` où `timestamp < now() - 90 days` ET `keep_forever = False`
2. Transaction atomique :
   - `INSERT INTO core_audit_log_archive ... SELECT ...`
   - `DELETE FROM core_audit_log WHERE id IN (...)`
3. Log : `Archived N rows for period before YYYY-MM-DD`

**Cron Linux** (à documenter) :
```cron
0 3 * * 1 cd /path/to/siga && /path/.venv/bin/python manage.py archive_audit_logs
```

**Cron Windows** (Planificateur de tâches) :
```cmd
schtasks /create /tn "SIGA Archive Audit" /sc weekly /d MON /st 03:00 /tr "C:\path\.venv\Scripts\python.exe C:\path\siga\manage.py archive_audit_logs"
```

#### `purge_audit_logs` — mensuel (1er du mois 04h)
```bash
python manage.py purge_audit_logs
```

**Logique** :
1. Pour chaque mois M dont les lignes ont > 365 jours :
   - Export `audit_YYYY-MM.jsonl.gz` dans `EXPORT_PATH`
   - Vérifier que le fichier existe et taille > 0
   - **Si OK** : `DELETE FROM core_audit_log_archive WHERE timestamp < now() - 365 days AND keep_forever = False`
   - **Si KO** : log erreur + abandon (les données restent en BD)
2. Log : `Exported and purged N rows from month YYYY-MM`

**Sécurité** :
- L'export précède la suppression — jamais l'inverse
- Vérification de l'intégrité du fichier avant DELETE
- `keep_forever = True` → jamais touché

#### `restore_audit_logs --month=2025-04`
Commande d'urgence pour réinjecter un mois exporté en cas de besoin (audit légal).

### 4.4 UX consultation

Page `/dashboard/historique` :
- Par défaut : **HOT uniquement** (90 derniers jours, instantané)
- Toggle « Inclure archive (jusqu'à 1 an) » → recherche aussi dans `core_audit_log_archive`
- Au-delà de 1 an : message « Pour les données plus anciennes, contacter l'admin (fichiers JSON disponibles) »

API :
```
GET /api/v1/audit/?include_archive=true&from=2025-01-01
```

### 4.5 Volume estimé (stable dans le temps)

| Palier | Lignes | Taille | Croissance |
|--------|--------|--------|------------|
| HOT (`core_audit_log`) | ~100 K | ~135 MB | **Stable** (90 jours roulants) |
| ARCHIVE (`core_audit_log_archive`) | ~275 K | ~415 MB | **Stable** (275 jours roulants) |
| JSON sur disque | linéaire | ~80 MB/an gzip | Augmente, mais sur disque pas en BD |
| **Total BD** | **~375 K** | **~550 MB** | **Stable** quelles que soient les années |

---

## 5. API REST

| Endpoint | Méthode | Permission | Description |
|----------|---------|------------|-------------|
| `/api/v1/audit/` | GET (list) | admin / IT | Liste paginée filtrable |
| `/api/v1/audit/{id}/` | GET | admin / IT | Détail d'une entrée |
| `/api/v1/audit/by-entity/?model=X&object_id=Y` | GET | admin / IT / propriétaire | Historique d'une entité |
| `/api/v1/audit/stats/` | GET | admin | Agrégations (par user, par jour, par module) |
| `/api/v1/audit/export/` | GET | admin | Export CSV streamé |

**Filtres pris en charge** sur la liste :
- `?user=X`
- `?model=Suivie`
- `?action=UPDATE`
- `?from=2026-01-01&to=2026-04-30`
- `?include_archive=true`
- `?search=texte` (recherche dans `label` + `changes`)

**Permissions** :
- `admin`, `IT` : tout voir
- Enseignant : peut voir l'historique de **ses propres** pointages, notes, vacations (via `?model=...&object_id=...` filtré sur `prof_id = request.user.prof_profile.id`)
- Étudiant : peut voir l'historique de **ses propres** notes et inscriptions

**Pas de POST/PUT/DELETE** : la table est append-only, lecture seule via API.

---

## 6. Frontend

### 6.1 Page principale `/dashboard/historique` (admin/IT)

Conformément au [design system](../../gesafped_frontend/docs/skill_design.md) :

```
┌──────────────────────────────────────────────────────────────────┐
│ 🕐 Historique des actions                            [Export CSV]│
├──────────────────────────────────────────────────────────────────┤
│ [Filtres]                                                        │
│  Utilisateur: [Tous ▾]  Module: [Tous ▾]  Action: [Tous ▾]     │
│  Du: [____] Au: [____]  Recherche: [_______________________]    │
│  ☐ Inclure archive (90j-1an)                                    │
├──────────────────────────────────────────────────────────────────┤
│ Date              │ User      │ Action       │ Cible    │       │
│ 2026-05-01 14:32  │ ahmed     │ 🟡 MODIF    │ SP#42   │ [Voir] │
│ 2026-05-01 14:31  │ sejad     │ 🟢 CRÉE     │ Em#88   │ [Voir] │
│ 2026-05-01 14:30  │ khadjetou │ 🟡 MODIF    │ Su#199  │ [Voir] │
│ 2026-05-01 14:29  │ ahmed     │ 🔵 LOGIN    │ —        │ [Voir] │
│ 2026-05-01 14:28  │ sejad     │ ⚪ BULK     │ Em(88)  │ [Voir] │
└──────────────────────────────────────────────────────────────────┘
                                                  < 1 2 3 ... 47 >
```

**Codes couleur** (selon design system) :
- 🟢 CREATE → vert (`#006633`)
- 🟡 UPDATE → jaune (`#E5C018`)
- 🔴 DELETE → rouge (`#C82020`)
- 🔵 LOGIN/LOGOUT → bleu
- ⚪ BULK_* → gris

### 6.2 Drawer détail (clic « Voir »)

```
┌────────────────────────────────────────────────────┐
│ × Modification — SuiviePointage #42                │
├────────────────────────────────────────────────────┤
│ Par     : Ahmed SEJAD (admin)                      │
│ Le      : 2026-05-01 14:32:18                      │
│ IP      : 192.168.1.42                             │
│ Endpoint: PATCH /api/v1/suivi/pointages/42/        │
│                                                    │
│ Champs modifiés (1) :                              │
│ ┌────────────────────────────────────────────────┐│
│ │ commentaire                                    ││
│ │   Avant : ┌─────────────┐                      ││
│ │           │ "Non fait"  │                      ││
│ │           └─────────────┘                      ││
│ │   Après : ┌─────────────┐ ✓                    ││
│ │           │ "Fait"      │                      ││
│ │           └─────────────┘                      ││
│ └────────────────────────────────────────────────┘│
│                                                    │
│ Contexte de la séance :                            │
│   Prof   : Ahmed SEJAD                             │
│   EM     : Introduction à la sécurité informatique │
│   Salle  : INFO4                                   │
│   Jour   : Lundi · 09h45-11h15                     │
└────────────────────────────────────────────────────┘
```

### 6.3 Composant `<AuditTimeline entity="..." id="..." />` réutilisable

Intégré sur les pages clés via un bouton 🕐 :
- `/dashboard/suivi/remplissage` : sur chaque pointage
- `/dashboard/emplois/gerer` : sur chaque emploi
- `/dashboard/suivi/charges` : sur chaque charge
- `/dashboard/profs` : sur chaque prof
- `/dashboard/em` : sur chaque EM

Chargement asynchrone via API filtrée :
```
GET /api/v1/audit/by-entity/?model=SuiviePointage&object_id=42
```

Présentation chronologique inversée (récent en haut), pagination interne si > 20 événements.

### 6.4 Composants standards utilisés

Conformément au design system :
- `<Pagination>` (existant)
- `.data-table` (CSS global existant)
- `<ConfirmModal>` (pas nécessaire — pas d'action destructrice)
- `flash.ts` pour les toasts d'erreur

---

## 7. Plan d'exécution — 3 sprints

### Sprint 1 — Backend capture (≈ 5h)

| # | Tâche | Effort | Fichiers concernés |
|---|-------|--------|---------------------|
| 1 | Migration `AuditLog` étendu (institution, request_id, label, endpoint, http_method, keep_forever) | 30 min | `core/migrations/0002_*.py` |
| 2 | `AuditMiddleware` (set thread-local user/IP/UA/request_id) + l'ajouter dans `MIDDLEWARE` | 30 min | `core/middleware.py`, `siga/settings/base.py` |
| 3 | Refactor `core/signals.py` : helpers génériques `_compute_diff()`, `_serialize_value()`, `_write_audit_safe()` | 2h | `core/signals.py` |
| 4 | Brancher signals sur les 16 modèles métier | 1h | `core/signals.py` |
| 5 | Décorateur `@audit_aggregate(label='...')` + appliquer sur `ajouter_suivie`, `archiver_emplois`, `restaurer_depuis_archive`, `bulk_update_commentaires` | 1h | `core/audit_helpers.py`, `apps/suivi/views.py`, `apps/emplois/services/emplois_service.py` |
| 6 | Signals `user_logged_in` / `user_logged_out` / `user_login_failed` / password change | 30 min | `core/signals.py` |

**Validation Sprint 1** :
- Faire un toggle pointage → vérifier 1 ligne dans `core_audit_log` avec user, IP, diff `{commentaire: {old: 'Non fait', new: 'Fait'}}`
- Faire un `ajouter_suivie` → vérifier **1 seule** ligne `BULK_CREATE` (et pas 200)
- Faire un login → vérifier 1 ligne `LOGIN_SUCCESS`
- `python manage.py check` propre

### Sprint 2 — Backend API + Frontend (≈ 5h)

| # | Tâche | Effort | Fichiers concernés |
|---|-------|--------|---------------------|
| 7 | `AuditLogViewSet` (read-only, filtres complets) + serializer + permissions | 1h | `apps/audit/` (nouveau) ou `core/views.py` |
| 8 | URL routing `/api/v1/audit/` | 15 min | `siga/urls.py` |
| 9 | Page frontend `/dashboard/historique` (liste + filtres) | 2h | `app/dashboard/historique/page.tsx` |
| 10 | Drawer détail (clic « Voir ») | 1h | composant local |
| 11 | Composant réutilisable `<AuditTimeline>` | 1h | `components/AuditTimeline.tsx` |
| 12 | Bouton 🕐 sur 5 pages clés | 1h | pages dashboard |

**Validation Sprint 2** :
- Ouvrir `/dashboard/historique` (en tant que admin) → liste paginée, filtres fonctionnels
- Cliquer « Voir » sur une entrée → drawer avec diff old/new lisible
- Sur une page suivi, cliquer 🕐 → timeline filtrée sur l'entité
- Test permission : un enseignant ne peut voir que ses propres pointages

### Sprint 3 — Rétention & maintenance (≈ 3h)

| # | Tâche | Effort | Fichiers concernés |
|---|-------|--------|---------------------|
| 13 | Modèle `AuditLogArchive` + migration | 30 min | `core/models.py`, migration |
| 14 | Commande `archive_audit_logs` (déplace HOT > 90j vers ARCHIVE) | 1h | `core/management/commands/archive_audit_logs.py` |
| 15 | Commande `purge_audit_logs` (export JSON + DELETE > 365j) | 1h | `core/management/commands/purge_audit_logs.py` |
| 16 | Commande `restore_audit_logs --month=YYYY-MM` (urgence) | 30 min | `core/management/commands/restore_audit_logs.py` |
| 17 | Setup cron Linux + script Windows planificateur | 30 min | `docs/AUDIT_CRON.md` |
| 18 | Documentation `docs/AUDIT.md` (architecture, requêtes courantes, dépannage) | 30 min | `docs/AUDIT.md` |

**Validation Sprint 3** :
- Backfiller des données fictives à > 90 jours → lancer `archive_audit_logs` → vérifier déplacement
- Backfiller des données fictives à > 365 jours → lancer `purge_audit_logs` → vérifier fichier JSON créé + suppression
- Lire le fichier JSON avec `zcat | jq` → format exploitable
- Lancer `restore_audit_logs --month=2025-04` → vérifier réinjection en archive

---

## 8. Performance — garanties

| Mesure | Garantie |
|--------|----------|
| Latence ajoutée par mutation utilisateur | **< 1 ms** (transaction.on_commit asynchrone) |
| Recherche historique d'une entité | **< 5 ms** (HOT) / < 50 ms (avec ARCHIVE) |
| Liste paginée 20 lignes filtrée | **< 50 ms** (HOT) / < 500 ms (avec ARCHIVE) |
| Stats agrégées sur 1 mois | **< 200 ms** |
| Volume DB | ~550 MB stable (HOT + ARCHIVE) |
| Volume disque (JSON) | ~80 MB / an compressé |
| Backup DB full | inchangé (~1 min sur 1.5 GB total) |

**Pas d'impact perçu** par l'utilisateur final.

---

## 9. Validation E2E (post-implémentation)

### Test 1 — Toggle pointage
1. Connexion `sejad`
2. `/dashboard/suivi/remplissage` → toggle un pointage de "Non fait" à "Fait"
3. Vérifier dans `core_audit_log` :
   ```sql
   SELECT * FROM core_audit_log ORDER BY timestamp DESC LIMIT 1;
   ```
   Doit contenir : `user_id=sejad`, `action='UPDATE'`, `model_name='SuiviePointage'`, `changes='{"commentaire": {"old": "Non fait", "new": "Fait"}}'`, `ip_address`, `user_agent`

### Test 2 — Génération suivi (bulk)
1. `/dashboard/suivi/ajouter` → générer la semaine N+1
2. Vérifier dans `core_audit_log` : **1 seule** ligne avec `action='BULK_CREATE'`, `label='Génération suivi semaine N+1'`, `changes={count_suivies: 200, count_pointages: 30}`

### Test 3 — Historique d'une entité
1. `/dashboard/suivi/remplissage` → cliquer 🕐 sur un pointage
2. Drawer s'ouvre avec timeline
3. Vérifier les 3 dernières actions affichées avec diffs

### Test 4 — Permissions
1. Connexion `ahmed` (DE, pas admin)
2. Tentative d'accès à `/dashboard/historique` → redirect ou 403
3. Tentative d'accès à `/api/v1/audit/` → 403

### Test 5 — Rétention
1. `INSERT INTO core_audit_log (timestamp, ...) VALUES ('2025-12-01', ...)` (fake old data)
2. `python manage.py archive_audit_logs`
3. Vérifier déplacement vers `core_audit_log_archive`
4. `python manage.py purge_audit_logs`
5. Vérifier fichier `/backups/audit/2025-12.jsonl.gz` créé

---

## 10. Questions résolues / par défaut

| Question | Décision |
|----------|----------|
| Périmètre des modèles | **16 modèles métier** (Suivie, SuiviePointage, Emplois, ChargeInstitution, Presence, Departement, Filiere, Prof, EM, Vacation, Institution, Etudiant, Inscription*, Reclamation, CustomUser, + Note/PVDeliberation/LigneDeliberation/RachatNote déjà câblés) |
| Capture du diff | **Diff complet** sauf champs auto/techniques blacklistés. FK avec label humain. |
| Permissions | Admin/IT page globale ; propriétaire pour ses propres données via bouton contextuel |
| Login tracking | **Oui** — tous les événements auth (success, failed, logout, password change) |
| Rétention | **Option C : 90j HOT + 365j ARCHIVE + JSON disque indéfini** ⭐ |
| Bulk operations | **Agrégation** : 1 AuditLog par opération bulk (pas 1 par ligne) |
| Export | CSV streamé (`/api/v1/audit/export/`) |
| Ordre d'exécution | **3 sprints** indépendants, validables individuellement |

---

## 11. Fichiers à créer / modifier

### Nouveaux fichiers (Sprint 1)
- `core/middleware.py` (AuditMiddleware)
- `core/audit_helpers.py` (decorator @audit_aggregate, _compute_diff)
- `core/migrations/0002_audit_log_extended.py`

### Nouveaux fichiers (Sprint 2)
- `apps/audit/__init__.py`, `apps/audit/views.py`, `apps/audit/serializers.py`, `apps/audit/urls.py`, `apps/audit/permissions.py`
- `app/dashboard/historique/page.tsx` (frontend)
- `components/AuditTimeline.tsx` (frontend)
- `lib/api/audit.ts` (frontend client)

### Nouveaux fichiers (Sprint 3)
- `core/models.py` : ajouter `AuditLogArchive`
- `core/migrations/0003_audit_log_archive.py`
- `core/management/commands/archive_audit_logs.py`
- `core/management/commands/purge_audit_logs.py`
- `core/management/commands/restore_audit_logs.py`
- `docs/AUDIT.md`, `docs/AUDIT_CRON.md`

### Fichiers modifiés
- `core/signals.py` (refactor + extension)
- `core/models.py` (AuditLog étendu)
- `siga/settings/base.py` (MIDDLEWARE + AUDIT_RETENTION)
- `siga/urls.py` (routing /api/v1/audit/)
- `apps/suivi/views.py` (décorateur @audit_aggregate sur ajouter_suivie, bulk_update_commentaires)
- `apps/emplois/services/emplois_service.py` (décorateur @audit_aggregate sur archiver_emplois, restaurer_depuis_archive)
- 5 pages dashboard frontend (bouton 🕐)

---

## 12. Pré-requis

- Backup complet de `gesafped26` avant Sprint 1
- Code Phase 5 (CharField → FK) déployé et validé ✅ (déjà fait)
- Données de test disponibles pour validation E2E

---

**Statut** : Prêt à exécuter. Démarrage Sprint 1 dès validation.
