# Plan d'implémentation — IPGEI
**Institut Préparatoire aux Grandes Écoles d'Ingénieurs — version dérivée de SIGA**

> Dépôts cibles : **`backend_ipgei`** (Django/DRF) · **`frontend_ipgei`** (Next.js).
> Base de données PostgreSQL dédiée : **`IPGEI`** (jamais partagée avec SIGA).
> Produit **séparé**, **forké sur le socle SIGA** — zéro impact sur SIGA en production.

---

## ⚠️ Principe non-négociable — Isolation totale de SIGA

Tout le développement IPGEI se fait dans des **dépôts séparés** (`backend_ipgei`, `frontend_ipgei`)
et sur une **base de données distincte `IPGEI`**. En aucun cas ce travail ne doit **modifier,
migrer, ni même toucher** :

- le **code existant** de SIGA (backend `SIGA-POSTGRES/siga`, frontend `gesafped_frontend`) ;
- la **base de données SIGA** (`siga_pg`) ;
- les migrations, données ou déploiements de SIGA en production.

« Forker le socle » = **copier** le code du socle comme point de départ d'IPGEI, jamais l'éditer en place.
SIGA reste **intact et opérationnel**.

---

## 0. Cadrage validé (décisions)

| # | Décision |
|---|---|
| Architecture | Produit **séparé**, forké du socle SIGA (pas de « profil activable »). Dépôts `backend_ipgei` + `frontend_ipgei`. |
| Cursus | **IPGEI à 2 niveaux** : niveau 1 = **MPSI**, niveau 2 = **MP**. |
| Classes | MPSI A/B/C…, MP A/B/C… — **aucune limite** par année. |
| Sous-groupes | **TP fixes** sur le semestre (Informatique + Physique). |
| Calendrier | **4 semestres** S1–S4 ; **semaines flexibles** (défaut 16), via le mécanisme SIGA existant (`Semestre.date_debut/fin` + `Semaine`). |
| EDT | **Hebdomadaire par duplication** de la grille type ; édition d'**une semaine** ou d'un **lot de N semaines**. |
| Permutation prof | **Créneau conservé** ; échange **prof (+ salle + matière)** ; même classe ; portée 1 ou N semaines ; circuit *demande→accord→validation directeur* **+** action directe directeur ; **charge suit le prof effectif**. |
| Permutation étudiant | **Changement de classe** ; **historique conservé** (re-rattachement, aucune copie/perte) ; circuit *A↔B→validation directeur* + action directe directeur. |
| Notes | **DS 1..N** + **examens 1..N** (moyenne **arithmétique**) ; pondération **par matière, historisée dans la note** ; coefficients ; volume horaire. |
| Pondération | Paramétrable par matière (défauts **30/70** sans TP, **20/10/70** avec TP) ; **identique pour toutes les classes**. |
| Délibération | Seuil **configurable** (défaut 10). **1ʳᵉ année** : admis / réorienté (pas de redoublement). **2ᵉ année** : autorisé CNIM / redoublant (**droit unique**). |
| Rattrapage | **Note maximale** (paramétrable ; défaut sans plafond). |
| Absences | **Par exception** (présent par défaut ; ne marquer que les absents) ; liée à la séance (semaine + créneau). |
| Enseignants | Types : agrégé, contractuel, vacataire, technologue. |
| Documents | Relevé de semestre, relevé annuel, décision de délibération, attestation d'autorisation CNIM. |
| Phasage | **Phase 1** (académique) → **Phase 2** (EDT hebdo + permutations). |

---

## 1. Stratégie technique

- **`backend_ipgei`** (Django/DRF) et **`frontend_ipgei`** (Next.js) : nouveaux dépôts **forkés du socle SIGA**.
- Le socle est gardé **aligné** avec SIGA (mêmes patterns) → patchs de sécurité re-synchronisables.
- **Points de jonction SIGA prévus dès le départ** (SSO, référentiels, API — §7).
- **Base de données PostgreSQL dédiée : `IPGEI`** — jamais la base SIGA (`siga_pg`). Aucune migration IPGEI ne s'exécute sur `siga_pg`.

---

## 2. Ce qu'on FORKE du socle (réutilisé tel quel)

