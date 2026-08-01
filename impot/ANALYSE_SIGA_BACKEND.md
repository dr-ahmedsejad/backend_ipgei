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

### 6.1 Ce qui peut etre reutilise TEL QUEL

| Existant | Reutilisation pour Scolarite |
|----------|------|
| `CustomUser` | Utilisateurs staff (admin, scolarite, DE, etc.) |
| `Departement` | Parent des filieres LMD |
| `Salle` | Salles d'examen |
| `Niveau` | Niveaux academiques (Licence, Master) |
| `Prof` | Enseignants LMD, tuteurs academiques de stage |
| `Institution` | Hierarchie institutionnelle |
| `Banque` | Paiement des vacataires (inchange) |
| Systeme RBAC complet | Permissions pour les nouveaux modules scolarite |
| JWT Authentication | Authentification identique |
| AuditMixin | Logging des actions scolarite |
| SelectAllMixin | Listes de reference |
| PDF Generation (pdfkit) | PV, releves, attestations, diplomes |
| Excel Import (openpyxl) | Import etudiants, notes |

### 6.2 Ce qui DOIT etre etendu (ajout de champs)

| Modele | Champs a Ajouter | Impact |
|--------|------------------|--------|
| `Year` | `date_debut`, `date_fin`, `est_active`, `est_cloturee` | **NUL** - Tous nullable/defaut. Le code existant ne lit que `annee`. |
| `ROLE_CHOICES` | `'responsable_filiere'`, `'jury_president'` | **NUL** - Ajout de choix. Les roles existants sont inchanges. |
| `UserPermission` | `filiere` FK (nullable) | **NUL** - Champ additionnel optionnel pour scope par filiere. |

### 6.3 Ce qui NE DOIT PAS etre modifie (zones de conflit)

| Modele | Pourquoi ne pas modifier | Alternative LMD |
|--------|--------------------------|-----------------|
| `Semestre` (parametres) | Utilise dans Emplois, Suivie, SuiviePointage avec synchronisation legacy CharField. Represente un semestre de PLANIFICATION (Pair/Impair), pas un semestre academique. | Nouveau modele `SemestreLMD` avec credits, filiere FK |
| `EM` (em) | Utilise dans Emplois, Suivie, Vacation, Avancement. Represente un cours pour la planification horaire (CM/TD/TP heures). Pas de credits ni coefficients. | Nouveaux modeles `ModuleLMD` + `ElementModule` avec bridge FK optionnel vers EM |
| `Etudiant` (absence) | Modele minimaliste (4 champs) couple au systeme d'absence. Pas de prenom, date de naissance, filiere, etc. | Nouveau modele `EtudiantLMD` complet avec bridge FK optionnel |

### 6.4 Ce qui est ENTIEREMENT NOUVEAU

| Domaine | Modeles a creer |
|---------|-----------------|
| **Structure LMD** | Filiere, SemestreLMD, ModuleLMD, ElementModule, EtudiantLMD |
| **Inscriptions** | Preinscription, InscriptionAdministrative, InscriptionPedagogique, InscriptionElement |
| **Evaluations** | SessionEvaluation, Note, Deliberation, ParametreJury, RachatNote |
| **Stages** | ConventionStage, EvaluationStage, DerogationMedicale |
| **Documents** | DocumentOfficiel, NumeroSerieConfig, RegistreDiplome |
| **Notifications** | Notification |
| **Audit** | AuditLog (table DB, remplace le logging fichier pour les actions critiques) |

---

## 7. Plan d'Amelioration et d'Integration

### 7.1 Nouvelles Applications Django a Creer

```
apps/
  scolarite/           # Structure LMD : Filiere, SemestreLMD, ModuleLMD, ElementModule, EtudiantLMD
  inscriptions/        # Preinscription, InscriptionAdmin, InscriptionPed, InscriptionElement
  evaluations/         # SessionEvaluation, Note, MoteurLMD, Deliberation, RachatNote
  stages/              # ConventionStage, EvaluationStage, DerogationMedicale
  documents/           # DocumentOfficiel, NumeroSerieConfig, RegistreDiplome
  notifications/       # Notification
```

