# IPGEI — Backend

> ## ⛔ DEUX INTERDITS ABSOLUS
>
> 1. **NE JAMAIS TOUCHER À `C:\SIR\SIGA`** (`siga` et `gesafped_frontend`).
>    Lecture seule, pour s'en inspirer. Aucune écriture, aucun commit, aucun push.
>    Vérifier avec `git -C C:\SIR\SIGA\siga status --porcelain` avant de conclure.
> 2. **N'ÉCRIRE QUE DANS LA BASE `IPGEI`.** Jamais `siga_pg`, `gesafped26` ni
>    `siga_prive` : elles cohabitent sur le même serveur PostgreSQL local.
>    Avant toute opération destructrice : `pg_dump`, puis répétition en
>    transaction annulée.

Django 4.2 + DRF servant le frontend Next.js `C:\SIR\IPGEI\frontend_ipgei`.
PostgreSQL 18, base **`IPGEI`**. 25 applications, ~75 600 lignes de Python.

*Chiffres relevés le 15/09/2026 ; les recompter avant de s'y fier.*

## Réglages — quatre profils, et lequel sert où

| Profil | Usage | À savoir |
|---|---|---|
| `siga.settings.development` | poste local | `DEBUG=True`, `ALLOWED_HOSTS=['*']` |
| `siga.settings.docker` | **la production** | n'impose pas HTTPS ; cookies et redirection SSL pilotés par variables |
| `siga.settings.production` | HTTPS strict | force cookies `Secure`, redirection SSL, HSTS |
| `siga.settings.test` | tests | **SQLite en mémoire** — ne touche aucune base réelle |

Le déploiement actuel est en **HTTP**, donc sous le profil `docker` avec
`DJANGO_COOKIE_SECURE=False`. Avec `production`, les cookies `Secure` ne
seraient jamais émis en clair : personne ne pourrait se connecter, et sans
message exploitable.

`SECRET_KEY` a encore une valeur de repli dans `base.py` — à retirer, un
déploiement incomplet démarrerait en silence avec une clé publique.

## Tests

**79 fichiers, 710 tests collectés. 703 passent**, 1 échec attendu (`xfail`),
6 échecs connus (relevé du 15/09/2026).

```bash
python -m pytest -q --no-migrations         # SQLite en mémoire, ~16 s
```

`pytest` vit dans `requirements-dev.txt`, absent du venv par défaut. Une CI
GitHub les exécute avec couverture sur `main` et `develop`.

Les six échecs connus, pour ne pas les rediagnostiquer :
- `test_aucune_donnee_technique_exposee` — **vrai défaut** : la page publique
  `/verifier/<token>` expose `semestre`, `moyenne_semestre`, `decision`, `nni` ;
- quatre tests de la couche **LMD** que le fork a remplacée (filière, module) ;
- un test cassé dans son propre code (`ListSerializer` instancié sans `child`).

## Règles métier qu'on ne devine pas

### Moyennes annuelles — `apps/ipgei/services.py`
```python
PONDERATION_ANNUELLE = {
    1: {'S1': 2, 'S2': 3},   # 1re année : (S1×2 + S2×3) / 5
    2: {'S3': 1, 'S4': 1},   # 2e année  : (S3 + S4) / 2
}
```

### Classement S3 — `apps/ipgei/classement.py`
`moyenne_classement = (moyenne 1re année + moyenne S3) / 2`, les deux termes à
parts égales. **Sans l'un des deux, l'étudiant n'est pas classé** : la feuille
sert à affecter, une demi-moyenne y passerait pour une vraie.

### Auditeur libre — `InscriptionIPGEI.auditeur_libre`
Il suit la scolarité **sans être de la promotion** : ni jury, ni classement, ni
PV, ni CNIM, ni effectifs. Il garde ses notes, son relevé et son rattrapage.

Le piège n'est pas qu'il apparaisse où il ne devrait pas — cela se verrait.
C'est qu'il **décale silencieusement le rang des autres** et la moyenne de
classe imprimée sur tous les relevés : les rangs restent une suite continue,
simplement fausse.

> Partout où l'on constitue une POPULATION, filtrer par
> **`InscriptionIPGEI.objects.cursus()`**.

L'exclusion des rangs vit dans `services._classer` et `statistiques_classe`,
décidée **là** et non chez les appelants : un queryset déjà filtré ne peut pas
être « dé-filtré ». L'auditeur y est classé **par comparaison** — situé dans la
promotion sans y entrer, quitte à partager son rang avec un diplômant.

