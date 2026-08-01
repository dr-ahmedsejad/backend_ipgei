from django.db import models

from core.validators import validate_document

GENRE_CHOICES = [('M', 'Masculin'), ('F', 'Féminin')]
STATUT_CHOICES = [
    (0, 'Présent'),
    (1, 'Absent'),
    (2, 'Sanctionné'),
    (3, 'Justifiée'),
]
STATUT_ETUDIANT_CHOICES = [
    ('actif',     'Actif'),
    ('suspendu',  'Suspendu'),
    ('diplome',   'Diplômé'),
    ('exclu',     'Exclu'),
    ('transfere', 'Transféré'),
]


class Etudiant(models.Model):
    # --- Lien compte utilisateur portail ---
    user = models.OneToOneField(
        'authentication.CustomUser',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='etudiant_profile',
    )

    # --- Existant (inchangé) ---
    matricule   = models.CharField(max_length=50, unique=True)
    nom         = models.CharField(max_length=200)
    # IPGEI : le rattachement pedagogique d'un etudiant passe par
    # `ipgei.InscriptionIPGEI` -> `ipgei.Classe`, pas par le `Departement` du
    # socle LMD. Le champ devient donc optionnel, plutot que d'imposer un
    # departement fictif a chaque preparationnaire.
    departement = models.ForeignKey(
        'departement.Departement', on_delete=models.PROTECT, related_name='etudiants',
        null=True, blank=True,
    )
    genre       = models.CharField(max_length=1, choices=GENRE_CHOICES, default='M')

    # --- Identité bilingue FR/AR (additif, nullable) ---
    # Stratégie : backfill nom_fr depuis nom via data migration, puis @property
    nom_fr          = models.CharField(max_length=200, blank=True, default='')
    nom_ar          = models.CharField(max_length=200, blank=True, default='')
    prenom_fr       = models.CharField(max_length=200, blank=True, default='')
    prenom_ar       = models.CharField(max_length=200, blank=True, default='')
    lieu_naissance_fr = models.CharField(max_length=200, blank=True, default='')
    lieu_naissance_ar = models.CharField(max_length=200, blank=True, default='')
    nationalite_fr  = models.CharField(max_length=50, blank=True, default='Mauritanienne')
    nationalite_ar  = models.CharField(max_length=50, blank=True, default='موريتانية')
    adresse_fr      = models.TextField(blank=True, default='')
    adresse_ar      = models.TextField(blank=True, default='')

    # --- Identité mono-langue ---
    date_naissance = models.DateField(null=True, blank=True)
    cni            = models.CharField(max_length=20, null=True, blank=True)
    telephone      = models.CharField(max_length=20, blank=True, default='')
    email          = models.EmailField(blank=True, default='')
    photo          = models.ImageField(upload_to='etudiants/photos/', null=True, blank=True)

    # --- BAC ---
    nbac           = models.CharField(max_length=20, null=True, blank=True)
    serie_bac      = models.CharField(max_length=50, blank=True, default='')
    moyenne_bac    = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    # --- Scolarité ---
    filiere        = models.ForeignKey(
        'scolarite.Filiere',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='etudiants',
    )
    statut         = models.CharField(
        max_length=20, choices=STATUT_ETUDIANT_CHOICES, default='actif',
    )
    date_creation  = models.DateTimeField(auto_now_add=True, null=True)

    class Meta:
        db_table = 'absence_etudiant'
        ordering = ['nom']
        indexes  = [
            models.Index(fields=['statut']),
            models.Index(fields=['filiere']),
        ]

    def __str__(self):
        return f'{self.matricule} - {self.nom}'

    @property
    def nom_display(self):
        """Retourne nom_fr si rempli, sinon nom (rétrocompatibilité)."""
        return self.nom_fr or self.nom

    def save(self, *args, **kwargs):
        # Compresse la photo lors d'un nouvel upload (JPEG q88, 1280px max).
        from core.image_utils import optimize_field_on_upload
        optimize_field_on_upload(self, 'photo')
        super().save(*args, **kwargs)


class Presence(models.Model):
    suivi             = models.ForeignKey('suivi.Suivie', on_delete=models.CASCADE, related_name='presences')
    etudiant          = models.ForeignKey(Etudiant, on_delete=models.CASCADE, related_name='presences')
    statut            = models.IntegerField(choices=STATUT_CHOICES, default=0)
    commentaire       = models.TextField(blank=True, default='')
    justificatif      = models.FileField(upload_to='justificatifs/', null=True, blank=True,
                                          validators=[validate_document])
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        db_table       = 'absence_presence'
        unique_together = ('suivi', 'etudiant')

    def __str__(self):
        return f'{self.etudiant} / {self.suivi} → {self.get_statut_display()}'


class SeuilAbsence(models.Model):
    seuil = models.IntegerField(default=3)

    class Meta:
        db_table = 'absence_seuilabsence'