### 7.2 Nouveaux Endpoints API

```
/api/v1/scolarite/filieres/              # CRUD Filieres
/api/v1/scolarite/semestres-lmd/         # CRUD SemestreLMD
/api/v1/scolarite/modules-lmd/           # CRUD ModuleLMD
/api/v1/scolarite/elements/              # CRUD ElementModule
/api/v1/scolarite/etudiants/             # CRUD EtudiantLMD + import Excel

/api/v1/inscriptions/preinscriptions/    # CRUD + endpoint public (AllowAny) pour soumission
/api/v1/inscriptions/administratives/    # CRUD + generation matricule
/api/v1/inscriptions/pedagogiques/       # CRUD + gestion dettes automatique
/api/v1/inscriptions/elements/           # Inscriptions aux elements

/api/v1/evaluations/sessions/            # CRUD SessionEvaluation
/api/v1/evaluations/notes/               # Saisie notes (bulk, import Excel)
/api/v1/evaluations/deliberations/       # Workflow deliberation
/api/v1/evaluations/rachats/             # Rachats jury
/api/v1/evaluations/calcul/{semestre}/   # Declenchement MoteurLMD

/api/v1/stages/conventions/              # CRUD ConventionStage
/api/v1/stages/evaluations/              # Evaluation stage (3 notes)
/api/v1/stages/derogations/              # Derogations medicales

/api/v1/documents/generer/               # Generation documents
/api/v1/documents/verify/{hash}/         # PUBLIC (AllowAny) - verification QR
/api/v1/documents/registre/              # Registre diplomes (read-only)

/api/v1/notifications/                   # Liste + mark-as-read
```

### 7.3 Nouveaux Modules RBAC a Enregistrer

```
scolarite_filieres      -> voir, modifier, supprimer
scolarite_semestres     -> voir, modifier, supprimer
scolarite_modules       -> voir, modifier, supprimer
scolarite_etudiants     -> voir, modifier, supprimer, exporter
inscriptions            -> voir, modifier, supprimer
evaluations_notes       -> voir, modifier, exporter
evaluations_delib       -> voir, modifier
stages                  -> voir, modifier, supprimer
documents               -> voir, modifier, exporter
notifications           -> voir, modifier
```

### 7.4 Nouveaux Modeles Detailles

#### App `scolarite`

```python
class Filiere(models.Model):
    departement     FK(Departement)           # Reutilise le modele existant
    code            CharField(20) unique
    intitule        CharField(200)
    type_diplome    CharField(10) choices=['LP','LF','M','ING']
    nb_semestres    IntegerField default=6
    credits_total   IntegerField default=180
    est_active      BooleanField default=True
    responsable     FK(Prof) null=True         # Reutilise le modele existant
    date_creation   DateTimeField auto_now_add

class SemestreLMD(models.Model):
    filiere         FK(Filiere)
    numero          IntegerField               # 1 a 6
    credits         IntegerField default=30    # Art. 7 : 30 credits/semestre
    annee_univ      FK(Year)                   # Reutilise parametres.Year
    semestre_emplois FK(Semestre) null=True     # Bridge vers planification
    unique_together = ('filiere', 'numero', 'annee_univ')

class ModuleLMD(models.Model):
    semestre_lmd    FK(SemestreLMD)
    code            CharField(20)
    intitule        CharField(200)
    credits         IntegerField
    coefficient     DecimalField(4,2)
    est_stage       BooleanField default=False  # Module stage S4/S6
    unique_together = ('code', 'semestre_lmd')

class ElementModule(models.Model):
    module          FK(ModuleLMD)
    code            CharField(20)
    intitule        CharField(200)
    coefficient     DecimalField(4,2)
    volume_cm       IntegerField default=0
    volume_td       IntegerField default=0
    volume_tp       IntegerField default=0
    poids_cc        DecimalField(5,2) default=40.00   # Pourcentage CC
    poids_tp        DecimalField(5,2) default=0.00
    poids_exam      DecimalField(5,2) default=60.00
    seuil_eliminatoire DecimalField(4,2) default=6.00  # Art. 15 : < 6 = eliminatoire
    em              FK(EM) null=True                    # Bridge vers planification
    responsable     FK(Prof) null=True

class EtudiantLMD(models.Model):
    matricule       CharField(50) unique
    nom             CharField(200)
    prenom          CharField(200)
    date_naissance  DateField
    lieu_naissance  CharField(200)
    nationalite     CharField(50) default='Mauritanienne'
    cni             CharField(20) null=True
    genre           CharField(1) choices=['M','F']
    adresse         TextField blank=True
    telephone       CharField(20) blank=True
    email           EmailField blank=True
    photo           ImageField null=True
    filiere         FK(Filiere)
    statut          CharField(20) choices=['actif','suspendu','diplome','exclu']
    etudiant_legacy FK(absence.Etudiant) null=True      # Bridge vers absence
    date_creation   DateTimeField auto_now_add
```

