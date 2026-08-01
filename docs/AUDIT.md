# Journal d'audit SIGA — guide d'exploitation

Ce document décrit le système d'audit déployé en 3 sprints (Backend capture →
API/UI → Rétention), comment l'utiliser au quotidien et comment l'opérer en
production.

---

## 1. Vue d'ensemble

### 1.1 Couches

```
[Action métier]                                            UI navigateur
     ↓ HTTP                                                     ↑ JSON
[ Django ViewSet ]                                       /api/v1/audit/
     ↓                                                          ↑
[ ORM .save() / .delete() / bulk_* ]                  [ AuditLogViewSet ]
     ↓ pre_save / post_save / post_delete                       ↑
[ core.signals._handle_post_save ]                       AuditLog (HOT)
     ↓                                                       ↑   ↑
[ core.audit_helpers.write_audit ]                           │   │
     ↓ transaction.on_commit() (~0 latence)                  │   │
[ core_audit_log ] ← INSERT only ───────────────────────────┘   │
     ↓ archive_audit_logs (cron)                                 │
[ core_audit_log_archive ] ← INSERT only ──────────────────────┘
     ↓ purge_audit_logs (cron)
[ backups/audit/audit_YYYY-MM.jsonl.gz ] ← INSERT-only files
                ↑
        restore_audit_logs (manuel)
```

### 1.2 Garanties

| Garantie | Mécanisme |
|---|---|
| **Append-only** | `AuditLog.save()` lève `PermissionError` si `pk` set ; `delete()` aussi |
| **Latence ~0 sur l'utilisateur** | `transaction.on_commit()` diffère l'INSERT après le COMMIT métier |
| **Pas de cascade** | Échec d'`AuditLog` n'interrompt jamais le flux (silent log + warning) |
| **Bulk = 1 entrée** | `@audit_aggregate` désactive les signaux unitaires, écrit 1 ligne stats |
| **Diff complet** | `pre_save` capture old, `post_save` calcule `{champ: {old, new}}` |
| **FK lisibles** | Sérialisé en `{id, label}` plutôt qu'un id nu |

### 1.3 Données persistées par event

```jsonc
{
  "id": 1234,
  "timestamp": "2026-04-30T14:00:23.123456+00:00",
  "user": 7,                        // CustomUser.pk (null si système)
  "action": "UPDATE",               // CREATE/UPDATE/DELETE/BULK_*/...
  "model_name": "SuiviePointage",
  "object_id": "1132",
  "label": "Pointage SEA L2 - G1",
  "changes": {                      // diff complet sauf BLACKLIST
    "commentaire": {"old": "Non fait", "new": "Fait"}
  },
  "ip_address": "10.0.0.42",
  "user_agent": "Mozilla/5.0 ...",
  "request_id": "uuid-v4",          // corrélation tous logs même requête
  "endpoint": "/api/v1/suivi/pointages/1132/",
  "http_method": "PATCH",
  "institution": 3,                 // si résolvable depuis l'instance
  "keep_forever": false             // true = jamais purgé (ex. LOGIN_FAILED)
}
```

### 1.4 Champs exclus du diff (BLACKLIST)

`id, pk, created_at, updated_at, date_creation, date_modification, last_login,
date_joined, password`

Pour ajouter un champ à exclure, modifier `core/audit_helpers.py:BLACKLIST_FIELDS`.

---

## 2. Modèles tracés

Liste actuelle dans `core/signals.py:TRACKED_MODELS` (16 modèles métier) :

- **Suivi** : `Suivie`, `SuiviePointage`, `ChargeInstitution`
- **Emplois** : `Emplois`, `EmploisArchive`
- **Absences** : `Presence`, `Etudiant`
- **Vacations** : `Vacation`, `Surveillance`
- **Évaluations** : `Note`, `PVDeliberation`, `LigneDeliberation`, `RachatNote`
- **Documents** : `DocumentOfficiel`, `RegistreDiplome`
- **Référentiel** : `Departement`, `Filiere`, `Prof`, `EM`
- **Inscriptions** : `InscriptionAdministrative`, `InscriptionPedagogique`,
  `InscriptionElement`