| Brique | Contenu réutilisé |
|---|---|
| `authentication` | JWT (cookies httpOnly), **RBAC granulaire**, délégation, rôles |
| `core` | **PDF renderer** (wkhtmltopdf), **signature PAdES**, audit, mixins, permissions, pagination |
| `parametres` | `Year`, **`Semestre` (`date_debut`/`date_fin`)**, **`Semaine` numérotées**, `Creneau`, `Jour`, `Seance`, `Institution` |
| `salle` · `prof` · `absence.Etudiant` | Salles, enseignants (à étendre), étudiants |
| `documents` | Génération de documents + **QR de vérification** + registre |
| `backup` · `notifications` · `portail` | Sauvegardes chiffrées, notifications, page publique de vérification |
| **Mécaniques métier** | **`SeuilsJury`** (seuils configurables en délibération) · **règle `max(note, rattrapage)`** |

---

## 3. Ce qu'on RETIRE (couche académique LMD)

`scolarite` (filières LMD), `modules`/`em` (EM LMD), la **délibération/PV LMD**, le **calcul de notes CC/TP LMD**, `inscriptions` pédagogiques LMD, `stages`, `avancement` LMD.
→ **Remplacés** par le moteur académique IPGEI (§4). Les patterns sont conservés ; le socle n'est pas réécrit.

---

## 4. Ce qu'on CONSTRUIT — moteur académique IPGEI

### 4.1 Structure : Classe · Sous-groupe (Phase 1)
- **`Classe`** : `cursus` (IPGEI), `niveau` (MPSI/MP), `libelle` (A/B/C…), `annee_univ`, `institution`. **Sans limite** par année.
- **`SousGroupeTP`** : rattaché à une `Classe`, `libelle` (G1/G2…), **fixe** sur le semestre, matières concernées (Info/Physique).
- **`InscriptionEtudiant`** : étudiant → classe (+ sous-groupe TP). Réutilise `Etudiant`.

### 4.2 Matières + pondération + coefficient + volume horaire (Phase 1)
- **`Matiere`** : `code`, `intitule`, `semestre` (S1–S4), `coefficient`, `volume_horaire`, `has_tp`,
  **pondération** `pct_ds` / `pct_tp` / `pct_exam` (défauts **30/70** sans TP, **20/10/70** avec TP),
  **paramétrable**, **identique pour toutes les classes**.
- Volume horaire → **info + base charge/paie**.

### 4.3 Évaluations DS/Examens flexibles + pondération historisée (Phase 1)
- **`Note`** par (étudiant × matière × semestre) : **liste de DS** (1..N), **liste d'examens** (1..N), note TP.
- **Snapshot pondération** : `pct_ds/pct_tp/pct_exam` **copiés dans la ligne Note** au moment du calcul
  → un changement futur de la pondération matière **n'altère jamais** les anciennes notes.
- Calcul :
  - `moy_DS = moyenne_arithmétique(DS)`
  - `moy_exam = moyenne_arithmétique(examens)`
  - `moy_matiere = pct_ds·moy_DS + pct_tp·TP + pct_exam·moy_exam` (pondération snapshotée)
- **Rattrapage** : `note_retenue = max(note, note_rattrapage)` — réutilise la règle SIGA ; **plafond paramétrable** (défaut : sans plafond).

### 4.4 Délibération IPGEI (Phase 1)
- **Seuil de validation configurable** par délibération (réutilise le pattern **`SeuilsJury`**, défaut 10, modifiable → 7…).
- Calcul moyenne semestre/année (**coefficients**) → **décision automatique** + ajustement possible par le jury.
- **Décisions 1ʳᵉ année** : `admis` (≥ seuil) / `réorienté` (< seuil) — **pas de redoublement**.
- **Décisions 2ᵉ année** : `autorisé_CNIM` (≥ seuil) / `redoublant` — **droit de redoublement unique** (compteur ; refus au 2ᵉ).

### 4.5 EDT hebdomadaire par duplication (Phase 2)
- **`GrilleType`** : modèle d'EDT par **(type_semestre pair/impair × classe)** — séances type (jour, créneau, matière, prof, salle, sous-groupe).
- **Duplication** → génère les **séances réelles pour chaque `Semaine`** du semestre (réutilise `Semaine` + génération hebdo SIGA).
- **Édition** : déplacer / supprimer / ajouter une séance, sur **une semaine** ou **un lot de N semaines**.
- Navigation « aller à la semaine du … » via `numero_semaine` / `date`.

### 4.6 Permutations (Phase 2)
- **`PermutationProf`** : **créneau conservé** ; échange **prof (+ salle + matière)** entre deux séances de la **même classe** ;
  **portée 1 ou N semaines** ; workflow **A→B→validation directeur** ; **action directe directeur** ; **charge/pointage suit le prof effectif** de la semaine.