`apps/ipgei/test_auditeur_libre.py` fige la seule assertion qui protège : rangs,
moyennes de classe et effectifs **identiques que l'auditeur existe ou non**.

### Absents comptés zéro
`_agreger(absents_a_zero=True)` compte une matière manquante comme **0 avec son
coefficient**. C'est le mode du jury et du classement — une épreuve non rendue
ne s'efface pas. Le mode par défaut, lui, redistribue sur les composantes
saisies : c'est celui d'un semestre en cours.

### Maquette et volumes — une seule maquette, pour toutes les promotions

Il n'existe **pas** de modèle `Maquette` : la maquette EST l'ensemble des
`Matiere`, qui ne sont **pas datées**. Une matière vaut donc pour toutes les
années, et le miroir lui tient **un seul `em.EM`** (unique sur `code_em` +
filière).

> **Retoucher une matière vaut rétroactivement pour toutes les promotions.**

Côté IPGEI c'est sans danger : `Note` recopie `coefficient` et `pct_*` au
calcul. Côté socle, l'EM ne recopiait rien — « Avancement » lisait `em.CM` en
direct comme volume de référence, si bien que ramener MATH-S1 de 147 h à 100 h
pour la rentrée faisait passer l'avancement de **2024-2025** de 1 % à 2 %.

`em.VolumeAnnuelEM` gèle ce volume par (année, EM), et `em.volumes` applique
l'idiome habituel — `volumes_applicables(em, annee)` rend le gel **sinon** l'EM,
comme `seuil_applicable` et `ponderation_applicable`. Le gel se pose à la
**génération du suivi** (`apps/suivi/views.py`) : le dernier moment où le volume
prévu est encore celui qu'on a voulu. Il est idempotent ; `rafraichir=True` est
un geste délibéré, jamais automatique.

Ce qui reste vrai et n'est **pas** couvert :
- `int(matiere.coefficient)` **tronque** dans l'EM. Sans effet aujourd'hui (tous
  les coefficients sont entiers), piège dès qu'un vaut 2,5.
- Le suffixe de niveau sur `code_em` est **conditionnel** : il n'apparaît que si
  deux `NiveauIPGEI` partagent un `rang`. Ajouter un MPI au rang de MP ferait
  basculer la condition et **renommerait 14 EM sur 26** (`MATH-S3` →
  `MATH-S3-MP`). Rien n'est perdu — c'est un UPDATE, les clés étrangères suivent
  — mais les attestations déjà imprimées ne diront plus le même code.

### Note retenue
`note_retenue = max(moyenne, min(rattrapage, plafond))` — un rattrapage ne
remplace la moyenne que s'il est meilleur.

## Pièges — chacun a déjà coûté du temps

- **`on_delete=PROTECT` partout** : 33 occurrences dans `apps/ipgei/models.py`,
  24 dans `inscriptions`, 20 dans `evaluations`. Une suppression échoue sur
  `jury_source`, `LigneDeliberation.inscription`, `InscriptionIPGEI.issue_de`,
  `GrilleFrais.annee_univ`… Utiliser `django.contrib.admin.utils.NestedObjects`
  pour voir la cascade **avant** d'agir.
- **Suppressions de masse** : encadrer par `core.audit_helpers.audit_aggregate_block()`,
  sinon le signal d'audit trébuche sur des objets déjà partis.
- **Signaux `on_commit`** : `apps/ipgei/signals.py` reflète le calendrier du
  socle dans les semestres IPGEI **après le commit**. Une répétition en
  transaction annulée ne les déclenche donc jamais — elle ne prouve rien sur
  leurs effets.
- **Deux tables de semaines** : `parametres.Semaine` (socle, une ligne par JOUR)
  et `ipgei.SemaineIPGEI` (une par semaine, portant les séances). La première
  se reflète dans la seconde ; les semaines porteuses de séances sont conservées.
- **`{# … #}` de Django ne commente qu'UNE ligne.** Sur deux lignes, la seconde
  s'imprime dans le PDF. Utiliser `{% comment %}`.
- **Excel/openpyxl** : le bandeau `collecte._entete_excel(..., largeurs_figees=True)`
  ne touche pas aux largeurs — indispensable quand elles sont calculées pour
  tenir sur une A4 paysage.
- **PDF** : `wkhtmltopdf` + polices Microsoft sont dans l'image Docker. Forcer à
  la fois `width` et `height` sur un sceau non carré l'écrase.