- **Réclamations** : `Reclamation`
- **Paramètres** : `Institution`
- **Sécurité** : `CustomUser` + events login/logout/login-failed

Pour ajouter un modèle :

1. Ajouter `'app.Model'` dans `TRACKED_MODELS`.
2. Redémarrer Django (les signaux sont reconnectés au démarrage).

Pour les opérations bulk : décorer la vue avec `@audit_aggregate(label='…',
action='BULK_CREATE', model_name='…')`. Voir exemples dans `apps/suivi/views.py`.

---

## 3. Endpoints API

Préfixe : `/api/v1/audit/` — cookie JWT requis (utilisateur authentifié).

| Méthode | URL | Permission | Description |
|---|---|---|---|
| GET | `/` | Auth (user voit ses logs) ; admin/IT voit tout | Liste paginée |
| GET | `/{id}/` | Auth + ownership | Détail complet (diff, user-agent, request-id) |
| GET | `/by-entity/?model=X&object_id=Y` | Auth | Timeline d'une entité — rendu dans `<AuditTimeline>` |
| GET | `/stats/?days=N` | Admin/IT | Compteurs par action/modèle/utilisateur |
| GET | `/export/` | Admin/IT | Export CSV streamé (les filtres list s'appliquent) |

**Filtres acceptés** sur `/` et `/export/` :
`user, institution, action, model_name, object_id, keep_forever,
timestamp_after (ISO), timestamp_before (ISO), search` (OR sur
label/model/object_id/user.username/endpoint).

---

## 4. UI

### 4.1 Page globale

`/dashboard/historique` — admin/IT uniquement. Filtres + pagination + export CSV.
Accepte les query-params `model=X`, `action=X`, `search=X`, `from=YYYY-MM-DD`,
`to=YYYY-MM-DD` pour deep-link.

### 4.2 Composants réutilisables

```tsx
import HistoryButton from '@/components/audit/HistoryButton';

// Ouvre un drawer avec la timeline filtrée pour cette entité
<HistoryButton model="SuiviePointage" objectId={sp.id} title="Pointage XYZ" />

// Variante bouton "label visible"
<HistoryButton model="Emplois" objectId={e.id} variant="button" label="Historique" />
```

```tsx
import AuditTimeline from '@/components/audit/AuditTimeline';

// Timeline inline (sans drawer)
<AuditTimeline model="Prof" objectId={prof.id} />
```

Bouton actuellement déployé sur :

- `/dashboard/suivi/remplissage` (par pointage)
- `/dashboard/suivi/charges` (par charge GP)
- `/dashboard/profs` (par enseignant)
- `/dashboard/em` (par EM)
- `/dashboard/emplois/gerer` (lien header → /historique?model=Emplois)

---

## 5. Rétention (Option C)

| Tier | Durée | Lieu | Recherche | Espace ~/an |
|---|---|---|---|---|
| **HOT** | 0–90 j | `core_audit_log` | < 50 ms | ~30 Mo |
| **ARCHIVE** | 90 j–1 an | `core_audit_log_archive` | < 200 ms | ~110 Mo |
| **COLD** | > 1 an | `backups/audit/audit_YYYY-MM.jsonl.gz` | restore_audit_logs (manuel) | ~10–30 Mo gzip |
| **FOREVER** | indéfini | reste en `core_audit_log_archive` | normal | dépend du volume |

`keep_forever=True` → l'event reste dans ARCHIVE pour toujours (pas purgé).
Activé automatiquement pour `LOGIN_FAILED` (sécurité) ; peut être ajouté pour
d'autres events sensibles via paramètre `write_audit(..., keep_forever=True)`.

### 5.1 Configuration

Dans `siga/settings/base.py` :

```python
AUDIT_RETENTION = {
    'HOT_DAYS':      90,
    'ARCHIVE_DAYS':  365,
    'EXPORT_PATH':   BASE_DIR / 'backups' / 'audit',
    'EXPORT_FORMAT': 'jsonl.gz',
}
```

### 5.2 Commandes

```bash
# Migrer HOT → ARCHIVE pour tout ce qui est > 90j
python manage.py archive_audit_logs

# Override + dry-run + batch
python manage.py archive_audit_logs --days 60 --dry-run --batch 5000

# Exporter ARCHIVE > 1 an vers JSONL.gz puis DELETE
python manage.py purge_audit_logs

# Restaurer un mois cold-stocké pour investigation forensique
python manage.py restore_audit_logs --month 2024-08

# Ou un fichier précis
python manage.py restore_audit_logs --file /chemin/audit_2024-08.jsonl.gz
```

### 5.3 Cron / Task Scheduler

**Linux** (`/etc/cron.d/siga-audit`) :

```cron
# Quotidien à 03:00 (heure de faible charge)
0 3 * * *  www-data  /opt/siga/scripts/audit_retention.sh >> /var/log/siga/audit_retention.log 2>&1
```

**Windows** (Task Scheduler) :

1. Action → Démarrer un programme : `C:\react_projects\GES\siga\scripts\audit_retention.bat`
2. Déclencheur : Quotidien à 03:00
3. Compte : utilisateur ayant accès à MySQL et au répertoire `backups/`

Voir [scripts/audit_retention.bat](../scripts/audit_retention.bat) et
[scripts/audit_retention.sh](../scripts/audit_retention.sh).

---

## 6. Format JSONL.gz

Chaque ligne du gzip = un event JSON sérialisé. Format aplati (pas de tableau
englobant), idéal pour `zcat | jq` ou `gzip -dc | grep`.

Exemple de manipulation rapide :

```bash
# Combien d'events dans un mois ?
zcat backups/audit/audit_2024-08.jsonl.gz | wc -l

# Tous les UPDATE sur la table SuiviePointage en aout 2024
zcat backups/audit/audit_2024-08.jsonl.gz \
  | jq -c 'select(.action=="UPDATE" and .model_name=="SuiviePointage")' \
  | head

# Restaurer en BD pour requeter via UI
python manage.py restore_audit_logs --month 2024-08
```

---

## 7. Sécurité opérationnelle

- **Append-only** : impossible de modifier ou supprimer un AuditLog via l'ORM.
  Pour purger, passer par `purge_audit_logs` qui exige cutoff > ARCHIVE_DAYS.
- **`keep_forever=True`** : ces events sont **immortels** dans ARCHIVE — utilisés
  pour LOGIN_FAILED et tout event critique (à étendre selon besoin légal).
- **Backups** : les fichiers `audit_*.jsonl.gz` doivent être inclus dans la
  sauvegarde système. Un fichier perdu = un mois d'historique cold perdu.
- **Modèles non tracés** : ajouter à `TRACKED_MODELS` est gratuit côté
  performances (signaux O(1) si `aggregate_active`).

---

## 8. Dépannage

| Symptôme | Cause probable | Fix |
|---|---|---|
| Aucun log écrit pour un modèle | Modèle absent de `TRACKED_MODELS` | Ajouter, redémarrer |
| 200 logs pour 1 bulk_create | Pas de `@audit_aggregate` ou décorateur après autre middleware | Ajouter le décorateur |
| `user=null` partout | `core.middleware.AuditMiddleware` absent ou avant `AuthenticationMiddleware` | Vérifier ordre dans `MIDDLEWARE` |
| `ip_address=null` | Middleware absent ou IP via header non standard | Adapter `request.META` selon proxy |
| Espace disque BD en croissance forte | `archive_audit_logs` jamais run | Vérifier cron/Task Scheduler |
| Restore échoue | Fichier corrompu ou format inattendu | Vérifier le gzip avec `gunzip -t` |

---

## 9. Évolutions futures (non incluses)

- Webhook / Slack alert sur `LOGIN_FAILED` répétés ou `PERMISSION_DENIED`
- Anonymisation RGPD : remplacer `user_id` par hash après suppression compte
- Recherche full-text sur `changes` (PostgreSQL `tsvector`)
- Export Parquet pour analytics
- Dashboard Grafana sur stats d'utilisation
