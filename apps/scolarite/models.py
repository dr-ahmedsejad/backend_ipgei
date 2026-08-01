from decimal import Decimal

from django.db import models
from django.conf import settings


TYPE_DIPLOME_CHOICES = [
    ('LP',       'Licence'),
    ('M',        'Master'),
    ('ING',      'Ingénieur'),
    ('Doctorat', 'Doctorat'),
]


class DepartementAcademique(models.Model):
    """
    Département académique au sens organisationnel (Informatique, Gestion, Mathématiques…).
    Distinct du modèle 'Departement' de apps/departement qui représente une CLASSE
    pédagogique (filière + niveau + groupe). Ce modèle se situe AU-DESSUS de Filiere.
    """
    code        = models.CharField(max_length=20, unique=True)       # ex: "INFO", "GEST"
    intitule_fr = models.CharField(max_length=200)
    intitule_ar = models.CharField(max_length=200, blank=True, default='')
    institution = models.ForeignKey(
        'parametres.Institution',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='departements_academiques',
    )
    responsable = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='departements_diriges',
    )
    actif       = models.BooleanField(default=True)

    class Meta:
        db_table        = 'scolarite_departement_academique'
        ordering        = ['code']
        verbose_name    = 'Département académique'
        verbose_name_plural = 'Départements académiques'

    def __str__(self):
        return f'{self.code} — {self.intitule_fr}'


class Filiere(models.Model):
    """
    Filière académique : SEA, SDID, Statistique, etc.
    Représente le PROGRAMME DE FORMATION, stable d'une année à l'autre.
    Distinct du Departement qui est la classe de planification annuelle (filiere + niveau + groupe).
    """
    code          = models.CharField(max_length=20, unique=True)
    intitule_fr   = models.CharField(max_length=200)
    intitule_ar   = models.CharField(max_length=200, blank=True, default='')
    type_diplome  = models.CharField(
        max_length=10,
        choices=TYPE_DIPLOME_CHOICES,
        default='LP',
    )
    nb_semestres  = models.IntegerField(default=6)
    credits_total = models.IntegerField(
        default=180,
        help_text="Crédits couverts par cette filière (auto-calculé : "
                  "(niveau_fin - niveau_debut + 1) × 60 si laissé à 0).",
    )
    niveau_debut  = models.IntegerField(
        default=1,
        help_text="Premier niveau couvert par la filière (1 = L1, 2 = L2…). "
                  "Tronc commun L1 = 1, spécialisation L2-L3 = 2.",
    )
    niveau_fin    = models.IntegerField(
        default=3,
        help_text="Dernier niveau couvert par la filière. "
                  "Tronc commun L1 = 1, licence complète = 3, master = 2.",
    )
    est_active    = models.BooleanField(default=True)
    filiere_parent = models.ForeignKey(
        'self', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='filieres_filles',
        help_text="Filière parente — restreint les changements administratifs "
                  "aux filles de la même parente.",
    )
    departement_academique = models.ForeignKey(
        DepartementAcademique,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='filieres',
    )
    responsable   = models.ForeignKey(
        'prof.Prof',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='filieres_responsable',
    )
    institution   = models.ForeignKey(
        'parametres.Institution',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='filieres',
    )
    date_creation    = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'scolarite_filiere'
        ordering = ['code']
        indexes = [
            models.Index(fields=['code', 'institution']),
            models.Index(fields=['est_active']),
        ]

    def __str__(self):
        return f'{self.code} — {self.intitule_fr}'

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.niveau_debut < 1:
            raise ValidationError({'niveau_debut': 'Le niveau de début doit être ≥ 1.'})
        if self.niveau_fin < self.niveau_debut:
            raise ValidationError({'niveau_fin': 'Le niveau de fin doit être ≥ niveau_debut.'})

    def save(self, *args, **kwargs):
        # Auto-calcule credits_total si non fourni (ou laissé à 0)
        # Règle LMD : 60 crédits par niveau (1 niveau = 2 semestres = 60 crédits)
        if not self.credits_total:
            self.credits_total = (self.niveau_fin - self.niveau_debut + 1) * 60
        super().save(*args, **kwargs)

    @property
    def credits_couvert(self) -> int:
        """Crédits théoriquement couverts par cette filière (60 × nombre de niveaux)."""
        return (self.niveau_fin - self.niveau_debut + 1) * 60

    @property
    def label_niveaux(self) -> str:
        """Label lisible pour le sélecteur (ex : 'L1 → L3' ou 'L2')."""
        if self.niveau_debut == self.niveau_fin:
            return f'L{self.niveau_debut}'
        return f'L{self.niveau_debut} → L{self.niveau_fin}'


class ParametresPonderation(models.Model):
    """
    Règle institutionnelle de pondération des notes. Singleton — un seul enregistrement en base.
    Avec TP : (CC × coeff_cc + EXAM × coeff_exam + TP × coeff_tp) / (coeff_cc + coeff_exam + coeff_tp)
    Sans TP : (CC × coeff_cc + EXAM × coeff_exam) / (coeff_cc + coeff_exam)
    La présence de TP est déterminée par EM.TP > 0 (heures TP planifiées).
    """
    coeff_cc   = models.IntegerField(default=2)
    coeff_exam = models.IntegerField(default=3)
    coeff_tp   = models.IntegerField(default=1)

    # Plafond de la note d'un EM validé en session de rattrapage
    # (décision du conseil scientifique). DÉFAUT institutionnel : il est copié
    # (figé) sur chaque SessionEvaluation à sa création — changer ces valeurs
    # n'impacte donc QUE les sessions créées ensuite, jamais les anciennes.
    rattrapage_plafond_actif = models.BooleanField(
        default=True,
        help_text="Si activé, un EM validé grâce au rattrapage est plafonné à la "
                  "valeur ci-dessous (défaut appliqué aux nouvelles sessions).",
    )
    rattrapage_plafond = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal('10.00'),
        help_text="Note maximale (sur 20) d'un EM validé en rattrapage. Défaut 10.",
    )

    class Meta:
        db_table        = 'scolarite_parametres_ponderation'
        verbose_name    = 'Paramètres de pondération'
        verbose_name_plural = 'Paramètres de pondération'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f'Pondération : CC×{self.coeff_cc} / EXAM×{self.coeff_exam} / TP×{self.coeff_tp}'