- **Documents lus en `fetch` + blob** : ne jamais leur laisser de
  `Content-Disposition`. Les gestionnaires de téléchargement — Internet Download
  Manager en tête, très répandu — s'y accrochent, et un simple
  `inline; filename="….pdf"` suffit : ils en lisent l'extension, redemandent
  l'URL pour leur compte, enregistrent le fichier et **tuent la requête de la
  page**. Le symptôme trompe : Chrome affiche « Failed to fetch » doublé d'un
  message CORS, alors que le serveur a répondu 200 avec les bons en-têtes et que
  le fichier est bien arrivé — par IDM, pas par l'application. Signe distinctif :
  la vue s'exécute **deux fois** par clic, une fois pour chacun.
  `core/download_middleware.py` retire l'en-tête pour les seules requêtes
  `Sec-Fetch-Mode: cors|same-origin` ; une navigation réelle (`navigate`) le
  conserve, les téléchargements par lien direct restent donc inchangés.

## Mode miroir — `core/mirror.py`, `core/sync.py`

Un seul code, deux rôles. `MIRROR_MODE=False` : instance **locale**, elle fait
autorité. `MIRROR_MODE=True` : **miroir** en lecture seule, qui refuse toute
écriture hors liste blanche.

Deux exceptions écrivent en ligne — la réclamation de séance et la saisie de
notes. Leurs tables sont dans `SYNC_EXCLUDE_TABLE` : **retirées entièrement du
dump**, donc ce qu'un enseignant y dépose survit à la publication suivante.
Ne pas confondre avec `SYNC_EXCLUDE_TABLE_DATA`, qui garde le schéma et vide les
données (journaux).

Ces modèles n'ont **aucune clé étrangère** vers le local : ils recopient la clé
naturelle et les libellés au moment du dépôt.

## Déploiement — `deploy/`

Voir `deploy/DEPLOIEMENT_IPGEI.md`. Trois points structurants :

- Le compose hérité de SIGA fige ses conteneurs sous les noms `siga-*` —
  **ceux d'une pile SIGA en service sur le VPS cible**. Utiliser
  `docker-compose.ipgei.yml`, entièrement préfixé `ipgei-`, projet `ipgei`.
- Servi en **HTTP sur le port 9090**, sans TLS.
- Le dump d'amorçage n'est lu **qu'au premier démarrage**, volume vierge.
  Le contrôler avec `gzip -t` : une archive corrompue échoue, le conteneur
  redémarre, trouve un répertoire non vide et affiche « Skipping
  initialization » — l'échec disparaît derrière une base saine mais vide.

## Structure

```
apps/ipgei/        → le module métier : classes, inscriptions, matières, notes,
                     délibérations, EDT, documents, concours CNIM
  services.py      → 3 715 lignes — moyennes, agrégation, décisions de jury
  views.py         → 4 624 lignes — un nouveau @action mérite son propre module
  classement.py    → classement S3 (PDF + Excel)
  pv.py            → PV de délibération      collecte.py → fiches et bandeau Excel
apps/{suivi,vacation,avancement,absence}/  → socle repris de SIGA
apps/reclamations/ → boîte de réception du portail (sans FK, cf. mode miroir)
core/              → mirror.py, sync.py, pagination, permissions, audit_helpers
deploy/            → pile Docker, Nginx, sauvegardes chiffrées
```

## Workflow

```bash
.venv\Scripts\python.exe manage.py runserver 8001     # 8001, pas 8000 :
                                                      # c'est le port qu'attend
                                                      # le front (.env.local),
                                                      # et un autre projet
                                                      # occupe déjà le 8000.
.venv\Scripts\python.exe manage.py check              # après chaque lot d'édits
.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
python -m pytest -q --no-migrations                   # SQLite jetable
```

## Commits

`feat(domaine): …`, `fix(domaine): …`, `chore(deploy): …` — en français,
expliquant **pourquoi**. Un seul remote, `ipgei` ; branche `ipgei-main`,
poussée aussi sur `main` :
```bash
git push ipgei ipgei-main && git push ipgei ipgei-main:main
```

## Pour les agents

- **Vérifier dans la base, pas seulement dans le code.** Écrire les recettes en
  transaction annulée (`transaction.set_rollback(True)`), **relatives à l'état**
  mesuré — jamais avec des compteurs absolus.
- Quand une recette contredit le code, **c'est souvent l'assertion qui est
  fausse** : vérifier le nom de table ou de champ avant d'accuser le produit.
- **`MEMORY.md`** dans `~/.claude/projects/.../memory/` porte l'historique des
  décisions.
