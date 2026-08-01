from django.db import models
from django.utils import timezone


class Year(models.Model):
    annee      = models.CharField(max_length=20, unique=True)
    # --- Nouveaux champs (nullable → zero impact sur les lignes existantes) ---
    date_debut  = models.DateField(null=True, blank=True)
    date_fin    = models.DateField(null=True, blank=True)
    est_active  = models.BooleanField(default=False)
    est_cloturee = models.BooleanField(default=False)

    class Meta:
        db_table = 'annee'
        ordering = ['-annee']

    def __str__(self):
        return self.annee


class Niveau(models.Model):
    niveau = models.CharField(max_length=50, unique=True)

    class Meta:
        db_table = 'niveau'
        ordering = ['niveau']

    def __str__(self):
        return self.niveau


class Semestre(models.Model):
    """
    Semestre générique S1..S6. Stable d'une année à l'autre, indépendant
    de la filière et de l'année universitaire (cf. plan institution_V1, Section 1).
    Les liens (année, filière) sont portés par
    InscriptionPedagogique → InscriptionAdministrative.
    """
    TYPE_CHOICES = [('P', 'Pair'), ('I', 'Impair')]
    code_semestre   = models.CharField(max_length=20)
    semestre        = models.CharField(max_length=100)
    niveau_semestre = models.ForeignKey(Niveau, on_delete=models.PROTECT, related_name='semestres')
    type_semestre   = models.CharField(max_length=1, choices=TYPE_CHOICES, default='I')
    credits         = models.IntegerField(default=30)

    class Meta:
        db_table = 'semestre'
        ordering = ['code_semestre']
        constraints = [
            models.UniqueConstraint(
                fields=['code_semestre', 'niveau_semestre', 'type_semestre'],
                name='uniq_semestre_code_niveau_type',
            ),
        ]

    def __str__(self):
        return self.semestre


class Seance(models.Model):
    type_seance = models.CharField(max_length=50, unique=True)
    # Cellule emploi affichee uniquement avec le type centre (pas de prof/em/salle).
    # Pour Sport, Instruction militaire, Conferences, etc. — types qui occupent un
    # creneau sans necessiter de prof/EM/salle specifique.
    is_special  = models.BooleanField(default=False)

    class Meta:
        db_table = 'Seance'
        ordering = ['type_seance']

    def __str__(self):
        return self.type_seance


class Creneau(models.Model):
    TYPE_CHOICES = [('matin', 'Matin'), ('apres-midi', 'Après-midi'), ('soir', 'Soir')]
    creneau      = models.CharField(max_length=100, unique=True)
    duree        = models.FloatField(default=1.5)
    type_creneau = models.CharField(max_length=20, choices=TYPE_CHOICES, default='matin')
    ordre        = models.IntegerField(default=0)
    is_actif     = models.BooleanField(default=True)

    class Meta:
        db_table = 'creneau'
        ordering = ['ordre', 'creneau']

    def __str__(self):
        return self.creneau


class Jour(models.Model):
    jour = models.CharField(max_length=20, unique=True)

    class Meta:
        db_table = 'jour'
        ordering = ['id']

    def __str__(self):
        return self.jour


class Semaine(models.Model):
    # Types de semaine. Seules les semaines de type 'cours' sont numerotees
    # dans la sequence pedagogique. Les autres (ferie/vacances/examen) ont
    # numero_semaine = NULL et sont hors-sequence (preservees pour audit).
    TYPE_COURS    = 'cours'
    TYPE_FERIE    = 'ferie'
    TYPE_VACANCES = 'vacances'
    TYPE_EXAMEN   = 'examen'
    TYPES_SEMAINE = [
        (TYPE_COURS,    'Cours'),
        (TYPE_FERIE,    'Férié'),
        (TYPE_VACANCES, 'Vacances'),
        (TYPE_EXAMEN,   'Examens'),
    ]

    numero_semaine      = models.IntegerField(null=True, blank=True)
    jour_fk             = models.ForeignKey('Jour', on_delete=models.RESTRICT, related_name='semaines')
    date                = models.DateField()
    annee_universitaire = models.CharField(max_length=9)
    type_semestre       = models.CharField(max_length=1, default='I')
    type_semaine        = models.CharField(
        max_length=10, choices=TYPES_SEMAINE, default=TYPE_COURS,
    )
    description         = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        db_table = 'semaine'
        ordering = ['annee_universitaire', 'type_semestre', 'date']

    @property
    def jour(self) -> str:
        """Compat lecture: retourne le libelle du jour via FK."""
        return self.jour_fk.jour if self.jour_fk_id else ''

    def __str__(self):
        if self.numero_semaine is None:
            return f'[{self.get_type_semaine_display()}] {self.date}'
        return f'S{self.numero_semaine} - {self.date}'


class Paiement(models.Model):
    type        = models.CharField(max_length=50)
    taux        = models.FloatField()
    date_debut  = models.DateField()

    class Meta:
        db_table       = 'paiement'
        unique_together = ('type', 'date_debut')
        ordering       = ['type', '-date_debut']

    def __str__(self):
        return f'{self.type} - {self.taux} MRU (depuis {self.date_debut})'

    @classmethod
    def get_taux_at(cls, type_seance: str, date=None) -> float:
        """Retourne le taux applicable à une date donnée."""
        if date is None:
            date = timezone.now().date()
        p = cls.objects.filter(type=type_seance, date_debut__lte=date).order_by('-date_debut').first()
        return p.taux if p else 0.0


class Ramadan(models.Model):
    debut = models.DateField()
    fin   = models.DateField()

    class Meta:
        db_table = 'parametres_ramadan'

    def __str__(self):
        return f'Ramadan {self.debut} → {self.fin}'