- **`PermutationEtudiant`** : **changement de classe** ; **historique conservé** (notes/absences restent liées à l'étudiant → simple re-rattachement) ;
  workflow **A↔B→validation directeur** + action directe directeur ; traçabilité.

### 4.7 Absences par exception (Phase 1)
- **Présent par défaut** ; on saisit **uniquement les absents** ; absence **liée à la séance** (semaine + créneau).

### 4.8 Enseignants (Phase 1)
- Types : **agrégé, contractuel, vacataire, technologue** (extension de `prof`).

---

## 5. Documents officiels IPGEI
Réutilisent l'infra `documents` (PAdES + QR) :
- **Relevé de semestre**
- **Relevé annuel**
- **Décision de délibération**
- **Attestation d'autorisation CNIM**

---

## 6. Frontend (`frontend_ipgei`)

- **Phase 1** : classes/sous-groupes, matières + pondération, **saisie des notes** (DS/examens multiples),
  **délibération** (seuil + décisions), **absences par exception**, documents.
- **Phase 2** : **grille EDT hebdomadaire** (duplication + édition semaine/lot), **permutations** (profs & étudiants) avec circuits de validation.
- Conventions SIGA conservées : **TanStack Query partout**, pattern queryKey factory, RBAC côté menu.

---

## 7. Points de jonction avec SIGA (dès le départ)
- **Identité / SSO** commune (si login unique souhaité par le Groupe).
- **Référentiels partagés** (profs, salles, années) échangeables par **API**.
- **Format d'échange** étudiants/profs (import/export) pour un futur passage d'un monde à l'autre.

---

## 8. Estimation (indicative, jours·homme)

| Lot | Phase | Charge |
|---|---|---|
| Fork socle + nettoyage LMD + setup dépôts (`backend_ipgei`, `frontend_ipgei`) | 0 | 15 |
| Structure classes / sous-groupes / inscriptions | 1 | 14 |
| Matières + pondération + coefficient + volume | 1 | 8 |
| Notes DS/examens + calcul + snapshot + rattrapage | 1 | 18 |
| Délibération IPGEI (seuils, 1A/2A, calculs) | 1 | 15 |
| Absences par exception | 1 | 6 |
| Documents (relevés, décisions, CNIM) | 1 | 12 |
| Frontend Phase 1 | 1 | 34 |
| **Sous-total Phase 1** | | **≈ 122 j·h** |
| EDT hebdomadaire (grille type, duplication, édition) | 2 | 25 |
| Permutations profs (workflow, lot, charge) | 2 | 15 |
| Permutations étudiants (changement de classe) | 2 | 10 |
| Frontend Phase 2 | 2 | 26 |
| **Sous-total Phase 2** | | **≈ 76 j·h** |
| Tests, recette, déploiement | — | 15 |
| **TOTAL indicatif** | | **≈ 213 j·h (~10 mois·homme)** |

> À TJM 18 000 MRU/j·h → ordre de grandeur **~3,8 M MRU** (indicatif, à calibrer).

---

## 9. Ordre de réalisation (jalons)
1. **J0** — Fork socle, dépôts `backend_ipgei` + `frontend_ipgei`, nettoyage LMD, types de prof.
2. **J1** — Classes / sous-groupes + inscriptions.
3. **J2** — Matières + pondération.
4. **J3** — Notes DS/examens + calcul + rattrapage.
5. **J4** — Délibération 1A/2A + seuils.
6. **J5** — Absences + documents → **Livraison Phase 1 (année exploitable)**.
7. **J6** — EDT hebdomadaire + duplication.
8. **J7** — Permutations profs & étudiants → **Livraison Phase 2**.

---

## 10. Risques & garanties

| Risque | Maîtrise |
|---|---|
| Divergence socle IPGEI ↔ SIGA | Patterns alignés ; re-sync périodique ; extraction en librairie si utile |
| Cohérence charge/pointage lors des permutations | Le pointage suit le **prof effectif** ; traçabilité + validation directeur |
| Perte d'historique au changement de classe | **Re-rattachement** (jamais de copie/suppression) → notes/absences intactes |
| Changement de pondération | **Snapshot dans la Note** → l'ancien reste figé |
| Impact sur SIGA en production | **Produit séparé + BD `IPGEI` distincte** → code, migrations et données SIGA jamais touchés ; aucune régression possible |

---

*Document de plan — IPGEI. Base : socle SIGA (copié, jamais modifié). Dépôts : `backend_ipgei` (Django), `frontend_ipgei` (Next.js). Base de données : `IPGEI`. SIGA reste intact.*