#### App `inscriptions`

```python
class Preinscription(models.Model):
    annee_univ      FK(Year)
    filiere         FK(Filiere)
    nom, prenom     CharField
    date_naissance  DateField
    genre           CharField(1)
    cni             CharField(20)
    telephone       CharField(20)
    email           EmailField
    bac_serie       CharField(50)
    bac_annee       IntegerField
    bac_mention     CharField(50) blank=True
    bac_moyenne     DecimalField(4,2) null=True
    documents       JSONField default=list           # References fichiers uploades
    statut          CharField choices=['soumise','en_examen','acceptee','rejetee','inscrite']
    motif_rejet     TextField blank=True
    date_soumission DateTimeField auto_now_add
    examinee_par    FK(CustomUser) null=True

class InscriptionAdministrative(models.Model):
    etudiant        FK(EtudiantLMD)
    annee_univ      FK(Year)
    filiere         FK(Filiere)
    niveau          IntegerField                     # 1, 2, 3
    numero_inscription CharField(50) unique           # Auto-genere
    statut          CharField choices=['en_cours','validee','annulee']
    montant_frais   DecimalField(10,2)
    est_payee       BooleanField default=False
    date_inscription DateTimeField auto_now_add
    unique_together = ('etudiant', 'annee_univ')

class InscriptionPedagogique(models.Model):
    inscription_admin FK(InscriptionAdministrative)
    semestre_lmd    FK(SemestreLMD)
    est_redoublant  BooleanField default=False
    date_inscription DateTimeField auto_now_add
    unique_together = ('inscription_admin', 'semestre_lmd')

class InscriptionElement(models.Model):
    inscription_ped FK(InscriptionPedagogique)
    element_module  FK(ElementModule)
    est_dette       BooleanField default=False        # Report annee precedente
    unique_together = ('inscription_ped', 'element_module')
```

#### App `evaluations`

```python
class SessionEvaluation(models.Model):
    annee_univ      FK(Year)
    type_session    CharField choices=['normale','rattrapage']
    semestre_lmd    FK(SemestreLMD) null=True
    date_debut      DateField
    date_fin        DateField
    est_ouverte     BooleanField default=False
    est_cloturee    BooleanField default=False

class Note(models.Model):
    inscription_element FK(InscriptionElement)
    session         FK(SessionEvaluation)
    note_cc         DecimalField(4,2) null=True        # 0-20
    note_tp         DecimalField(4,2) null=True
    note_exam       DecimalField(4,2) null=True
    note_finale     DecimalField(4,2) null=True        # Calculee par MoteurLMD
    est_absent      BooleanField default=False
    saisie_par      FK(CustomUser)
    date_saisie     DateTimeField auto_now
    unique_together = ('inscription_element', 'session')

class Deliberation(models.Model):
    session         FK(SessionEvaluation)
    semestre_lmd    FK(SemestreLMD)
    filiere         FK(Filiere)
    date_deliberation DateTimeField
    president_jury  FK(CustomUser)
    statut          CharField choices=['preparation','en_cours','validee','cloturee']
    pv_genere       BooleanField default=False
    pv_fichier      FileField null=True
    unique_together = ('session', 'semestre_lmd', 'filiere')

class ParametreJury(models.Model):
    deliberation    FK(Deliberation)
    seuil_validation_module   DecimalField(4,2) default=10.00
    seuil_validation_semestre DecimalField(4,2) default=10.00
    seuil_compensation        DecimalField(4,2) default=8.00

class RachatNote(models.Model):
    deliberation    FK(Deliberation)
    note            FK(Note)
    ancienne_valeur DecimalField(4,2)
    nouvelle_valeur DecimalField(4,2)
    motif           TextField
    decidee_par     FK(CustomUser)
    date_decision   DateTimeField auto_now_add
```

