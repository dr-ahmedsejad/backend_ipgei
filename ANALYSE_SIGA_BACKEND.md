# Analyse Approfondie du Backend SIGA
## Audit Complet : Modeles, Vues, Serializers et Plan d'Integration Scolarite LMD

> **Date :** 13 avril 2026
> **Projet :** SIGA (Systeme Integre de Gestion Academique)
> **Stack :** Django 4.2.16 | DRF 3.15.2 | MySQL 8 | SimpleJWT | pdfkit | openpyxl
> **Chemin :** `C:\react_projects\GES\siga`

---

## Table des Matieres

1. [Vue d'Ensemble du Projet](#1-vue-densemble-du-projet)
2. [Inventaire des Modeles](#2-inventaire-des-modeles)
3. [Inventaire des Vues](#3-inventaire-des-vues)
4. [Inventaire des Serializers](#4-inventaire-des-serializers)
5. [Infrastructure Core](#5-infrastructure-core)
6. [Cartographie Existant vs Scolarite LMD](#6-cartographie-existant-vs-scolarite-lmd)
7. [Plan d'Amelioration et d'Integration](#7-plan-damelioration-et-dintegration)

---

## 1. Vue d'Ensemble du Projet

### 1.1 Architecture des Fichiers

```
siga/
  manage.py
  requirements.txt
  migrate_from_gesafped.py          # Script migration legacy
  siga/                              # Configuration Django
    settings/
      base.py                        # Config partagee (JWT, RBAC, CORS, Throttling)
      development.py                 # DEBUG=True, SQL logging
      production.py                  # SSL, HSTS, cookies securises
    urls.py                          # Routeur racine /api/v1/
    wsgi.py
  core/                              # Utilitaires partages
    authentication.py                # JWT Cookie Authentication
    permissions.py                   # RBAC avec cache versionne
    exceptions.py                    # Exception handler DRF custom
    axes_utils.py                    # Protection brute-force
    mixins.py                        # AuditMixin, SelectAllMixin
    pagination.py                    # StandardPagination (10/page)
    throttles.py                     # Rate limiting
  apps/                              # 12 applications Django
    authentication/                  # Auth + RBAC (6 modeles)
    parametres/                      # Config systeme (8 modeles)
    departement/                     # Departements (1 modele)
    banque/                          # Banques (1 modele)
    salle/                           # Salles (1 modele)
    em/                              # Elements de module (1 modele)
    prof/                            # Professeurs (1 modele)
    emplois/                         # Emplois du temps (2 modeles)
    suivi/                           # Suivi des seances (3 modeles)
    vacation/                        # Vacations et surveillances (2 modeles)
    absence/                         # Etudiants et presences (3 modeles)
    avancement/                      # Avancement (pas de modeles, calculs)
```

### 1.2 Configuration Cle

| Element | Valeur |
|---------|--------|
| **Base de donnees** | MySQL 8 (gesafped) |
| **Authentification** | JWT en cookies HttpOnly (access 15min, refresh 7j) |
| **Permissions** | RBAC custom avec cache Redis/LocMem (TTL 5min) |
| **Protection brute-force** | django-axes (5 tentatives / 15min) |
| **Rate limiting** | Anon 20/min, User 200/min, Login 5/15min |
| **Documentation API** | drf-spectacular (Swagger + ReDoc) |
| **Generation PDF** | pdfkit (wkhtmltopdf) |
| **Import Excel** | openpyxl |
| **Langue** | fr-fr |
| **Timezone** | Africa/Nouakchott |

### 1.3 Roles Existants

| Code | Libelle | Perimetre |
|------|---------|-----------|
| `admin` | Administrateur | Global (bypass RBAC) |
| `DG` | Directeur general | Etablissement |
| `DA` | Direction administrative | Etablissement |
| `DE` | Direction enseignement | Etablissement |
| `AA` | Assistant administratif | Departement |
| `IT` | Informatique | Technique |
| `scolarite` | Scolarite | Departement |

### 1.4 Endpoints API Existants

```
/api/v1/auth/           -> Authentication, profil, RBAC
/api/v1/parametres/     -> Configuration systeme (10 ViewSets)
/api/v1/banques/        -> Banques
/api/v1/salles/         -> Salles
/api/v1/departements/   -> Departements
/api/v1/em/             -> Elements de module
/api/v1/profs/          -> Professeurs + stats
/api/v1/emplois/        -> Emplois du temps + grille + dispo + PDF
/api/v1/suivi/          -> Suivi seances + pointage + charges
/api/v1/absences/       -> Etudiants + presences + rapport
/api/v1/vacations/      -> Vacations + surveillances + fiches
/api/v1/avancement/     -> Avancement EM/Profs + stats + PDFs
```

---

## 2. Inventaire des Modeles

### 2.1 App `authentication` (6 modeles)

#### CustomUser
```
Fichier : apps/authentication/models.py
Heritage : AbstractUser
Table DB : authentication_customuser

Champs :
  email              EmailField       unique=True
  role               CharField(20)    choices=ROLE_CHOICES, default='AA'
  name               CharField(150)   blank=True
  avatar             ImageField       upload_to='avatars/', validators=[max 5Mo, JPEG/PNG]
  + champs herites   (username, password, first_name, last_name, is_staff, is_active, etc.)
```

#### Module (RBAC)
```
Table DB : authentication_module
Champs :
  code               CharField(50)    unique=True        # ex: 'emplois', 'suivi', 'profs'
  nom                CharField(100)
  icone              CharField(50)    blank=True
  ordre              IntegerField     default=0
```

#### Action (RBAC)
```
Table DB : authentication_action
Champs :
  code               CharField(50)    unique=True        # ex: 'voir', 'modifier', 'supprimer', 'exporter'
  nom                CharField(100)
  icone              CharField(50)    blank=True
```

#### ModuleAction (RBAC - table de jonction)
```
Table DB : authentication_moduleaction
Champs :
  module             FK(Module)       CASCADE
  action             FK(Action)       CASCADE
Contrainte : unique_together = ('module', 'action')
```

#### RoleDefault (RBAC - permissions par defaut)
```
Table DB : authentication_roledefault
Champs :
  role               CharField(20)    choices=ROLE_CHOICES
  module_action      FK(ModuleAction) CASCADE
  allowed            BooleanField     default=False
Contrainte : unique_together = ('role', 'module_action')
```

#### UserPermission (RBAC - surcharges par utilisateur)
```
Table DB : authentication_userpermission
Champs :
  user               FK(CustomUser)   CASCADE
  module_action      FK(ModuleAction) CASCADE
  allowed            BooleanField     default=False
  departement        FK(Departement)  SET_NULL, null=True  # Scope par dept
Contrainte : unique_together = ('user', 'module_action')
```

#### UserContexte (contexte session)
```
Table DB : authentication_usercontexte
Champs :
  user               OneToOneField(CustomUser) CASCADE
  annee_universitaire CharField(9)    blank=True
  semestre           CharField(10)    choices=['Pairs','Impairs'], default='Pairs'
  updated_at         DateTimeField    auto_now=True
```

---

### 2.2 App `parametres` (8 modeles)

#### Year
```
Table DB : annee
Champs :
  annee              CharField(20)    unique=True        # ex: '2024-2025'
```

#### Niveau
```
Table DB : niveau
Champs :
  niveau             CharField(50)    unique=True        # ex: 'Licence', 'Master'
```

#### Semestre
```
Table DB : semestre
Champs :
  code_semestre      CharField(20)                       # ex: 'S1', 'S2'
  semestre           CharField(100)                      # ex: 'Semestre 1'
  niveau_semestre    FK(Niveau)       CASCADE
  type_semestre      CharField(1)     choices=['P'=Pair,'I'=Impair], default='I'
Note : Ce modele represente un semestre de PLANIFICATION (emplois du temps),
       PAS un semestre LMD academique avec credits.
```

#### Seance
```
Table DB : Seance
Champs :
  type_seance        CharField(50)    unique=True        # ex: 'CM', 'TD', 'TP', 'PR'
```

#### Creneau
```
Table DB : creneau
Champs :
  creneau            CharField(100)   unique=True        # ex: '08:00 - 09:30'
  duree              FloatField       default=1.5
  type_creneau       CharField(20)    choices=['matin','apres-midi','soir']
  ordre              IntegerField     default=0
  is_actif           BooleanField     default=True
```

#### Jour
```
Table DB : jour
Champs :
  jour               CharField(20)    unique=True        # ex: 'Lundi', 'Mardi'
```

#### Semaine
```
Table DB : semaine
Champs :
  numero_semaine     IntegerField
  jour               CharField(20)
  date               DateField
  annee_universitaire CharField(20)
  type_semestre      CharField(1)     default='I'
```

#### Paiement
```
Table DB : paiement
Champs :
  type               CharField(50)                       # ex: 'CM', 'TD'
  taux               FloatField                          # ex: 500.0 (MRU/h)
  date_debut         DateField                           # date d'effet
Contrainte : unique_together = ('type', 'date_debut')
Methode classmethod : get_taux_at(type_seance, date) -> float
```

#### Ramadan
```
Table DB : parametres_ramadan
Champs :
  debut              DateField
  fin                DateField
```

#### Institution
```
Table DB : institution
Champs :
  acronyme           CharField(20)    unique=True
  nom                CharField(200)
```

---

### 2.3 App `departement` (1 modele)

#### Departement
```
Table DB : departement
Champs :
  nom                CharField(200)
  description        TextField        blank=True
  niveau             FK(Niveau)       SET_NULL, null=True
  decalage           IntegerField     default=0
  annee_universitaire CharField(20)   blank=True
  code               CharField(20)    blank=True
```

---

### 2.4 App `banque` (1 modele)

#### Banque
```
Table DB : banque
Champs :
  nom                CharField(200)   unique=True
  description        TextField        blank=True
```

---

### 2.5 App `salle` (1 modele)

#### Salle
```
Table DB : salle
Champs :
  nom                CharField(100)   unique=True
  capacite           IntegerField     default=0
```

---

### 2.6 App `em` (1 modele)

#### EM (Element de Module - planification)
```
Table DB : em
Champs :
  code_em            CharField(50)
  intitule           CharField(200)
  CM                 IntegerField     default=0           # Heures CM prevues
  TD                 IntegerField     default=0           # Heures TD prevues
  TP                 IntegerField     default=0           # Heures TP prevues
  PR                 IntegerField     default=0           # Heures PR prevues
  departement        FK(Departement)  CASCADE
  semestre           FK(Semestre)     CASCADE
Contrainte : unique_together = ('code_em', 'departement')
Note : Represente un cours pour la PLANIFICATION des emplois du temps.
       Pas de credits, coefficients, ni ponderations d'evaluation.
```

---

### 2.7 App `prof` (1 modele)

#### Prof
```
Table DB : prof
Champs :
  NNI                BigIntegerField  unique=True          # Numero national d'identification
  nom                CharField(200)
  telephone          PositiveIntegerField null=True
  email              EmailField       blank=True
  genre              CharField(1)     choices=['M','F']
  type               CharField(20)    choices=['vacataire','permanent','contractuel']
  niveau_de_diplome  CharField(20)    choices=['Master','Ingenieur','Doctorat','Autre']
  description_dernier_diplome CharField(200) blank=True
  banque             FK(Banque)       CASCADE, null=True
  numero_de_compte   CharField(100)   blank=True
  cv                 FileField        upload_to='cvs/'
  diplome            FileField        upload_to='diplomes/'
  grade              CharField(100)   choices=['Professeur','MCF-A','MCF-B','MA-A','MA-B','Assistant','Vacataire','Ingenieur','Autre']
  charge             PositiveIntegerField null=True        # Charge CM annuelle
  decharge           PositiveIntegerField default=0
```

---

### 2.8 App `emplois` (2 modeles)

#### Emplois
```
Table DB : emplois_emplois
Champs legacy (CharField pour compatibilite GesAFPED) :
  id_prof, id_em, type_seance, jour, creneau, id_salle, id_departement, id_semestre
  annee_universitaire, type_semestre, taux_paiement
ForeignKeys (nouveaux champs normalises) :
  prof               FK(Prof)         SET_NULL
  em                 FK(EM)           SET_NULL
  departement        FK(Departement)  SET_NULL
  salle              FK(Salle)        SET_NULL
  semestre           FK(Semestre)     SET_NULL
  creneau_fk         FK(Creneau)      SET_NULL
Index : (annee_universitaire, departement, semestre), (annee_universitaire, jour, creneau_fk)
Methode save() : Synchronise les CharField legacy depuis les FK + auto-populate taux_paiement
```

#### EmploisArchive
```
Table DB : emplois_emploisarchive
Structure identique a Emplois sans index.
Sert a conserver un snapshot des emplois avant generation du suivi.
```

---

### 2.9 App `suivi` (3 modeles)

#### Suivie
```
Table DB : suivi_suivie
Meme pattern dual CharField + FK que Emplois.
Champs supplementaires :
  numero_semaine     IntegerField     default=0
  commentaire        TextField        blank=True
  date_suivie        DateField        null=True
  duree_creneau      FloatField       default=1.5
  taux_paiement      FloatField       default=0.0
```

#### SuiviePointage
```
Table DB : suivi_suivie_pointage
Variante de Suivie pour le pointage multi-departement.
id_departement peut contenir plusieurs IDs (CharField max_length=200).
```

#### ChargeInstitution
```
Table DB : suivi_chargeinstitution
Champs :
  institution        FK(Institution)  CASCADE
  prof               FK(Prof)         CASCADE
  charge_cm          IntegerField     default=0
  annee_universitaire CharField(20)
Contrainte : unique_together = ('prof', 'institution', 'annee_universitaire')
```

---

### 2.10 App `vacation` (2 modeles)

#### Vacation
```
Table DB : vacation_vacation
Champs :
  prof               FK(Prof)         CASCADE
  departements       M2M(Departement) blank=True
  em                 FK(EM)           SET_NULL, null=True
  type               FK(Seance)       SET_NULL, null=True
  duree              FloatField       default=1.5
  date               DateField
  annee_univ         CharField(20)
  taux_paiement      FloatField       default=0.0
Propriete : montant = duree * taux_paiement
```

#### Surveillance
```
Table DB : vacation_surveillance
Champs :
  prof               FK(Prof)         CASCADE
  departement        FK(Departement)  CASCADE
  duree              FloatField       default=2.0
  date               DateField
  annee_univ         CharField(20)
```

---

### 2.11 App `absence` (3 modeles)

#### Etudiant
```
Table DB : absence_etudiant
Champs :
  matricule          CharField(50)    unique=True
  nom                CharField(200)
  departement        FK(Departement)  CASCADE
  genre              CharField(1)     choices=['M','F']
Note : Modele MINIMALISTE, utilise uniquement pour le tracking des absences.
       Pas de prenom, date de naissance, filiere, email, telephone, etc.
```

#### Presence
```
Table DB : absence_presence
Champs :
  suivi              FK(Suivie)       CASCADE
  etudiant           FK(Etudiant)     CASCADE
  statut             IntegerField     choices=[0=Present, 1=Absent, 2=Sanctionne, 3=Justifiee]
  commentaire        TextField        blank=True
  justificatif       FileField        upload_to='justificatifs/'
  date_modification  DateTimeField    auto_now=True
Contrainte : unique_together = ('suivi', 'etudiant')
```

#### SeuilAbsence
```
Table DB : absence_seuilabsence
Champs :
  seuil              IntegerField     default=3           # Seuil d'alerte
Pattern : Singleton (pk=1 toujours)
```

---

### 2.12 App `avancement` (0 modeles)

Pas de modeles propres. Calcule l'avancement a partir des donnees `Suivie`, `Emplois`, `Vacation`.

---

### Recapitulatif des Modeles

| App | Nb Modeles | Modeles |
|-----|-----------|---------|
| authentication | 6 | CustomUser, Module, Action, ModuleAction, RoleDefault, UserPermission, UserContexte |
| parametres | 8 | Year, Niveau, Semestre, Seance, Creneau, Jour, Semaine, Paiement, Ramadan, Institution |
| departement | 1 | Departement |
| banque | 1 | Banque |
| salle | 1 | Salle |
| em | 1 | EM |
| prof | 1 | Prof |
| emplois | 2 | Emplois, EmploisArchive |
| suivi | 3 | Suivie, SuiviePointage, ChargeInstitution |
| vacation | 2 | Vacation, Surveillance |
| absence | 3 | Etudiant, Presence, SeuilAbsence |
| avancement | 0 | (calculs uniquement) |
| **TOTAL** | **29** | |

---

## 3. Inventaire des Vues

### 3.1 App `authentication`

| Vue | Type | Endpoint | Permissions | Description |
|-----|------|----------|-------------|-------------|
| `LoginView` | GenericAPIView | POST /auth/login/ | AllowAny + LoginRateThrottle | Login avec JWT cookies + axes |
| `LogoutView` | GenericAPIView | POST /auth/logout/ | IsAuthenticated | Blacklist refresh + clear cookies |
| `CookieTokenRefreshView` | TokenRefreshView | POST /auth/token/refresh/ | AllowAny | Refresh JWT depuis cookie |
| `ProfilView` | RetrieveUpdateAPIView | GET/PATCH /auth/profil/ | IsAuthenticated | Profil utilisateur + avatar |
| `ChangePasswordView` | UpdateAPIView | POST /auth/change-password/ | IsAuthenticated + SensitiveThrottle | Changement mot de passe |
| `ContexteView` | GenericAPIView | GET/PATCH /auth/contexte/ | IsAuthenticated | Annee/semestre actif |
| `MeView` | GenericAPIView | GET /auth/me/ | IsAuthenticated | Utilisateur + contexte courant |
| `MesModulesView` | GenericAPIView | GET /auth/mes-modules/ | IsAuthenticated | Liste modules accessibles |
| `UserViewSet` | ModelViewSet | /auth/users/ | IsAdmin | CRUD utilisateurs + toggle_active + unblock |
| `LockedAttemptsView` | GenericAPIView | GET /auth/locked-attempts/ | IsAdminOrIT | IPs bloquees |
| `ModuleViewSet` | ReadOnlyModelViewSet | /auth/rbac/modules/ | IsAdmin | Liste modules RBAC |
| `RBACMatrixView` | APIView | GET /auth/rbac/matrix/ | IsAdmin | Matrice role x action |
| `TogglePermissionView` | APIView | POST /auth/rbac/toggle/ | IsAdmin | Toggle permission |
| `UserPermissionsView` | ListAPIView | GET /auth/rbac/user/{id}/permissions/ | IsAdmin | Permissions d'un user |
| `UsersMatrixView` | APIView | GET /auth/rbac/users-matrix/ | IsAdmin | Matrice user x action paginee |
| `UserToggleView` | APIView | POST /auth/rbac/user-toggle/ | IsAdmin + AdminActionThrottle | Toggle permission user |
| `RoleToggleView` | APIView | POST /auth/rbac/role-toggle/ | IsAdmin | Toggle permission role |

**Service RBAC** (`services/rbac_service.py`) :
- `get_role_matrix()` : Matrice complete roles x permissions
- `get_users_matrix(page, size, search)` : Matrice utilisateurs paginee
- `get_user_modules(user)` : Codes modules accessibles
- `toggle_user_permission(user, ma, state)` : Appliquer on/off/role
- `toggle_role_permission(role, ma)` : Toggle defaut role

---

### 3.2 App `parametres`

| Vue | Type | Module RBAC | Actions Custom |
|-----|------|-------------|----------------|
| `YearViewSet` | ModelViewSet | IsAdmin | `all()` : AllowAny, cache 5min |
| `NiveauViewSet` | ModelViewSet | IsAdmin | - |
| `SemestreViewSet` | ModelViewSet | IsAdmin | - |
| `SeanceViewSet` | ModelViewSet | IsAdmin | - |
| `CreneauViewSet` | ModelViewSet | IsAdmin | - |
| `JourViewSet` | ModelViewSet | IsAdmin | - |
| `SemaineViewSet` | ModelViewSet | IsAdmin | `generer()` : generation semaines |
| `PaiementViewSet` | ModelViewSet | IsAdmin | `taux_actuel()` : taux courants |
| `RamadanViewSet` | ModelViewSet | IsAdmin | - |
| `InstitutionViewSet` | ModelViewSet | IsAdmin | NoPagination |

Tous utilisent `AuditMixin` + `SelectAllMixin`.

---

### 3.3 App `departement`

| Vue | Type | Module RBAC | Filtres |
|-----|------|-------------|---------|
| `DepartementViewSet` | ModelViewSet | 'departements' | annee_universitaire, niveau / nom, code |

---

### 3.4 App `banque`

| Vue | Type | Module RBAC | Filtres |
|-----|------|-------------|---------|
| `BanqueViewSet` | ModelViewSet | 'banques' | nom, description |

---

### 3.5 App `salle`

| Vue | Type | Module RBAC | Filtres |
|-----|------|-------------|---------|
| `SalleViewSet` | ModelViewSet | 'salles' | nom / nom, capacite |

---

### 3.6 App `em`

| Vue | Type | Module RBAC | Filtres |
|-----|------|-------------|---------|
| `EMViewSet` | ModelViewSet | 'em' | departement, semestre / code_em, intitule |

---

### 3.7 App `prof`

| Vue | Type | Module RBAC | Filtres | Actions Custom |
|-----|------|-------------|---------|----------------|
| `ProfViewSet` | ModelViewSet | 'profs' | type, genre, grade, diplome, banque / nom, email, NNI | `stats()` : statistiques aggregees (type, genre, diplomes) |

Serializers differencies : `ProfListSerializer` (leger) pour list, `ProfSerializer` (complet) pour detail.

---

### 3.8 App `emplois`

| Vue | Type | Module RBAC | Actions Custom |
|-----|------|-------------|----------------|
| `EmploisViewSet` | ModelViewSet | 'emplois' | `grille()` : grille emploi par jour/creneau |
| | | | `check-dispo()` : verification disponibilite prof/salle/dept |
| | | | `grille-all()` : grille tous departements |
| | | | `bulk()` : creation en masse (207 Multi-Status) |
| | | | `pdf()` : generation PDF emploi du temps |

**Service** (`services/emplois_service.py`) :
- `archiver_emplois()` : Archive avant generation suivi
- `restaurer_depuis_archive()` : Restauration si suppression suivi

---

### 3.9 App `suivi`

| Vue | Type | Module RBAC | Actions Custom |
|-----|------|-------------|----------------|
| `SuivieViewSet` | ModelViewSet | 'suivi' | `par-semaine()` : suppression LIFO par semaine |
| | | | `semaines-generees()` : liste semaines generees |
| | | | `ajouter()` : generation suivi depuis emplois |
| `SuiviePointageViewSet` | ModelViewSet | 'suivi' | - |
| `ChargeInstitutionViewSet` | ModelViewSet | 'suivi' | - |

---

### 3.10 App `vacation`

| Vue | Type | Module RBAC | Actions Custom |
|-----|------|-------------|----------------|
| `VacationViewSet` | ModelViewSet | (vacation) | `etat()` : etat des vacations |
| | | | `fiches()` : fiches mensuelles paiement |
| | | | `pdf-fiches()` : PDF fiches paiement |
| | | | `attestation()` : attestation de travail |
| | | | `pdf-attestation()` : PDF attestation |
| `SurveillanceViewSet` | ModelViewSet | (vacation) | - |

**Logique metier** : Calcul paiement mensuel combinant Suivie (cours effectues) + Vacation + Surveillance. Conversion TD/TP/PR en equivalent CM (ratio 2/3).

---

### 3.11 App `absence`

| Vue | Type | Module RBAC | Actions Custom |
|-----|------|-------------|----------------|
| `EtudiantViewSet` | ModelViewSet | 'absences' | `importer()` : import Excel (matricule, nom, genre) |
| `PresenceViewSet` | ModelViewSet | 'absences' | `bulk()` : mise a jour en masse |
| | | | `upload-justificatif()` : upload justificatif absence |
| | | | `rapport()` : statistiques absences par etudiant |
| | | | `par-etudiant()` : historique presences d'un etudiant |
| `SeuilAbsenceView` | RetrieveUpdateAPIView | 'absences' | Singleton (pk=1) |

---

### 3.12 App `avancement`

| Vue | Type | Module RBAC | Description |
|-----|------|-------------|-------------|
| `AvancementEMView` | APIView | 'avancement' | Avancement par EM (plan vs realise) |
| `AvancementProfsView` | APIView | 'avancement' | Avancement par prof |
| `AvancementProfDetailView` | APIView | 'avancement' | Detail avancement d'un prof |
| `ChargeProfsPermanantsView` | APIView | 'avancement' | Charge des permanents |
| `SuiviProfView` | APIView | 'avancement' | Suivi presence prof |
| `StatistiquesProfsView` | APIView | 'avancement' | Stats profs |
| `StatistiquesSemestresView` | APIView | 'avancement' | Stats semestres |
| `StatistiquesVacationsView` | APIView | 'avancement' | Stats vacations |
| + 6 vues PDF | APIView | 'avancement' | Versions PDF des rapports ci-dessus |

---

## 4. Inventaire des Serializers

### 4.1 Authentication

| Serializer | Modele | Usage |
|------------|--------|-------|
| `SIGATokenObtainPairSerializer` | - | JWT login avec payload custom + contexte |
| `UserContexteSerializer` | UserContexte | annee_universitaire, semestre |
| `UserSerializer` | CustomUser | id, username, name, email, role, avatar, is_active |
| `UserCreateSerializer` | CustomUser | Avec password + confirmation |
| `UserUpdateSerializer` | CustomUser | name, email, role, avatar |
| `ChangePasswordSerializer` | - | old_password, new_password |
| `ModuleSerializer` | Module | code, nom, icone, ordre + actions imbriquees |
| `ActionSerializer` | Action | code, nom, icone |
| `UserToggleSerializer` | - | user_id, module_action_id, state (on/off/role) |
| `RoleToggleSerializer` | - | role, module_action_id |

### 4.2 Parametres

| Serializer | Modele | Champs Particuliers |
|------------|--------|---------------------|
| `YearSerializer` | Year | annee |
| `NiveauSerializer` | Niveau | niveau |
| `SemestreSerializer` | Semestre | + niveau_nom (read-only) |
| `SeanceSerializer` | Seance | type_seance |
| `CreneauSerializer` | Creneau | tous les champs |
| `JourSerializer` | Jour | jour |
| `SemaineSerializer` | Semaine | tous les champs |
| `PaiementSerializer` | Paiement | type, taux, date_debut |
| `RamadanSerializer` | Ramadan | debut, fin |
| `InstitutionSerializer` | Institution | acronyme, nom |
| `GenerateSemainesSerializer` | - | Input : date_debut, date_fin, type_semestre |

### 4.3 Departement / Banque / Salle

| Serializer | Champs Particuliers |
|------------|---------------------|
| `DepartementSerializer` | + niveau_nom (read-only) |
| `BanqueSerializer` | tous les champs |
| `SalleSerializer` | tous les champs |

### 4.4 EM

| Serializer | Champs Particuliers |
|------------|---------------------|
| `EMSerializer` | + departement_nom, semestre_nom (read-only) |

### 4.5 Prof

| Serializer | Usage | Champs |
|------------|-------|--------|
| `ProfSerializer` | Detail/Update | Tous + banque_nom |
| `ProfListSerializer` | Liste | Leger (id, NNI, nom, type, genre, grade, banque, email, telephone, cv, diplome) |
| `ProfStatsSerializer` | Stats | Statistiques aggregees |

### 4.6 Emplois

| Serializer | Usage | Champs Particuliers |
|------------|-------|---------------------|
| `EmploisSerializer` | Liste | + prof_nom, em_code, em_intitule, dept_nom, salle_nom, semestre_nom, creneau_label |
| `EmploisCreateSerializer` | Creation | Exclut les champs legacy (id_prof, id_em...) |
| `DisponibiliteCheckSerializer` | Input | annee_universitaire, type_semestre, jour, creneau, departement, prof, salle |

### 4.7 Suivi

| Serializer | Usage |
|------------|-------|
| `SuivieSerializer` | Liste avec type_seance_label |
| `SuivieCreateSerializer` | Creation sans champs legacy |
| `SuiviePointageSerializer` | + labels resolus via SerializerMethodField |
| `ChargeInstitutionSerializer` | + institution_nom |

### 4.8 Vacation

| Serializer | Usage |
|------------|-------|
| `VacationSerializer` | + dept_noms (M2M), montant (calcule) |
| `VacationCreateSerializer` | Exclut taux_paiement |
| `SurveillanceSerializer` | + prof_nom, dept_nom |
| `EtatVacationSerializer` | Input : annee, mois |
| `AttestationSerializer` | Input pour attestation |

### 4.9 Absence

| Serializer | Usage |
|------------|-------|
| `EtudiantSerializer` | + departement_nom |
| `PresenceSerializer` | + etudiant_nom, matricule, statut_label |
| `PresenceBulkSerializer` | Liste pour mise a jour en masse |
| `SeuilAbsenceSerializer` | seuil (int) |
| `ImportEtudiantsSerializer` | Input : fichier Excel |

---

## 5. Infrastructure Core

### 5.1 Systeme RBAC (`core/permissions.py`)

```
Fonctionnement :
1. Chaque ViewSet declare `required_module = 'code_module'`
2. RBACPermission mappe l'action DRF vers un code action :
   list/retrieve -> 'voir'
   create/update/partial_update -> 'modifier'
   destroy -> 'supprimer'
   export -> 'exporter'
3. Admin (role='admin' ou is_superuser) -> acces total (bypass)
4. Resolution :
   a. UserPermission explicite pour cet utilisateur ? -> utilise allowed
   b. Sinon, RoleDefault pour ce role ? -> utilise allowed
   c. Sinon -> refuse
5. Cache versionne (Redis ou LocMem) avec TTL 5 min
```

### 5.2 AuditMixin (`core/mixins.py`)

```python
# Log CREATE/UPDATE/DELETE vers le logger Python (fichier logs/siga.log)
# PAS de table de base de donnees d'audit actuellement
```

### 5.3 SelectAllMixin (`core/mixins.py`)

```python
# Ajoute une action GET /resource/all/ sans pagination
# Utile pour les listes de reference (departements, salles, etc.)
```

### 5.4 Throttles (`core/throttles.py`)

| Classe | Limite | Scope |
|--------|--------|-------|
| `LoginRateThrottle` | 5 req / 15 min | Par IP |
| `SensitiveEndpointThrottle` | 5 req / 1 heure | Par utilisateur |
| `AdminActionThrottle` | 60 req / 1 min | Par utilisateur |

### 5.5 Pagination (`core/pagination.py`)

- `StandardPagination` : 10 elements par page (configurable via `page_size`)
- `NoPagination` : Pour les petits datasets (institutions, etc.)

---

## 6. Cartographie Existant vs Scolarite LMD

### 6.1 Principe Directeur : ETENDRE, NE PAS DUPLIQUER

> **Decision architecturale :** Plutot que creer des modeles paralleles (SemestreLMD, EtudiantLMD, ModuleLMD),
> on **etend les modeles existants** avec des champs nullable. Cela evite la duplication conceptuelle,
> simplifie les requetes et garantit un referentiel unique.
> Les champs ajoutes sont TOUS nullable ou avec default, donc **zero impact** sur le code existant.

### 6.2 Exigence Multi-Institution (Parametrage Institutionnel)

> **Le systeme SIGA doit etre deployable pour N'IMPORTE QUEL etablissement** (Universite de Nouakchott,
> ESP, ISCAE, etc.) sans modification de code. Toute l'identite visuelle et textuelle est pilotee
> par le parametrage.

Le modele `Institution` existant (acronyme + nom) est **trop pauvre**. Il doit devenir le **coeur
de la configuration institutionnelle** :

```
Institution ACTUEL :       Institution ETENDU :
  acronyme                   acronyme
  nom                        nom_fr                    # Nom en francais
                              nom_ar                    # Nom en arabe
                              nom_complet_fr            # Nom officiel complet FR
                              nom_complet_ar            # Nom officiel complet AR
                              logo                      # Image (affichee partout)
                              logo_republique           # Sceau national
                              adresse_fr                # Adresse FR
                              adresse_ar                # Adresse AR
                              ville                     # Ville
                              telephone
                              email
                              site_web
                              code_etablissement        # Code officiel MESRS
                              type_etablissement        # universite, ecole, institut
                              directeur_nom_fr          # Signataire documents
                              directeur_nom_ar
                              directeur_titre_fr        # "Doyen", "Directeur"
                              directeur_titre_ar
                              est_principale            # BooleanField - institution active
```

**Impact sur TOUTE l'application :**
- **En-tetes PDF** (emplois, PV, attestations, releves, diplomes) : logo + nom FR/AR automatique
- **Interface frontend** : logo dans le header, noms selon la langue active
- **Documents officiels** : sceau, signataire, adresse -- tout depuis Institution
- **Deploiement** : On installe SIGA, on remplit Institution, tout s'adapte

### 6.3 Modeles Existants a ETENDRE (ajout de champs nullable)

#### Year (parametres) -- +4 champs

| Champ a ajouter | Type | Default | Impact sur l'existant |
|-----------------|------|---------|----------------------|
| `date_debut` | DateField | null=True | NUL - le code existant ne lit que `annee` |
| `date_fin` | DateField | null=True | NUL |
| `est_active` | BooleanField | default=False | NUL |
| `est_cloturee` | BooleanField | default=False | NUL |

#### Semestre (parametres) -- +3 champs

| Champ a ajouter | Type | Default | Impact sur l'existant |
|-----------------|------|---------|----------------------|
| `credits` | IntegerField | default=30 | NUL - Emplois/Suivi ne lisent jamais ce champ |
| `filiere` | FK(Filiere) | null=True | NUL - Nullable, les semestres existants gardent filiere=None |
| `annee_univ` | FK(Year) | null=True | NUL - Nullable |

**Logique :**
- Les semestres EXISTANTS (S1 Impair, S2 Pair...) restent des **templates de planification** (filiere=None)
- Les semestres SCOLARITE sont crees avec filiere + annee_univ + credits remplis
- Emplois/Suivi continuent d'utiliser les templates, la scolarite utilise les instances avec filiere

#### EM (em) -- +6 champs

| Champ a ajouter | Type | Default | Impact sur l'existant |
|-----------------|------|---------|----------------------|
| `credits` | IntegerField | null=True | NUL |
| `coefficient` | DecimalField(4,2) | null=True | NUL |
| `poids_cc` | DecimalField(5,2) | default=40.00 | NUL |
| `poids_tp` | DecimalField(5,2) | default=0.00 | NUL |
| `poids_exam` | DecimalField(5,2) | default=60.00 | NUL |
| `seuil_eliminatoire` | DecimalField(4,2) | default=6.00 | NUL |
| `est_element_module` | BooleanField | default=False | NUL - distingue EM planification vs element LMD |
| `module_parent` | FK('self') | null=True | NUL - si c'est un element, pointe vers son module |
| `responsable` | FK(Prof) | null=True | NUL |

**Logique :**
- Les EM EXISTANTS restent des cours pour la planification (est_element_module=False, module_parent=None)
- Les EM SCOLARITE ont est_element_module=True, credits, coefficient, poids remplis
- Un "Module LMD" est un EM avec module_parent=None et des enfants EM qui pointent vers lui
- Un "Element de Module" est un EM avec module_parent rempli
- Cela permet de lier directement un element LMD a l'emploi du temps SANS bridge FK

#### Etudiant (absence) -- +12 champs

| Champ a ajouter | Type | Default | Impact sur l'existant |
|-----------------|------|---------|----------------------|
| `prenom` | CharField(200) | blank=True, default='' | NUL |
| `date_naissance` | DateField | null=True | NUL |
| `lieu_naissance` | CharField(200) | blank=True, default='' | NUL |
| `nationalite` | CharField(50) | default='Mauritanienne' | NUL |
| `cni` | CharField(20) | null=True, blank=True | NUL |
| `adresse` | TextField | blank=True, default='' | NUL |
| `telephone` | CharField(20) | blank=True, default='' | NUL |
| `email` | EmailField | blank=True, default='' | NUL |
| `photo` | ImageField | null=True | NUL |
| `filiere` | FK(Filiere) | null=True | NUL |
| `statut` | CharField(20) | default='actif' | NUL |
| `date_creation` | DateTimeField | auto_now_add=True | NUL |

**Logique :**
- Les etudiants EXISTANTS (4 champs) continuent de fonctionner pour le tracking des absences
- Les etudiants SCOLARITE ont les champs supplementaires remplis
- Un seul modele Etudiant, un seul matricule, pas de duplication

#### Departement -- +1 champ

| Champ a ajouter | Type | Default | Impact sur l'existant |
|-----------------|------|---------|----------------------|
| `institution` | FK(Institution) | null=True | NUL - Permet le rattachement institutionnel |

#### Institution (parametres) -- +16 champs (voir section 6.2)

Tous les champs ajoutes sont nullable ou avec default. L'existant (acronyme + nom) reste fonctionnel.

#### ROLE_CHOICES (authentication) -- +2 choix

| Choix a ajouter | Impact |
|-----------------|--------|
| `('responsable_filiere', 'Responsable de filiere')` | NUL - Ajout a la liste |
| `('jury_president', 'President de jury')` | NUL |

#### UserPermission (authentication) -- +1 champ

| Champ a ajouter | Type | Default | Impact |
|-----------------|------|---------|--------|
| `filiere` | FK(Filiere) | null=True | NUL - Scope optionnel par filiere |

### 6.4 Ce qui est ENTIEREMENT NOUVEAU

| Domaine | Modeles a creer | App |
|---------|-----------------|-----|
| **Structure LMD** | Filiere | scolarite |
| **Inscriptions** | Preinscription, InscriptionAdministrative, InscriptionPedagogique, InscriptionElement | inscriptions |
| **Evaluations** | SessionEvaluation, Note, Deliberation, ParametreJury, RachatNote | evaluations |
| **Stages** | ConventionStage, EvaluationStage, DerogationMedicale | stages |
| **Documents** | DocumentOfficiel, NumeroSerieConfig, RegistreDiplome | documents |
| **Notifications** | Notification | notifications |
| **Audit** | AuditLog | core |

### 6.5 Schema Relationnel Unifie (apres extension)

```
Institution (parametres - etendu)
  |
  +-- Departement (departement - etendu avec institution FK)
        |
        +-- Filiere (scolarite - NOUVEAU)
        |     |
        |     +-- Semestre (parametres - etendu avec filiere + credits)
        |           |
        |           +-- EM "Module" (em - etendu, module_parent=None)
        |                 |
        |                 +-- EM "Element" (em - etendu, module_parent=Module)
        |
        +-- EM existants (planification emplois du temps, est_element_module=False)
        |
        +-- Etudiant (absence - etendu avec filiere, prenom, date_naissance...)
              |
              +-- InscriptionAdministrative (inscriptions)
              +-- InscriptionPedagogique (inscriptions)
              +-- Note (evaluations)
              +-- ConventionStage (stages)
              +-- DocumentOfficiel (documents)
```

---

## 7. Plan d'Amelioration et d'Integration

> **Principes directeurs :**
> 1. **Etendre, ne pas dupliquer** -- enrichir les modeles existants avec des champs nullable
> 2. **Multi-institution** -- le systeme s'adapte a tout etablissement via le parametrage
> 3. **Bonnes pratiques** -- securite, scalabilite, performance, tracabilite a chaque couche

---

### 7.1 Probleme Critique : Departement = Filiere + Niveau + Groupe

#### Constat (donnees reelles)

```
Donnees actuelles dans la table departement :
  "SEA L1 - G1"    code=SEA   niveau=L1   annee=2025-2026
  "SEA L1 - G2"    code=SEA   niveau=L1   annee=2025-2026
  "SEA L2 - G1"    code=SEA   niveau=L2   annee=2025-2026
  "SEA L2 - G2"    code=SEA   niveau=L2   annee=2025-2026
  "SEA L3"         code=SEA   niveau=L3   annee=2025-2026
  "SDID"           code=SDID  niveau=L3   annee=2025-2026
  "SDID L2"        code=SDID  niveau=L2   annee=2024-2025
  "SDID L2 G1"     code=SDID  niveau=L2   annee=2024-2025
  "HE"             code=HE    niveau=Transversal
  "ST"             code=ST    niveau=Transversal
  "Statistique"    code=Stat  niveau=L3   annee=2024-2025
```

**Le modele Departement encode 3 dimensions dans un seul objet :**
- La **filiere** (SEA, SDID, Statistique)
- Le **niveau** (L1, L2, L3) -- deja via FK Niveau
- Le **groupe** (G1, G2) -- encode dans le nom

Et il est **duplique par annee universitaire** (meme filiere recree chaque annee).

#### Solution Proposee : Hierarchie Propre

```
AVANT (plat) :                          APRES (hierarchique) :

Departement                             Filiere (NOUVEAU)
  "SEA L1 - G1"                           code="SEA"
  "SEA L1 - G2"                           intitule="Sciences Exactes et Appliquees"
  "SEA L2 - G1"                           type_diplome="LP"
  "SEA L2 - G2"                           |
  "SEA L3"                                +-- Departement (ETENDU)
                                                "SEA L1 - G1"  filiere=SEA, groupe="G1"
                                                "SEA L1 - G2"  filiere=SEA, groupe="G2"
                                                "SEA L2 - G1"  filiere=SEA, groupe="G1"
                                                ...
```

**Concretement :**
1. Creer le modele `Filiere` (nouvelle app `scolarite`)
2. Ajouter `filiere` (FK nullable) et `groupe` (CharField nullable) a `Departement`
3. Les departements existants continuent de fonctionner tel quel (filiere=None)
4. Migration de donnees : creer les Filieres depuis les `code` distincts des departements, puis lier
5. Les modules transversaux (HE, ST) restent des departements sans filiere

---

### 7.1.1 Compatibilite avec la BD existante (verdict)

**Resume** : l'approche est compatible **au niveau schema** (toutes les additions sont nullable/blank), mais comporte 3 risques concrets a gerer.

#### Ce qui ne casse PAS (additif pur)

La table `departement` n'est ni renommee ni recreee — on lui ajoute uniquement `institution`, `filiere`, `groupe` (tous nullable/blank). Toutes les FK existantes restent valides :

| Modele | Fichier | on_delete |
|---|---|---|
| `Etudiant.departement` | apps/absence/models.py:15 | CASCADE |
| `EM.departement` | apps/em/models.py:10 | CASCADE (+ unique_together('code_em','departement')) |
| `Emplois.departement` | apps/emplois/models.py:22 | SET_NULL |
| `EmploisArchive.departement` | apps/emplois/models.py:80 | SET_NULL |
| `Suivie.departement` | apps/suivi/models.py:24 | SET_NULL |
| `Surveillance.departement` | apps/vacation/models.py:7 | CASCADE |
| `Vacation.departements` | apps/vacation/models.py:19 | M2M |
| `UserPermission.departement` | apps/authentication/models.py:109 | SET_NULL |

Extensions `Etudiant (+12)`, `EM (+8)`, `Semestre (+3)`, `Year (+4)` : toutes nullable/default → zero impact.

#### Risque 1 — Donnees heterogenes dans `departement.nom`

Les lignes actuelles melangent 3 schemas de nommage :

```
"SEA L1 - G1"    -> filiere=SEA, niveau=L1, groupe=G1   (format tiret)
"SDID L2 G1"     -> filiere=SDID, niveau=L2, groupe=G1  (format sans tiret)
"SDID L2"        -> AMBIGU : toute la promo L2 ou groupe implicite ?
"SDID"           -> AMBIGU : sans niveau, annee 2025-2026
"HE", "ST"       -> transversaux, filiere=None
"Statistique"    -> filiere=Stat, niveau=L3
```

**Action** : auditer manuellement les lignes "SDID" et "SDID L2" AVANT le backfill. Un regex naif va mal les classer.

#### Risque 2 — Rename `Institution.nom` -> `nom_fr`

C'est la **seule modification non-additive** du plan. Le serializer `Institution` utilise `fields='__all__'` ([apps/parametres/serializers.py:61](apps/parametres/serializers.py)), donc un rename sec fait disparaitre `nom` de l'API et casse le frontend silencieusement. Strategie corrigee en section 7.2 ci-dessous.

#### Risque 3 — CASCADE si des Departements sont fusionnes

Fusionner "SDID" + "SDID L2" en une ligne unique declencherait les `CASCADE` sur `Etudiant` et `EM`, avec perte de donnees. Regle imperative : **in-place uniquement, jamais de DELETE sur `departement`**. Enrichir les lignes existantes via UPDATE, ne jamais les recreer.

---

### 7.1.2 Recommandation : flux Departement / Filiere / Groupe

> **Garder `Departement` comme table de planification annuelle (= une "classe"), ajouter `Filiere` comme dimension stable a cote, relier via `Departement.filiere_id` nullable.**
> Tout le code existant continue de marcher, la scolarite gagne sa dimension propre, zero migration destructive.

#### Constat semantique

La table `departement` actuelle n'est pas une "direction administrative" : c'est en realite une **classe de planification** (filiere + niveau + groupe + annee, dupliquee chaque annee). 8 FK critiques pointent dessus (`Etudiant`, `EM`, `Emplois`, `Suivie`, `Vacation`, `Surveillance`, `UserPermission`, `EmploisArchive`). La casser serait suicidaire.

#### Modele recommande

```
Filiere (NOUVEAU, stable, partagee)           Institution (etendue)
  code        unique                             |
  intitule    FR/AR                               (FK optionnelle depuis Filiere)
  type_diplome                                   ^
  institution FK nullable           -------------+
       ^
       |  (FK nullable)
       |
Departement (EXISTANT, renomme semantiquement "Classe annuelle")
  nom, niveau, annee_universitaire, code   <- existant, inchange
  filiere    FK(Filiere)      nullable      <- NOUVEAU
  groupe     CharField(10)    blank         <- NOUVEAU, 'G1'/'G2'/''
  institution FK              nullable      <- NOUVEAU
       ^
       +--- Etudiant      (inchange, FK departement CASCADE)
       +--- EM            (inchange, FK + unique_together preservee)
       +--- Emplois       (inchange)
       +--- Suivie        (inchange)
       +--- Vacation      (inchange, M2M)
       +--- Surveillance  (inchange)
       +--- UserPermission (inchange)
```

#### Double lecture selon le besoin metier

- **Planification horaire / emploi du temps / suivi / pointage** → continue de requeter `Departement` directement. **Zero changement de code existant.**
- **Scolarite / notes / deliberation / releves** → requete via `Filiere` et agrege les `Departement` qui partagent `filiere_id`. Une deliberation "SEA L1" concerne `Departement.filter(filiere__code='SEA', niveau__code='L1')` = toutes les classes G1, G2, etc.

#### Migration en 3 temps (non destructive)

| Etape | Action | Reversible |
|---|---|---|
| **A** | `makemigrations` additif : cree `Filiere`, ajoute 3 champs nullable sur `Departement` | Oui |
| **B** | Data migration : `INSERT INTO scolarite_filiere (code, intitule) SELECT DISTINCT code FROM departement WHERE code != ''` puis `UPDATE departement SET filiere_id = ...` via matching sur `code` | Oui |
| **C** | Backfill de `groupe` par regex sur `nom` (`L\d\s*-?\s*(G\d)`), avec audit manuel prealable des lignes ambigues ("SDID", "SDID L2") | Oui |

Aucun DELETE. Aucun rename de FK. Aucun `on_delete=CASCADE` declenche. Le code `emplois`, `suivi`, `absence`, `vacation` tourne **sans modification** pendant toute la migration.

#### Gains

- **Scolarite** peut grouper par filiere stable sans parser des chaines de `Departement.nom`.
- **Multi-annee** : la filiere "SEA" existe une seule fois ; seules ses classes annuelles se multiplient.
- **Transversaux (HE, ST)** restent `filiere=None` → aucune exception speciale necessaire.
- **Zero regression** sur les flux existants (emploi, pointage, vacation, permissions).

#### A NE PAS faire

1. ❌ Creer une nouvelle table `Classe` et migrer les 8 FK dessus — risque eleve, benefice nul.
2. ❌ Supprimer ou fusionner des lignes `Departement` existantes — `CASCADE` sur `Etudiant`/`EM` fait perdre des donnees.
3. ❌ Passer `Departement.filiere` en NOT NULL immediatement — les transversaux doivent rester `filiere=None`.
4. ❌ Renommer `Institution.nom` en un seul coup (voir strategie additive en 7.2).

---

### 7.2 Parametrage Institutionnel Multi-Etablissement

#### Extension du Modele Institution

```python
# parametres/models.py -- Institution ETENDU

class Institution(models.Model):
    # --- Existant ---
    acronyme            CharField(20) unique
    nom                 CharField(200)              # CONSERVE -- deprecie via @property -> nom_fr
                                                    # (rename sec casserait le serializer fields='__all__')

    # --- NOUVEAUX CHAMPS (tous nullable/blank) ---

    # Identite bilingue FR/AR
    nom_fr              CharField(200)              # "Ecole Superieure Polytechnique"
    nom_ar              CharField(200) blank=True   # "المدرسة العليا متعددة التقنيات"
    nom_complet_fr      CharField(500) blank=True   # Nom officiel complet FR
    nom_complet_ar      CharField(500) blank=True   # Nom officiel complet AR
    devise_fr           CharField(200) blank=True   # "Honneur - Fraternite - Justice"
    devise_ar           CharField(200) blank=True

    # Identite visuelle
    logo                ImageField null=True        # Logo etablissement (header, PDF, UI)
    logo_republique     ImageField null=True        # Sceau national (diplomes)
    favicon             ImageField null=True        # Favicon navigateur

    # Coordonnees
    adresse_fr          TextField blank=True
    adresse_ar          TextField blank=True
    ville_fr            CharField(100) blank=True   # "Nouakchott"
    ville_ar            CharField(100) blank=True   # "نواكشوط"
    pays_fr             CharField(100) default='Mauritanie'
    pays_ar             CharField(100) default='موريتانيا'
    telephone           CharField(20) blank=True
    fax                 CharField(20) blank=True
    email               EmailField blank=True
    site_web            URLField blank=True

    # Informations officielles
    code_etablissement  CharField(20) blank=True    # Code MESRS
    type_etablissement  CharField(30) choices=[
                            ('universite','Universite'),
                            ('ecole','Ecole Superieure'),
                            ('institut','Institut'),
                        ] default='ecole'
    ministere_fr        CharField(200) blank=True   # "Min. Enseignement Sup."
    ministere_ar        CharField(200) blank=True

    # Signataire des documents officiels
    directeur_nom_fr    CharField(200) blank=True   # "Pr. Mohamed Ahmed"
    directeur_nom_ar    CharField(200) blank=True
    directeur_titre_fr  CharField(100) blank=True   # "Directeur"
    directeur_titre_ar  CharField(100) blank=True
    directeur_signature ImageField null=True         # Image signature (attestations, PV)

    # Configuration active
    est_principale      BooleanField default=True    # Institution active du deploiement

    class Meta:
        db_table = 'institution'
```

#### Comment ca marche dans toute l'application

```
1. FRONTEND (header, sidebar, login page) :
   GET /api/v1/parametres/institution/active/   -> AllowAny, cache 1h
   Retourne : nom_fr, nom_ar, logo, favicon, devise_fr, devise_ar
   -> Le frontend affiche dynamiquement selon la langue active

2. GENERATION PDF (emplois, PV, attestations, releves, diplomes) :
   institution = Institution.objects.filter(est_principale=True).first()
   -> En-tete : logo + nom_fr + nom_ar + ministere
   -> Pied : adresse, telephone, site_web
   -> Signataire : directeur_nom + titre + signature image
   -> Sceau : logo_republique (pour diplomes)

3. DEPLOIEMENT UNIVERSITE DE NOUAKCHOTT :
   Institution.nom_fr = "Universite de Nouakchott"
   Institution.nom_ar = "جامعة نواكشوط"
   Institution.logo = <logo_unk.png>
   -> Automatiquement : toute l'app, tous les PDF, tout le frontend

4. DEPLOIEMENT ESP :
   Institution.nom_fr = "Ecole Superieure Polytechnique"
   Institution.nom_ar = "المدرسة العليا متعددة التقنيات"
   Institution.logo = <logo_esp.png>
   -> Meme code, meme deploiement, tout s'adapte
```

#### Endpoint Institution Active (public, cache)

```python
class InstitutionActiveView(APIView):
    """Retourne l'institution active -- endpoint public, cache 1h."""
    permission_classes = [AllowAny]
    throttle_classes = [AnonRateThrottle]

    def get(self, request):
        cache_key = 'institution_active'
        data = cache.get(cache_key)
        if not data:
            inst = Institution.objects.filter(est_principale=True).first()
            data = InstitutionPublicSerializer(inst).data
            cache.set(cache_key, data, timeout=3600)
        return Response(data)
```

---

### 7.3 Extensions des Modeles Existants (Details)

#### Year (parametres) -- +4 champs

```python
# AJOUTER :
date_debut      DateField null=True, blank=True
date_fin        DateField null=True, blank=True
est_active      BooleanField default=False
est_cloturee    BooleanField default=False
```
Impact : NUL -- le code existant ne lit que `annee`.

#### Semestre (parametres) -- +3 champs

```python
# AJOUTER :
filiere         FK('scolarite.Filiere') null=True, blank=True
annee_univ      FK('parametres.Year') null=True, blank=True
credits         IntegerField default=30
```
**Logique double usage :**
- Semestres existants (S1 a S5) : templates de planification, `filiere=None`
- Semestres scolarite : instances avec `filiere` + `annee_univ` + `credits` remplis
- Emplois/Suivi utilisent les templates, la scolarite filtre par `filiere IS NOT NULL`

#### EM (em) -- +8 champs

```python
# AJOUTER :
credits             IntegerField null=True
coefficient         DecimalField(4,2) null=True
poids_cc            DecimalField(5,2) default=40.00
poids_tp            DecimalField(5,2) default=0.00
poids_exam          DecimalField(5,2) default=60.00
seuil_eliminatoire  DecimalField(4,2) default=6.00
est_element_module  BooleanField default=False
module_parent       FK('self') null=True, blank=True, related_name='elements'
responsable         FK('prof.Prof') null=True, blank=True
```
**Logique hierarchie dans EM :**
- EM existants : `est_element_module=False`, `module_parent=None` -> planification horaire
- Module LMD : `est_element_module=True`, `module_parent=None` -> conteneur avec credits/coeff
- Element LMD : `est_element_module=True`, `module_parent=<un module>` -> note evaluable
- Un element peut etre directement planifie dans l'emploi du temps (meme modele)

#### Etudiant (absence) -- +12 champs

```python
# AJOUTER :

# --- Identite bilingue FR/AR ---
nom_fr              CharField(200) blank=True, default=''   # backfill depuis `nom` existant
nom_ar              CharField(200) blank=True, default=''
prenom_fr           CharField(200) blank=True, default=''
prenom_ar           CharField(200) blank=True, default=''
lieu_naissance_fr   CharField(200) blank=True, default=''
lieu_naissance_ar   CharField(200) blank=True, default=''
nationalite_fr      CharField(50) default='Mauritanienne'
nationalite_ar      CharField(50) default='موريتانية'
adresse_fr          TextField blank=True, default=''
adresse_ar          TextField blank=True, default=''

# --- Identite mono-langue (pas de variante AR) ---
date_naissance      DateField null=True
cni                 CharField(20) null=True, blank=True
telephone           CharField(20) blank=True, default=''
email               EmailField blank=True, default=''
photo               ImageField null=True

# --- Scolarite ---
filiere             FK('scolarite.Filiere') null=True, blank=True
statut              CharField(20) default='actif'
                    choices=['actif','suspendu','diplome','exclu']
date_creation       DateTimeField auto_now_add=True
```

**Note bilingue** : meme pattern que `Institution` (section 7.2). Tous les champs
textuels d'identite existent en FR et AR pour supporter l'affichage dans les deux
langues (attestations, cartes etudiantes, releves, diplomes). Les champs techniques
(dates, email, CNI, telephone, photo, FK, enum) restent mono-langue — la
localisation se fait a l'affichage, pas au stockage.

**Strategie pour le champ existant `nom`** : identique a `Institution.nom` en 7.2.
Conserve en place, backfille via data migration (`UPDATE etudiant SET nom_fr = nom`),
et expose via un `@property` qui retourne `self.nom_fr` le temps que le frontend
migre. Aucun rename sec — zero rupture du serializer DRF.

**Un seul modele Etudiant** : les existants (4 champs) continuent de fonctionner,
les nouveaux ont les champs scolarite + identite bilingue remplis.

#### Departement -- +3 champs

```python
# AJOUTER :
institution         FK('parametres.Institution') null=True, blank=True
filiere             FK('scolarite.Filiere') null=True, blank=True
groupe              CharField(20) blank=True, default=''   # 'G1', 'G2', ''
```

#### Institution (parametres) -- +20 champs

Voir section 7.2 ci-dessus.

#### ROLE_CHOICES (authentication) -- +2 choix

```python
('responsable_filiere', 'Responsable de filiere'),
('jury_president',      'President de jury'),
```

#### UserPermission (authentication) -- +1 champ

```python
filiere = FK('scolarite.Filiere', null=True, blank=True)
```

---

### 7.3.bis Plan de Migration de Donnees (ordre d'execution)

> **Principe imperatif** : additif uniquement — `ALTER TABLE ADD COLUMN`, jamais de `DROP` ou `DELETE` sur les tables existantes.

| Etape | Action | Reversible | Fichier |
|-------|--------|------------|---------|
| **1** | `makemigrations` additif — cree `scolarite_filiere`, ajoute colonnes nullable sur `departement`, `absence_etudiant`, `em`, `institution`, `semestre`, `annee` | Oui | migrations generees |
| **2** | Data migration : `INSERT INTO scolarite_filiere (code, intitule_fr) SELECT DISTINCT code, code FROM departement WHERE code != ''` | Oui | data migration manuelle |
| **3** | Audit manuel des lignes ambigues (`SDID`, `SDID L2`) avant backfill | Oui | — |
| **4** | Data migration : `UPDATE departement SET filiere_id = ..., groupe = ...` via matching `code` et regex sur `nom` (jamais DELETE) | Oui | data migration manuelle |
| **5** | Data migration : `UPDATE institution SET nom_fr = nom` (backfill bilingue) | Oui | data migration manuelle |
| **6** | Data migration : `UPDATE absence_etudiant SET nom_fr = nom` | Oui | data migration manuelle |
| **7** | Migration structurelle `UserPermission.filiere` (nullable, safe) | Oui | deja dans migrations |
| **8** | Deprecation finale : supprimer `Institution.nom` et `Etudiant.nom` originaux **apres** que le frontend ait migre sur `nom_fr` | Non — uniquement apres validation frontend | migration future |

#### Script data migration type (etape 2)

```python
# apps/scolarite/migrations/0002_backfill_filiere_from_departement.py
from django.db import migrations

def create_filieres_from_departements(apps, schema_editor):
    Departement = apps.get_model('departement', 'Departement')
    Filiere     = apps.get_model('scolarite', 'Filiere')
    codes_vus   = set()
    for dep in Departement.objects.exclude(code='').order_by('code'):
        if dep.code not in codes_vus:
            Filiere.objects.get_or_create(
                code=dep.code,
                defaults={'intitule_fr': dep.code},
            )
            codes_vus.add(dep.code)
    # Lier les departements a leur filiere
    for dep in Departement.objects.exclude(code=''):
        filiere = Filiere.objects.filter(code=dep.code).first()
        if filiere and dep.filiere_id is None:
            dep.filiere = filiere
            dep.save(update_fields=['filiere'])

def reverse_create_filieres(apps, schema_editor):
    Filiere = apps.get_model('scolarite', 'Filiere')
    Filiere.objects.all().delete()

class Migration(migrations.Migration):
    dependencies = [
        ('scolarite', '0001_initial'),
        ('departement', '0002_departement_filiere_departement_groupe_and_more'),
    ]
    operations = [
        migrations.RunPython(create_filieres_from_departements, reverse_create_filieres),
    ]
```

#### Regles imperative de la migration

1. **Ne jamais supprimer un `Departement` existant** — les FK `CASCADE` sur `Etudiant` et `EM` provoqueraient une perte de donnees irreversible.
2. **Backfill `groupe` par regex** apres audit manuel des cas ambigus (`SDID`, `SDID L2`).
3. **`Institution.nom` conserve** via `@property nom_display` jusqu'a migration complete du frontend.
4. **Chaque etape est idempotente** — peut etre rejouee sans effet de bord si un `get_or_create` / `update_or_create` est utilise.
5. **Tester en staging avant production** — verifier les counts avant/apres chaque etape.

---

### 7.4 Nouvelles Applications et Modeles

#### App `scolarite` (1 modele)

```python
class Filiere(models.Model):
    """
    Filiere academique : SEA, SDID, Statistique, etc.
    Represente le PROGRAMME DE FORMATION, pas le groupe de planification.
    """
    code                CharField(20) unique
    intitule_fr         CharField(200)
    intitule_ar         CharField(200) blank=True
    type_diplome        CharField(10) choices=[
                            ('LP','Licence Professionnelle'),
                            ('LF','Licence Fondamentale'),
                            ('M','Master'),
                            ('ING','Ingenieur'),
                        ]
    nb_semestres        IntegerField default=6
    credits_total       IntegerField default=180
    est_active          BooleanField default=True
    responsable         FK('prof.Prof') null=True, blank=True
    institution         FK('parametres.Institution') null=True, blank=True
    date_creation       DateTimeField auto_now_add
    date_modification   DateTimeField auto_now

    class Meta:
        ordering = ['code']
        indexes = [
            models.Index(fields=['code', 'institution']),
        ]
```

#### App `inscriptions` (4 modeles)

```python
class Preinscription(models.Model):
    """Dossier de preinscription publique (sans authentification)."""
    # Reference
    numero_dossier      UUIDField default=uuid4, unique=True    # Token public de suivi
    annee_univ          FK(Year)
    filiere             FK(Filiere)

    # Identite
    nom                 CharField(100)
    prenom              CharField(100)
    date_naissance      DateField
    lieu_naissance      CharField(100)
    genre               CharField(1) choices=['M','F']
    nationalite         CharField(50) default='Mauritanienne'
    cni                 CharField(20)
    telephone           CharField(20)
    email               EmailField

    # Cursus
    bac_serie           CharField(50)
    bac_annee           IntegerField
    bac_mention         CharField(50) blank=True
    bac_moyenne         DecimalField(4,2) null=True

    # Documents uploades (stockes de maniere securisee)
    documents           JSONField default=list

    # Workflow
    statut              CharField(20) choices=[
                            'soumise','en_examen','acceptee','rejetee','inscrite'
                        ] default='soumise'
    motif_rejet         TextField blank=True
    date_soumission     DateTimeField auto_now_add
    examinee_par        FK(CustomUser) null=True
    date_examen         DateTimeField null=True

    class Meta:
        ordering = ['-date_soumission']
        indexes = [
            models.Index(fields=['statut', 'annee_univ']),
            models.Index(fields=['numero_dossier']),
        ]


class InscriptionAdministrative(models.Model):
    """Inscription admin : paiement + generation matricule."""
    etudiant            FK(Etudiant) on_delete=PROTECT
    annee_univ          FK(Year)
    filiere             FK(Filiere)
    niveau              IntegerField                     # 1, 2, 3
    numero_inscription  CharField(50) unique             # Auto-genere
    statut              CharField(20) choices=[
                            'en_cours','validee','annulee'
                        ] default='en_cours'
    montant_frais       DecimalField(10,2) default=0
    est_payee           BooleanField default=False
    date_paiement       DateField null=True
    recu_paiement       CharField(100) blank=True
    date_inscription    DateTimeField auto_now_add
    validee_par         FK(CustomUser) null=True

    class Meta:
        unique_together = ('etudiant', 'annee_univ')
        indexes = [
            models.Index(fields=['annee_univ', 'filiere', 'statut']),
        ]


class InscriptionPedagogique(models.Model):
    """Inscription a un semestre specifique."""
    inscription_admin   FK(InscriptionAdministrative) on_delete=PROTECT
    semestre            FK(Semestre) on_delete=PROTECT   # Semestre etendu avec filiere
    est_redoublant      BooleanField default=False
    date_inscription    DateTimeField auto_now_add
    validee_par         FK(CustomUser) null=True

    class Meta:
        unique_together = ('inscription_admin', 'semestre')


class InscriptionElement(models.Model):
    """Inscription a un element de module (gestion des dettes)."""
    inscription_ped     FK(InscriptionPedagogique) on_delete=PROTECT
    element             FK(EM) on_delete=PROTECT         # EM etendu (est_element_module=True)
    est_dette           BooleanField default=False
    annee_dette         FK(Year) null=True               # Annee d'origine si dette

    class Meta:
        unique_together = ('inscription_ped', 'element')
```

#### App `evaluations` (5 modeles + 1 service)

```python
class SessionEvaluation(models.Model):
    """Session d'examen (ouverture/cloture de saisie)."""
    annee_univ          FK(Year)
    type_session        CharField(20) choices=['normale','rattrapage']
    semestre            FK(Semestre) null=True            # null = tous les semestres
    date_debut_saisie   DateTimeField
    date_cloture_saisie DateTimeField
    est_ouverte         BooleanField default=False
    est_cloturee        BooleanField default=False

    class Meta:
        indexes = [
            models.Index(fields=['annee_univ', 'type_session']),
        ]


class Note(models.Model):
    """Note d'un etudiant sur un element de module."""
    etudiant            FK(Etudiant) on_delete=PROTECT
    element             FK(EM) on_delete=PROTECT         # EM etendu
    session             FK(SessionEvaluation) on_delete=PROTECT
    annee_univ          FK(Year)

    # Notes brutes (0-20)
    note_cc             DecimalField(4,2) null=True
    note_tp             DecimalField(4,2) null=True
    note_exam           DecimalField(4,2) null=True

    # Note calculee (par MoteurLMD)
    note_finale         DecimalField(4,2) null=True

    # Metadonnees
    est_absent          BooleanField default=False
    saisie_par          FK(CustomUser) related_name='notes_saisies'
    date_saisie         DateTimeField auto_now
    modifiee_par        FK(CustomUser) null=True, related_name='notes_modifiees'

    # Rachat jury
    est_rachetee        BooleanField default=False
    note_avant_rachat   DecimalField(4,2) null=True

    class Meta:
        unique_together = ('etudiant', 'element', 'session')
        indexes = [
            models.Index(fields=['session', 'element']),
            models.Index(fields=['etudiant', 'annee_univ']),
        ]

    def clean(self):
        """Validation metier : bornes 0-20, session non close."""
        if self.session.est_cloturee:
            raise ValidationError("Session de saisie cloturee.")
        for note in [self.note_cc, self.note_tp, self.note_exam]:
            if note is not None and not (0 <= note <= 20):
                raise ValidationError("Note entre 0 et 20.")


class Deliberation(models.Model):
    """Deliberation de jury avec workflow a etats."""
    session             FK(SessionEvaluation)
    semestre            FK(Semestre)
    filiere             FK(Filiere)
    date_deliberation   DateTimeField
    president_jury      FK(CustomUser) related_name='deliberations_presidees'
    statut              CharField(20) choices=[
                            'preparation','en_cours','validee','cloturee'
                        ] default='preparation'
    pv_fichier          FileField null=True
    pv_genere_le        DateTimeField null=True
    cloturee_par        FK(CustomUser) null=True, related_name='deliberations_cloturees'
    cloturee_le         DateTimeField null=True

    class Meta:
        unique_together = ('session', 'semestre', 'filiere')
        indexes = [
            models.Index(fields=['filiere', 'statut']),
        ]


class ParametreJury(models.Model):
    """Ajustements exceptionnels du jury."""
    deliberation                FK(Deliberation) on_delete=CASCADE
    seuil_validation_module     DecimalField(4,2) default=10.00
    seuil_validation_semestre   DecimalField(4,2) default=10.00
    seuil_compensation          DecimalField(4,2) default=8.00
    seuil_eliminatoire          DecimalField(4,2) default=6.00
    justification               TextField blank=True


class RachatNote(models.Model):
    """Rachat de note par le jury -- IMMUABLE apres creation."""
    deliberation        FK(Deliberation) on_delete=PROTECT
    note                FK(Note) on_delete=PROTECT
    ancienne_valeur     DecimalField(4,2)
    nouvelle_valeur     DecimalField(4,2)
    motif               TextField
    decidee_par         FK(CustomUser)
    date_decision       DateTimeField auto_now_add

    class Meta:
        indexes = [
            models.Index(fields=['deliberation']),
        ]
```

**Service MoteurLMD** (`apps/evaluations/services/moteur_lmd.py`) :

```python
class MoteurLMD:
    """
    Moteur de calcul conforme a l'Arrete 562.
    Fonctions PURES (sans effets de bord) pour faciliter les tests.
    """

    @staticmethod
    def calculer_moyenne_element(note, element):
        """Art. 12 : Moyenne ponderee CC + TP + Examen."""
        # (note_cc * poids_cc + note_tp * poids_tp + note_exam * poids_exam) / 100

    @staticmethod
    def est_element_eliminatoire(moyenne, seuil=Decimal('6')):
        """Art. 15 : < 6/20 = eliminatoire."""

    @staticmethod
    def appliquer_regle_maximum_rattrapage(moy_ordinaire, moy_rattrapage):
        """Art. 18 : L'etudiant garde la note superieure."""

    @staticmethod
    def calculer_moyenne_module(notes_elements, elements):
        """Art. 13 : Moyenne ponderee des elements du module.
        Retourne {moyenne, est_valide, est_bloquant, has_eliminatoire}"""

    @staticmethod
    def calculer_resultat_semestre(resultats_modules):
        """Art. 14, 15 : MG >= 10, tous modules >= 8, pas d'eliminatoire.
        Retourne {moyenne, est_valide, credits_capitalises, decision}"""

    @staticmethod
    def calculer_progression_annuelle(credits_annee, credits_capitalises,
                                       s1_s2_valides, demande_s5):
        """Art. 20 : >= 65% credits pour passer.
        Verrou S5 si S1+S2 non valides."""

    @staticmethod
    def verifier_eligibilite_diplome(etudiant):
        """Art. 25 : 180 credits + PFE >= 12/20 + tous semestres clotures."""

    @staticmethod
    def calculer_mention(moyenne_generale):
        """>= 16 Tres Bien, >= 14 Bien, >= 12 Assez Bien, >= 10 Passable."""
```

#### App `stages` (3 modeles)

```python
class ConventionStage(models.Model):
    etudiant            FK(Etudiant) on_delete=PROTECT
    semestre            FK(Semestre) on_delete=PROTECT   # S4 ou S6
    type_stage          CharField(10) choices=['S4','S6_PFE']

    # Entreprise
    entreprise_nom      CharField(200)
    entreprise_adresse  TextField
    tuteur_entreprise_nom    CharField(100)
    tuteur_entreprise_email  EmailField blank=True
    tuteur_entreprise_tel    CharField(20) blank=True

    # Encadrement academique
    tuteur_academique   FK(Prof) on_delete=SET_NULL, null=True

    # Sujet
    sujet               TextField
    objectifs           TextField blank=True

    # Periode
    date_debut          DateField
    date_fin            DateField

    # Fichiers
    convention_fichier  FileField null=True

    statut              CharField(20) choices=[
                            'brouillon','validee','en_cours','terminee','abandonnee'
                        ] default='brouillon'
    date_creation       DateTimeField auto_now_add

    class Meta:
        indexes = [
            models.Index(fields=['etudiant', 'type_stage']),
        ]


class EvaluationStage(models.Model):
    """Art. 11 : Stage evalue par 3 notes."""
    convention          OneToOneField(ConventionStage) on_delete=PROTECT
    note_soutenance     DecimalField(4,2) null=True
    note_memoire        DecimalField(4,2) null=True
    note_entreprise     DecimalField(4,2) null=True
    note_finale         DecimalField(4,2) null=True
    date_soutenance     DateField null=True
    jury                M2M(Prof) blank=True

    @property
    def est_valide_pfe(self):
        """Art. 25 : PFE S6 >= 12/20."""
        if self.convention.type_stage == 'S6_PFE':
            return self.note_finale and self.note_finale >= Decimal('12')
        return self.note_finale and self.note_finale >= Decimal('10')


class DerogationMedicale(models.Model):
    """Art. 23 : Gel d'annee sans compter le redoublement."""
    etudiant            FK(Etudiant)
    semestre            FK(Semestre)
    motif               TextField
    date_debut          DateField
    date_fin            DateField
    justificatif        FileField
    validee_par         FK(CustomUser) null=True
    est_approuvee       BooleanField default=False
    date_creation       DateTimeField auto_now_add
```

#### App `documents` (3 modeles)

```python
class NumeroSerieConfig(models.Model):
    """Configuration auto-increment par type de document et par institution."""
    institution         FK(Institution) on_delete=CASCADE
    type_document       CharField(30) choices=[
                            'attestation_inscription','releve_semestre',
                            'releve_complet','attestation_reussite','diplome'
                        ]
    prefixe             CharField(10)               # 'AI', 'RN', 'DLP'
    dernier_numero      IntegerField default=0
    nb_chiffres         IntegerField default=5

    class Meta:
        unique_together = ('institution', 'type_document')

    def generer_prochain(self):
        """Thread-safe via F() expression."""
        from django.db.models import F
        NumeroSerieConfig.objects.filter(pk=self.pk).update(
            dernier_numero=F('dernier_numero') + 1
        )
        self.refresh_from_db()
        annee = timezone.now().year
        return f"{self.prefixe}-{annee}-{str(self.dernier_numero).zfill(self.nb_chiffres)}"


class DocumentOfficiel(models.Model):
    """Document genere avec QR code de verification."""
    etudiant            FK(Etudiant) on_delete=PROTECT
    type_document       CharField(30)
    numero_serie        CharField(50) unique
    fichier             FileField upload_to='documents_officiels/'
    qr_code_token       UUIDField default=uuid4, unique=True, db_index=True
    hash_document       CharField(64) null=True     # SHA-256
    genere_par          FK(CustomUser)
    annee_univ          FK(Year) null=True
    semestre            FK(Semestre) null=True
    est_valide          BooleanField default=True
    date_generation     DateTimeField auto_now_add

    class Meta:
        indexes = [
            models.Index(fields=['etudiant', 'type_document']),
        ]


class RegistreDiplome(models.Model):
    """Registre INVIOLABLE -- APPEND-ONLY, pas d'UPDATE/DELETE."""
    document            OneToOneField(DocumentOfficiel) on_delete=PROTECT
    etudiant            FK(Etudiant) on_delete=PROTECT
    filiere             FK(Filiere) on_delete=PROTECT
    numero_diplome      CharField(50) unique
    mention             CharField(20)
    moyenne_generale    DecimalField(4,2)
    credits_valides     IntegerField
    date_delivrance     DateField
    date_enregistrement DateTimeField auto_now_add

    class Meta:
        indexes = [
            models.Index(fields=['etudiant']),
            models.Index(fields=['numero_diplome']),
        ]
```

#### App `notifications` (1 modele)

```python
class Notification(models.Model):
    destinataire        FK(CustomUser) on_delete=CASCADE
    titre               CharField(200)
    message             TextField
    type_notif          CharField(20) choices=[
                            'info','warning','action','success'
                        ] default='info'
    lue                 BooleanField default=False
    lien                CharField(200) blank=True
    date_creation       DateTimeField auto_now_add
    date_lecture        DateTimeField null=True

    class Meta:
        ordering = ['-date_creation']
        indexes = [
            models.Index(fields=['destinataire', 'lue']),
        ]
```

#### Core - `AuditLog` (tracabilite DB)

```python
# core/models.py (NOUVEAU)
class AuditLog(models.Model):
    """
    Journal d'audit en base de donnees.
    Complete le logging fichier existant (AuditMixin).
    Table APPEND-ONLY : pas d'UPDATE/DELETE via l'ORM.
    """
    user                FK(CustomUser) null=True, on_delete=SET_NULL
    action              CharField(10)               # CREATE, UPDATE, DELETE
    model_name          CharField(100)              # 'Note', 'Deliberation', etc.
    object_id           CharField(50)
    changes             JSONField default=dict       # {field: {old: x, new: y}}
    ip_address          GenericIPAddressField null=True
    user_agent          TextField blank=True
    timestamp           DateTimeField auto_now_add, db_index=True

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['model_name', 'object_id']),
            models.Index(fields=['user', 'timestamp']),
        ]
```

---

### 7.5 Bonnes Pratiques Transversales

#### Securite

| Mesure | Implementation |
|--------|---------------|
| **Notes immuables post-PV** | Middleware verifiant `deliberation.statut == 'cloturee'` avant tout PATCH/PUT sur Note |
| **RegistreDiplome inviolable** | Override `perform_update()` et `perform_destroy()` -> retourne 405 |
| **AuditLog append-only** | Meme pattern : pas de update/delete autorise |
| **Upload securise** | Validation MIME type + taille max + scan extension (deja en place pour avatars) |
| **Chiffrement documents** | FileField avec storage chiffre (django-storages + S3/KMS en prod) |
| **QR token non predictible** | UUIDv4 (128 bits d'entropie) |
| **Hash SHA-256 des PDF** | Stocke sur DocumentOfficiel, verifiable a posteriori |
| **Rate limiting endpoints publics** | AnonRateThrottle sur preinscription et verification QR |
| **Validation notes 0-20** | `clean()` sur Note + contrainte DB `CHECK(note >= 0 AND note <= 20)` |

#### Scalabilite et Performance

| Mesure | Implementation |
|--------|---------------|
| **Index DB** | Sur toutes les colonnes de filtrage frequent (voir `class Meta: indexes` ci-dessus) |
| **select_related / prefetch_related** | Obligatoire sur toutes les vues avec FK (pattern deja en place) |
| **Cache institution** | 1h TTL sur l'endpoint public (change rarement) |
| **Cache RBAC** | Deja en place (5min TTL, versionnement) |
| **Pagination** | StandardPagination (10/page) sur toutes les listes |
| **NumeroSerie thread-safe** | `F()` expression pour increment atomique (pas de race condition) |
| **Generation PDF async** | Celery task pour PV/diplomes (optionnel, pdfkit suffit pour <100 etudiants) |
| **Bulk operations** | `bulk_create()` / `bulk_update()` pour import notes/etudiants |
| **Requetes N+1** | Detectees via django-debug-toolbar en dev (deja configure) |

#### Tracabilite

| Element | Mecanisme |
|---------|-----------|
| **Toute action CRUD** | AuditMixin (fichier) + AuditLog (DB) |
| **Modifications de notes** | `modifiee_par` + `date_saisie` + AuditLog avec old/new values |
| **Rachats jury** | RachatNote immuable + AuditLog |
| **Documents generes** | SHA-256 + QR token + numero serie |
| **Diplomes delivres** | RegistreDiplome append-only |
| **Connexions** | django-axes (IP, timestamp, succes/echec) |
| **Qui a valide quoi** | `validee_par`, `cloturee_par`, `saisie_par` sur chaque modele |

---

### 7.6 Nouveaux Endpoints API

```
# --- SCOLARITE ---
GET/POST   /api/v1/scolarite/filieres/
GET/PUT/DEL /api/v1/scolarite/filieres/{id}/

# --- INSCRIPTIONS ---
POST       /api/v1/inscriptions/preinscriptions/          # PUBLIC (AllowAny)
GET        /api/v1/inscriptions/preinscriptions/{token}/   # PUBLIC suivi dossier
GET/PATCH  /api/v1/inscriptions/preinscriptions/           # Staff : liste + decision
POST       /api/v1/inscriptions/administratives/
GET        /api/v1/inscriptions/administratives/
POST       /api/v1/inscriptions/pedagogiques/
POST       /api/v1/inscriptions/elements/

# --- EVALUATIONS ---
GET/POST   /api/v1/evaluations/sessions/
POST       /api/v1/evaluations/notes/                      # Saisie individuelle
POST       /api/v1/evaluations/notes/bulk/                 # Saisie en masse
POST       /api/v1/evaluations/notes/import/               # Import Excel
GET        /api/v1/evaluations/calcul/{semestre_id}/       # Declenchement MoteurLMD
GET/POST   /api/v1/evaluations/deliberations/
PATCH      /api/v1/evaluations/deliberations/{id}/cloturer/
GET        /api/v1/evaluations/deliberations/{id}/pv/      # Telecharger PV PDF
POST       /api/v1/evaluations/rachats/

# --- STAGES ---
GET/POST   /api/v1/stages/conventions/
GET/POST   /api/v1/stages/evaluations/
GET/POST   /api/v1/stages/derogations/

# --- DOCUMENTS ---
POST       /api/v1/documents/generer/                      # Attestation, releve, diplome
GET        /api/v1/documents/verify/{qr_token}/            # PUBLIC (AllowAny)
GET        /api/v1/documents/registre/                     # Registre diplomes (read-only)
GET        /api/v1/documents/etudiant/{id}/                # Documents d'un etudiant

# --- NOTIFICATIONS ---
GET        /api/v1/notifications/
PATCH      /api/v1/notifications/{id}/lire/
POST       /api/v1/notifications/tout-lire/

# --- INSTITUTION (extension) ---
GET        /api/v1/parametres/institution/active/          # PUBLIC, cache 1h
```

### 7.7 Nouveaux Modules RBAC

```
scolarite_filieres      -> voir, modifier, supprimer
scolarite_etudiants     -> voir, modifier, supprimer, exporter
inscriptions            -> voir, modifier, supprimer
evaluations_notes       -> voir, modifier, exporter
evaluations_delib       -> voir, modifier
stages                  -> voir, modifier, supprimer
documents               -> voir, modifier, exporter
notifications           -> voir, modifier
```

### 7.8 Migration des Donnees Existantes

#### Etape 1 : Creer les Filieres depuis les Departements

```python
# Script de migration (data migration Django)
def creer_filieres_depuis_departements(apps, schema_editor):
    Departement = apps.get_model('departement', 'Departement')
    Filiere = apps.get_model('scolarite', 'Filiere')

    # Extraire les codes uniques (SEA, SDID, Stat)
    codes = Departement.objects.exclude(
        niveau__niveau='Transversal'
    ).values_list('code', flat=True).distinct()

    for code in codes:
        dept = Departement.objects.filter(code=code).first()
        Filiere.objects.get_or_create(
            code=code,
            defaults={
                'intitule_fr': dept.nom.split(' ')[0],  # A ajuster manuellement
                'type_diplome': 'LP',
            }
        )

    # Lier les departements a leurs filieres
    for filiere in Filiere.objects.all():
        Departement.objects.filter(code=filiere.code).update(filiere=filiere)
```

#### Etape 2 : Renseigner les groupes

```python
def extraire_groupes(apps, schema_editor):
    Departement = apps.get_model('departement', 'Departement')
    for dept in Departement.objects.all():
        nom = dept.nom
        if 'G1' in nom:
            dept.groupe = 'G1'
        elif 'G2' in nom:
            dept.groupe = 'G2'
        dept.save()
```

---

### 7.9 Phases d'Implementation

```
PHASE 0 : Fondations (1 semaine)
  [x] Existant : JWT, RBAC, Throttling, AuditMixin, PDF, Excel
  [ ] A faire :
      - Creer core/models.py avec AuditLog
      - Ajouter 'core' a INSTALLED_APPS
      - Etendre Institution (+20 champs bilingues, logo, signataire)
      - Etendre Year (+4 champs)
      - Ajouter roles responsable_filiere et jury_president
      - Ajouter champ filiere a UserPermission
      - Upgrader AuditMixin pour ecrire en DB + fichier
      - Endpoint /api/v1/parametres/institution/active/ (public, cache)

PHASE 1 : Structure LMD + Filieres (2 semaines)
  [x] Existant : Departement, Niveau, Semestre, EM, Prof, Etudiant
  [ ] A faire :
      - Creer apps/scolarite/ avec Filiere
      - Etendre Departement (+institution, +filiere, +groupe)
      - Etendre Semestre (+filiere, +annee_univ, +credits)
      - Etendre EM (+credits, +coefficient, +poids, +module_parent, +est_element_module)
      - Etendre Etudiant (+prenom, +date_naissance, +filiere, +12 champs)
      - Data migration : creer Filieres depuis Departements existants
      - Enregistrer modules RBAC via data migration
      - ViewSets + Serializers pour Filiere
      - Import Excel etudiants enrichi

PHASE 2 : Inscriptions (2 semaines)
  [ ] A faire :
      - Creer apps/inscriptions/
      - Endpoint public preinscription (AllowAny, throttle)
      - Service generation matricule (thread-safe)
      - Workflow : preinscription -> administrative -> pedagogique
      - Gestion automatique des dettes (InscriptionElement.est_dette)
      - Verrou 3eme annee (Art. 20)
      - Validation : paiement requis avant inscription pedagogique

PHASE 3 : Evaluations et Notes (3 semaines)
  [ ] A faire :
      - Creer apps/evaluations/
      - Saisie notes (individuelle + bulk + import Excel)
      - Validation 0-20 + contraintes DB CHECK
      - MoteurLMD avec TOUS les calculs Arrete 562
      - Tests unitaires exhaustifs (>= 30 cas, couverture 90%)
      - Workflow deliberation (preparation -> en_cours -> validee -> cloturee)
      - Rachats jury avec audit complet
      - Verrouillage notes post-cloture
      - Generation PV en PDF (logo + signataire depuis Institution)

PHASE 4 : Stages et PFE (1 semaine)
  [x] Existant : Prof (tuteur academique)
  [ ] A faire :
      - Creer apps/stages/
      - Convention stage CRUD + generation PDF convention
      - Evaluation stage (3 notes)
      - Integration MoteurLMD (PFE S6 >= 12/20)
      - Derogation medicale (Art. 23)

PHASE 5 : Documents Officiels (2 semaines)
  [x] Existant : pdfkit (infrastructure PDF), Institution etendue
  [ ] A faire :
      - Creer apps/documents/
      - NumeroSerie thread-safe (F() expression)
      - Generation QR code (ajouter lib qrcode)
      - Templates PDF bilingues FR/AR (en-tete depuis Institution)
      - Attestation inscription, releve notes, attestation reussite, diplome
      - Endpoint public verification QR (AllowAny, throttle)
      - RegistreDiplome append-only (405 sur update/delete)
      - Hash SHA-256 de chaque PDF genere

PHASE 6 : Notifications + Tests (1 semaine)
  [ ] A faire :
      - Creer apps/notifications/
      - Helpers de creation lies aux workflows (inscription, notes, deliberation)
      - Endpoint liste + mark-as-read + tout-lire
      - Tests d'integration end-to-end
      - Tests de charge (locust) sur endpoints critiques

TOTAL : ~12 semaines (3 mois) pour l'integration complete
```

---

### 7.10 Dependances a Ajouter

```
# requirements.txt - AJOUTS
qrcode[pil]>=7.4        # Generation QR codes pour documents
hashlib                  # SHA-256 (stdlib, deja disponible)

# Recommande :
celery>=5.3              # Taches asynchrones (generation PDF lourds)
django-redis>=5.4        # Cache Redis en production
locust>=2.20             # Tests de charge

# Optionnel :
weasyprint>=60.0         # Alternative a pdfkit (meilleur support CSS, pas besoin de wkhtmltopdf)
```

---

## Resume

| Metrique | Valeur |
|----------|--------|
| **Apps existantes** | 12 |
| **Modeles existants** | 29 |
| **Apps a creer** | 6 (scolarite, inscriptions, evaluations, stages, documents, notifications) |
| **Modeles a creer** | 17 |
| **Modeles existants a etendre** | 6 (Institution +20, Year +4, Semestre +3, EM +8, Etudiant +12, Departement +3) |
| **Code existant casse** | 0 lignes (tous les ajouts sont nullable/default) |
| **Endpoints existants impactes** | 0 |
| **Nouveaux endpoints** | ~30 |
| **Nouveaux modules RBAC** | 8 |
| **Duree estimee** | ~12 semaines |
| **Multi-institution** | Oui -- tout pilote par parametrage Institution |
| **Bilingue FR/AR** | Oui -- champs _fr/_ar sur Institution, Filiere |