class Institution(models.Model):
    TYPE_CHOICES = [
        ('universite', 'Université'),
        ('ecole',      'École Supérieure'),
        ('institut',   'Institut'),
    ]

    # --- Existant (CONSERVÉ — ne pas renommer directement, voir @property ci-dessous) ---
    acronyme = models.CharField(max_length=20, unique=True)
    nom      = models.CharField(max_length=200)  # deprecié progressivement via @property -> nom_fr

    # --- Tutelle / Groupe de rattachement ---
    groupe_fr       = models.CharField(max_length=200, blank=True, default='',
                          help_text="Organisation de tutelle (ex: Groupe Polytechnique)")
    groupe_ar       = models.CharField(max_length=200, blank=True, default='',
                          help_text="Organisation de tutelle en arabe (ex: مجمع بوليتكنيك)")

    # --- Identité bilingue FR/AR (additif, nullable) ---
    nom_fr          = models.CharField(max_length=200, blank=True, default='')
    nom_ar          = models.CharField(max_length=200, blank=True, default='')
    nom_complet_fr  = models.CharField(max_length=500, blank=True, default='')
    nom_complet_ar  = models.CharField(max_length=500, blank=True, default='')
    devise_fr       = models.CharField(max_length=200, blank=True, default='')
    devise_ar       = models.CharField(max_length=200, blank=True, default='')

    # --- Identité visuelle ---
    logo            = models.ImageField(upload_to='institutions/logos/', null=True, blank=True)
    logo_republique = models.ImageField(upload_to='institutions/sceaux/', null=True, blank=True)
    logo_groupe     = models.ImageField(upload_to='institutions/logos/', null=True, blank=True,
                          help_text="Logo du Groupe de tutelle (ex: Groupe Polytechnique) — "
                                    "affiché en haut à gauche des attestations de diplôme.")
    favicon         = models.ImageField(upload_to='institutions/favicons/', null=True, blank=True)

    # --- Coordonnées ---
    adresse_fr  = models.TextField(blank=True, default='')
    adresse_ar  = models.TextField(blank=True, default='')
    ville_fr    = models.CharField(max_length=100, blank=True, default='')
    ville_ar    = models.CharField(max_length=100, blank=True, default='')
    pays_fr     = models.CharField(max_length=100, blank=True, default='Mauritanie')
    pays_ar     = models.CharField(max_length=100, blank=True, default='موريتانيا')
    telephone   = models.CharField(max_length=20, blank=True, default='')
    fax         = models.CharField(max_length=20, blank=True, default='')
    email       = models.EmailField(blank=True, default='')
    site_web    = models.URLField(blank=True, default='')

    # --- Informations officielles ---
    code_etablissement  = models.CharField(max_length=20, blank=True, default='')
    type_etablissement  = models.CharField(
        max_length=30, choices=TYPE_CHOICES, default='ecole',
    )
    ministere_fr = models.CharField(max_length=200, blank=True, default='')
    ministere_ar = models.CharField(max_length=200, blank=True, default='')

    # --- Signataire des documents officiels ---
    directeur_nom_fr    = models.CharField(max_length=200, blank=True, default='')
    directeur_nom_ar    = models.CharField(max_length=200, blank=True, default='')
    directeur_titre_fr  = models.CharField(max_length=100, blank=True, default='')
    directeur_titre_ar  = models.CharField(max_length=100, blank=True, default='')
    directeur_signature = models.ImageField(upload_to='institutions/signatures/', null=True, blank=True)

    # --- Second signataire : Commandant du Groupe Polytechnique (attestation de diplôme) ---
    # Modèle officiel : l'attestation de diplôme porte DEUX signatures — le Directeur
    # de l'institut (champs directeur_* ci-dessus) ET le Commandant du Groupe de
    # tutelle. Champs configurables (pas de hardcode dans le template).
    commandant_nom_fr    = models.CharField(max_length=200, blank=True, default='')
    commandant_nom_ar    = models.CharField(max_length=200, blank=True, default='')
    commandant_titre_fr  = models.CharField(max_length=150, blank=True, default='Le Commandant du Groupe Polytechnique')
    commandant_titre_ar  = models.CharField(max_length=150, blank=True, default='قائد مجمع بوليتكنيك')
    commandant_signature = models.ImageField(upload_to='institutions/signatures/', null=True, blank=True)

    # Numéro de départ (offset) des diplômes : évite de commencer à 0001, qui
    # révélerait le rang/volume délivré. Le numéro reste un identifiant
    # ADMINISTRATIF — l'anti-falsification repose sur le QR (token aléatoire) +
    # la signature PDF, pas sur ce numéro. Cf. generer_numero_diplome().
    diplome_sequence_debut = models.PositiveIntegerField(default=500)

    # --- Configuration active ---
    est_principale = models.BooleanField(default=True)

    class Meta:
        db_table = 'institution'

    def save(self, *args, **kwargs):
        # Optimise les images uploadees (logos / sceaux / signatures) : PNG
        # optimise, transparence conservee, 512px max. No-op si pas de nouvel upload.
        from core.image_utils import optimize_field_on_upload
        for _f in ('logo', 'logo_republique', 'logo_groupe', 'favicon',
                   'directeur_signature', 'commandant_signature'):
            optimize_field_on_upload(self, _f, keep_transparency=True)
        # Garantit l'unicite de l'institution principale.
        super().save(*args, **kwargs)
        if self.est_principale:
            Institution.objects.exclude(pk=self.pk).filter(est_principale=True).update(est_principale=False)

    def __str__(self):
        return self.acronyme

    @property
    def nom_display(self):
        """Retourne nom_fr si rempli, sinon nom (rétrocompatibilité pendant la transition)."""
        return self.nom_fr or self.nom