#### App `stages`

```python
class ConventionStage(models.Model):
    etudiant        FK(EtudiantLMD)
    semestre_lmd    FK(SemestreLMD)              # S4 ou S6
    entreprise_nom  CharField(200)
    entreprise_adresse TextField
    tuteur_entreprise_nom CharField(100)
    tuteur_entreprise_email EmailField blank=True
    tuteur_entreprise_tel CharField(20) blank=True
    tuteur_academique FK(Prof)
    sujet           TextField
    date_debut      DateField
    date_fin        DateField
    convention_fichier FileField null=True
    statut          CharField choices=['brouillon','validee','en_cours','terminee','abandonnee']

class EvaluationStage(models.Model):
    convention      OneToOneField(ConventionStage)
    note_soutenance DecimalField(4,2) null=True
    note_memoire    DecimalField(4,2) null=True
    note_entreprise DecimalField(4,2) null=True
    note_finale     DecimalField(4,2) null=True
    date_soutenance DateField null=True
    jury            M2M(Prof)

class DerogationMedicale(models.Model):
    etudiant        FK(EtudiantLMD)
    semestre_lmd    FK(SemestreLMD)
    motif           TextField
    date_debut      DateField
    date_fin        DateField
    justificatif    FileField
    validee_par     FK(CustomUser) null=True
    est_approuvee   BooleanField default=False
```

#### App `documents`

```python
class NumeroSerieConfig(models.Model):
    type_document   CharField unique choices=['attestation_inscription','releve_semestre',
                    'releve_complet','attestation_reussite','diplome']
    prefixe         CharField(10)                    # ex: 'AI', 'RN', 'DLP'
    dernier_numero  IntegerField default=0
    nb_chiffres     IntegerField default=5           # ex: 00042

class DocumentOfficiel(models.Model):
    etudiant        FK(EtudiantLMD)
    type_document   CharField choices=[...]
    numero_serie    CharField(50) unique              # Auto-genere
    fichier         FileField
    qr_code_hash    UUIDField unique default=uuid4    # Token verification
    hash_document   CharField(64) null=True           # SHA-256
    genere_par      FK(CustomUser)
    annee_univ      FK(Year)
    semestre_lmd    FK(SemestreLMD) null=True
    est_valide      BooleanField default=True
    date_generation DateTimeField auto_now_add

class RegistreDiplome(models.Model):
    document        OneToOneField(DocumentOfficiel)
    etudiant        FK(EtudiantLMD)
    filiere         FK(Filiere)
    numero_diplome  CharField(50) unique              # ex: 'DLP-MRT-2026-00042'
    mention         CharField(20)
    moyenne_generale DecimalField(4,2)
    credits_valides IntegerField
    date_delivrance DateField
    date_enregistrement DateTimeField auto_now_add
    # APPEND-ONLY : pas de update/delete autorise
```

#### App `notifications`

```python
class Notification(models.Model):
    destinataire    FK(CustomUser)
    titre           CharField(200)
    message         TextField
    type_notif      CharField(20) choices=['info','warning','action','success']
    lue             BooleanField default=False
    lien            CharField(200) blank=True         # URL relative
    date_creation   DateTimeField auto_now_add
    date_lecture    DateTimeField null=True
```

#### Core - `AuditLog`

