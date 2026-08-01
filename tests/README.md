# Suite de tests SIGA — Sprint 2

## Demarrage rapide

```bash
# Installation (une fois)
pip install -r requirements-dev.txt

# Lancer toute la suite
pytest tests/

# Avec couverture HTML detaillee
pytest tests/ \
  --cov=apps.evaluations.services \
  --cov=apps.vacation.models \
  --cov=core.permissions \
  --cov-report=html:htmlcov

# Ouvrir le rapport
start htmlcov/index.html        # Windows
open  htmlcov/index.html        # Mac
xdg-open htmlcov/index.html     # Linux
```

## Architecture

```
tests/
+- README.md                       (ce fichier)
+- __init__.py
+- conftest.py                     fixtures partagees + table prof_type_history + cache.clear
+- factories/                      factory_boy par domaine
|  +- parametres.py                Institution, Niveau, Semestre, Paiement, Seance...
|  +- scolarite.py                 Filiere (LP/ING), Etudiant, DepartementAcademique
|  +- em.py                        EM legacy, Module LMD, ElementModule, Departement annuel
|  +- inscriptions.py              InscriptionAdmin, InscriptionPed, InscriptionElement
|  +- evaluations.py               SessionEvaluation x4, Note, ResultatElement, ResultatSemestre
|  +- deliberation.py              PVDeliberation semestriel + annuel
|  +- vacation.py                  Prof, Vacation
|  +- auth.py                      CustomUser (admin/DE/etudiant/...), Module, Action, RoleDefault
+- test_calcul_notes.py            34 tests + 1 xfail (gap DNI 75%)
+- test_calculer_element.py        9 tests d'integration BD (NoteCalculService)
+- test_deliberation_semestre.py   12 tests (Art. 15-17 LP)
+- test_deliberation_annuelle.py   20 tests (Art. 20-22 LP, Art. 24-28 ING + verrou S5)
+- test_vacation_models.py         13 tests (Paiement.get_taux_at + Vacation.save/montant)
+- test_rbac.py                    16 tests (regression bug DE)
```

## Bilan sprint 2 (10 jours)

- **104 tests passants + 1 xfailed** en 2.65s sur sqlite RAM (jamais touche gesafped26)
- **Couverture services critiques** :
  - `vacation/models.py`             100%
  - `deliberation_semestre.py`       76%
  - `deliberation_annuelle.py`       70%
  - `core/permissions.py`            69%
  - `calcul_notes.py`                63%
- **Bugs regression couverts** :
  - DE avec emplois.modifier doit pouvoir editer (UserPermission > RoleDefault)
  - Filtre par session du PV semestriel (sinon SR ecrase SN)
  - Progression sur annee courante ne compte pas comme deja_redoublant
  - Vacation.save() preserve taux_paiement explicite
- **Gaps reglementaires documentes (xfail)** :
  - `calculer_progression_annuelle()` hardcode 65% (LP) — DNI necessite 75%
    Le fix est deja en place dans `DeliberationAnnuelleIngenieur` (sous-classe).

## Conventions

- 1 fichier de test par module/service teste
- Classes `Test*` pour grouper les scenarios
- Utiliser les fixtures de `conftest.py` quand possible (`institution`, `semestre_S1`, `filiere_dlp`, `D` = Decimal)
- Marquer `@pytest.mark.unit` pour les tests sans BD (rapides)
- Marquer `@pytest.mark.integration` pour les tests qui touchent l'API DRF
- Marquer `@pytest.mark.regression` pour les tests reproduisant un bug fixe

## Sur quoi ecrire en priorite (apres sprint 2)

- `apps.evaluations.services.calcul_module` (17% — module Art. 13)
- `apps.evaluations.services.pv_enrichment` (0% — generation PV detaillee)
- `apps.evaluations.services.deliberation` (0% — service rachat manuel)
- `apps.evaluations.services.anonymat` (0% — generation codes anonymes)
- API DRF (RBACPermission applique sur les endpoints reels via test client)