```python
# core/models.py (NOUVEAU)
class AuditLog(models.Model):
    user            FK(CustomUser) null=True SET_NULL
    action          CharField(10)                     # CREATE, UPDATE, DELETE
    model_name      CharField(100)                    # ex: 'Note', 'Deliberation'
    object_id       CharField(50)
    changes         JSONField default=dict            # {field: {old, new}}
    ip_address      GenericIPAddressField null=True
    timestamp       DateTimeField auto_now_add
    # Combinaison avec le logging fichier existant
```

### 7.5 MoteurLMD - Service de Calcul

```
Fichier : apps/evaluations/services/moteur_lmd.py

Fonctions pures (sans effets de bord) pour faciliter les tests :

calculer_moyenne_element(note, element)
  -> (note_cc * poids_cc + note_tp * poids_tp + note_exam * poids_exam) / 100
  -> Retourne Decimal arrondi a 2 decimales

est_element_eliminatoire(moyenne, seuil=6.0)
  -> moyenne < seuil

calculer_moyenne_module(notes_elements, elements)
  -> Moyenne ponderee par coefficients
  -> Retourne {moyenne, est_valide, est_bloquant, has_eliminatoire}

calculer_resultat_semestre(resultats_modules)
  -> Art. 14, 15 : MG >= 10, tous modules >= 8, pas d'eliminatoire
  -> Retourne {moyenne, est_valide, credits_capitalises, decision}

appliquer_regle_maximum_rattrapage(moy_ordinaire, moy_rattrapage)
  -> Art. 18 : max(ordinaire, rattrapage)

calculer_progression_annuelle(credits_annee, credits_capitalises, s1_s2_valides, demande_s5)
  -> Art. 20 : >= 65% credits pour passer
  -> Art. 20 : Verrou S5 si S1+S2 non valides

verifier_eligibilite_diplome(etudiant)
  -> Art. 25 : 180 credits + PFE >= 12/20 + tous semestres clotures

calculer_mention(moyenne_generale)
  -> >= 16 : Tres Bien, >= 14 : Bien, >= 12 : Assez Bien, >= 10 : Passable
```

### 7.6 Modifications aux Modeles Existants

#### Year (parametres/models.py) - AJOUT DE CHAMPS

```python
# AJOUTER (tous nullable/defaut, zero impact sur le code existant) :
date_debut      DateField null=True, blank=True
date_fin        DateField null=True, blank=True
est_active      BooleanField default=False
est_cloturee    BooleanField default=False
```

#### ROLE_CHOICES (authentication/models.py) - AJOUT DE CHOIX

```python
# AJOUTER a la liste existante :
('responsable_filiere', 'Responsable de filiere'),
('jury_president',      'President de jury'),
```

#### UserPermission (authentication/models.py) - AJOUT DE CHAMP

```python
# AJOUTER (nullable, zero impact) :
filiere = models.ForeignKey(
    'scolarite.Filiere', on_delete=models.SET_NULL,
    null=True, blank=True, related_name='user_permissions_rbac',
)
```

### 7.7 Phases d'Implementation

```
PHASE 0 : Fondations (1 semaine)
  [x] Existant : JWT, RBAC, Throttling, AuditMixin, PDF, Excel
  [ ] A faire :
      - Creer core/models.py avec AuditLog
      - Ajouter 'core' a INSTALLED_APPS
      - Etendre Year avec date_debut, date_fin, est_active, est_cloturee
      - Ajouter roles responsable_filiere et jury_president
      - Ajouter champ filiere a UserPermission
      - Upgrader AuditMixin pour ecrire en DB + fichier

PHASE 1 : Structure LMD (2 semaines)
  [x] Existant : Departement, Niveau, Prof, Salle
  [ ] A faire :
      - Creer apps/scolarite/ (Filiere, SemestreLMD, ModuleLMD, ElementModule, EtudiantLMD)
      - ViewSets + Serializers + URLs
      - Enregistrer modules RBAC via data migration
      - Import Excel etudiants
      - Validation Art. 7 (30 credits/semestre), Art. 8 (structure semestres)

PHASE 2 : Inscriptions (2 semaines)
  [ ] A faire :
      - Creer apps/inscriptions/
      - Endpoint public preinscription (AllowAny)
      - Service generation matricule
      - Workflow inscription : preinscription -> administrative -> pedagogique
      - Gestion automatique des dettes (InscriptionElement.est_dette)
      - Verrou 3eme annee (Art. 20)

PHASE 3 : Evaluations et Notes (3 semaines)
  [ ] A faire :
      - Creer apps/evaluations/
      - Saisie notes (individuelle + bulk + import Excel)
      - MoteurLMD avec TOUS les calculs Arrete 562
      - Tests unitaires exhaustifs (>= 30 cas)
      - Workflow deliberation (preparation -> en_cours -> validee -> cloturee)
      - Rachats jury avec audit
      - Generation PV en PDF
      - Verrouillage notes post-cloture deliberation

PHASE 4 : Stages et PFE (1 semaine)
  [x] Existant : Prof (tuteur academique)
  [ ] A faire :
      - Creer apps/stages/
      - Convention stage CRUD
      - Evaluation stage (3 notes)
      - Integration MoteurLMD (PFE S6 >= 12/20 pour diplome)
      - Derogation medicale

PHASE 5 : Documents Officiels (2 semaines)
  [x] Existant : pdfkit (infrastructure PDF)
  [ ] A faire :
      - Creer apps/documents/
      - Auto-increment numero serie
      - Generation QR code (ajouter lib qrcode)
      - Templates PDF : attestation, releve notes, diplome
      - Endpoint public verification authenticite
      - Registre diplomes (append-only)

PHASE 6 : Notifications (1 semaine)
  [ ] A faire :
      - Creer apps/notifications/
      - Modele Notification
      - Helpers de creation integres aux workflows
      - Endpoint liste + mark-as-read
      - Polling (WebSocket optionnel futur)

TOTAL : ~12 semaines (3 mois) pour l'integration complete
```

### 7.8 Points d'Attention

#### Collision de Noms
- `authentication.Module` (RBAC) vs `scolarite.ModuleLMD` (academique) : Nommage explicite avec suffixe `LMD`
- `parametres.Semestre` (planification) vs `scolarite.SemestreLMD` (academique) : Idem

#### Compatibilite Ascendante
- Les apps existantes (emplois, suivi, avancement, vacation) ne sont PAS modifiees
- Les bridges FK sont unidirectionnels et nullable : le code existant n'a jamais besoin de les connaitre
- Les nouveaux champs sur Year et UserPermission sont nullable/defaut

#### Strategie Etudiant
- `EtudiantLMD` (scolarite) est un modele RICHE pour la scolarite
- `Etudiant` (absence) reste MINIMAL pour le tracking des presences
- Le bridge FK `etudiant_legacy` permet une unification future
- Les etudiants NE SONT PAS des CustomUser (pas d'acces au back-office SIGA)

#### Base de Donnees
- Toutes les nouvelles tables creees via migrations Django dans la meme DB MySQL
- Pas de changement de moteur de base de donnees
- Ajouter `qrcode` et eventuellement `weasyprint` aux requirements.txt

### 7.9 Dependances a Ajouter

```
# requirements.txt - AJOUTS
qrcode[pil]>=7.4        # Generation QR codes pour documents
# Optionnel :
# weasyprint>=60.0       # Alternative a pdfkit pour PDF (meilleur CSS)
# celery>=5.3            # Taches asynchrones (generation PDF, emails)
# django-redis>=5.4      # Si Redis pas encore en production
```

---

## Resume

| Metrique | Valeur |
|----------|--------|
| **Apps existantes** | 12 |
| **Modeles existants** | 29 |
| **Apps a creer** | 6 (scolarite, inscriptions, evaluations, stages, documents, notifications) |
| **Modeles a creer** | ~22 |
| **Modeles a etendre** | 3 (Year +4 champs, ROLE_CHOICES +2 roles, UserPermission +1 FK) |
| **Modeles a NE PAS modifier** | 3 (Semestre, EM, Etudiant) |
| **Code existant a modifier** | < 10 lignes (ajouts dans models.py authentication et parametres) |
| **Endpoints existants impactes** | 0 |
| **Nouveaux endpoints** | ~25 |
| **Duree estimee** | ~12 semaines |
