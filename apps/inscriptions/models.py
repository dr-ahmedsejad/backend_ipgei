import uuid
from django.db import models

from core.validators import validate_document
from apps.scolarite.models import TYPE_DIPLOME_CHOICES


STATUT_PREINSCRIPTION_CHOICES = [
    ('soumise',    'Soumise'),
    ('en_examen',  'En examen'),
    ('acceptee',   'Acceptée'),
    ('rejetee',    'Rejetée'),
    ('inscrite',   'Inscrite'),
]

STATUT_INSCRIPTION_CHOICES = [
    ('en_cours', 'En cours'),
    ('validee',  'Validée'),
    ('annulee',  'Annulée'),
]

GENRE_CHOICES = [('M', 'Masculin'), ('F', 'Féminin')]


class Preinscription(models.Model):
    """
    Dossier de pré-inscription publique (sans authentification).
    Workflow : soumise → en_examen → acceptée/rejetée → inscrite.
    """
    # Référence publique (token de suivi dossier sans exposer l'id)
    numero_dossier  = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    annee_univ      = models.ForeignKey(
        'parametres.Year', on_delete=models.PROTECT, related_name='preinscriptions',
        null=True, blank=True,
    )
    filiere         = models.ForeignKey(
        'scolarite.Filiere', on_delete=models.PROTECT, related_name='preinscriptions',
        null=True, blank=True,
    )
    # Section 1bis institution_V1
    institution     = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT,
        related_name='preinscriptions',
    )

    # Identité (bilingue)
    nom_fr          = models.CharField(max_length=100, default='')
    nom_ar          = models.CharField(max_length=100, blank=True, default='')
    prenom_fr       = models.CharField(max_length=100, default='')
    prenom_ar       = models.CharField(max_length=100, blank=True, default='')
    date_naissance  = models.DateField(null=True, blank=True)
    lieu_naissance  = models.CharField(max_length=100, blank=True, default='')
    genre           = models.CharField(max_length=1, choices=GENRE_CHOICES, blank=True, default='')
    nationalite     = models.CharField(max_length=50, default='Mauritanienne')
    cni             = models.CharField(max_length=20, blank=True, default='')
    telephone       = models.CharField(max_length=20, blank=True, default='')
    email           = models.EmailField(blank=True, default='')

    # Cursus antérieur
    serie_bac       = models.CharField(max_length=50, blank=True, default='')
    annee_bac       = models.IntegerField(null=True, blank=True)
    mention_bac     = models.CharField(max_length=50, blank=True, default='')
    bac_moyenne     = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)

    # Motivation
    motif           = models.TextField(blank=True, default='')

    # Documents joints
    piece_identite  = models.FileField(upload_to='preinscriptions/identite/',
                                       validators=[validate_document], blank=True, null=True)
    releve_notes    = models.FileField(upload_to='preinscriptions/releves/',
                                       validators=[validate_document], blank=True, null=True)
    photo           = models.ImageField(upload_to='preinscriptions/photos/',
                                       validators=[validate_document], blank=True, null=True)

    # Workflow
    statut          = models.CharField(
        max_length=20, choices=STATUT_PREINSCRIPTION_CHOICES, default='soumise',
    )
    motif_rejet     = models.TextField(blank=True, default='')
    date_soumission = models.DateTimeField(auto_now_add=True)
    examinee_par    = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='preinscriptions_examinees',
    )
    date_examen     = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'inscriptions_preinscription'
        ordering = ['-date_soumission']
        indexes  = [
            models.Index(fields=['statut', 'annee_univ']),
            models.Index(fields=['numero_dossier']),
            models.Index(fields=['cni']),
        ]

    def __str__(self):
        return f'Dossier {self.numero_dossier} — {self.nom_fr} {self.prenom_fr}'

    def save(self, *args, **kwargs):
        # Compresse la photo lors d'un nouvel upload (JPEG q88, 1280px max).
        from core.image_utils import optimize_field_on_upload
        optimize_field_on_upload(self, 'photo')
        super().save(*args, **kwargs)


class InscriptionAdministrative(models.Model):
    """
    Inscription administrative : lien étudiant ↔ filière ↔ année.
    Paiement des frais + génération du numéro d'inscription.
    """
    etudiant            = models.ForeignKey(
        'absence.Etudiant', on_delete=models.PROTECT,
        related_name='inscriptions_admin',
    )
    annee_univ          = models.ForeignKey(
        'parametres.Year', on_delete=models.PROTECT,
        related_name='inscriptions_admin',
    )
    filiere             = models.ForeignKey(
        'scolarite.Filiere', on_delete=models.PROTECT,
        related_name='inscriptions_admin',
    )
    # Section 1bis institution_V1
    institution         = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT,
        related_name='inscriptions_admin',
    )
    niveau              = models.IntegerField(help_text="Année d'étude : 1, 2, 3, ...")
    numero_inscription  = models.CharField(max_length=50, unique=True)
    statut              = models.CharField(
        max_length=20, choices=STATUT_INSCRIPTION_CHOICES, default='en_cours',
    )
    montant_frais       = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    est_payee           = models.BooleanField(default=False)
    date_paiement       = models.DateField(null=True, blank=True)
    recu_paiement       = models.CharField(max_length=100, blank=True, default='')
    date_inscription    = models.DateTimeField(auto_now_add=True)
    validee_par         = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='inscriptions_admin_validees',
    )

    class Meta:
        db_table        = 'inscriptions_administrative'
        unique_together = ('etudiant', 'annee_univ')
        indexes         = [
            models.Index(fields=['annee_univ', 'filiere', 'statut']),
            models.Index(fields=['numero_inscription']),
        ]

    def __str__(self):
        return f'{self.etudiant} — {self.filiere} {self.annee_univ}'


class InscriptionPedagogique(models.Model):
    """
    Inscription à un semestre spécifique (lien inscription admin ↔ semestre étendu).
    Gère le cas redoublant.
    """
    inscription_admin = models.ForeignKey(
        InscriptionAdministrative, on_delete=models.PROTECT,
        related_name='inscriptions_ped',
    )
    semestre          = models.ForeignKey(
        'parametres.Semestre', on_delete=models.PROTECT,
        related_name='inscriptions_ped',
        help_text="Semestre étendu : filiere + annee_univ remplis.",
    )
    est_redoublant    = models.BooleanField(default=False)
    est_dette         = models.BooleanField(
        default=False,
        help_text="True si cette inscription pédagogique correspond à une mise en dette globale du semestre.",
    )
    date_inscription  = models.DateTimeField(auto_now_add=True)
    validee_par       = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='inscriptions_ped_validees',
    )

    class Meta:
        db_table        = 'inscriptions_pedagogique'
        unique_together = ('inscription_admin', 'semestre')

    def __str__(self):
        return f'{self.inscription_admin} / {self.semestre}'


class GrilleFrais(models.Model):
    """
    Grille tarifaire des frais d'inscription administrative.

    Le montant est fixé par (institution, année universitaire, type de diplôme,
    niveau) — il n'est plus saisi à la main lors du paiement. Sous-ensemble léger
    et forward-compatible du futur module de facturation (cf. facturation.md).
    """
    institution   = models.ForeignKey(
        'parametres.Institution', on_delete=models.CASCADE,
        related_name='grilles_frais',
    )
    annee_univ    = models.ForeignKey(
        'parametres.Year', on_delete=models.PROTECT,
        related_name='grilles_frais',
    )
    # Miroir de scolarite.Filiere.type_diplome (LP, M, ING, Doctorat).
    type_diplome  = models.CharField(
        max_length=20, choices=TYPE_DIPLOME_CHOICES, default='LP',
    )
    niveau        = models.IntegerField(help_text="Année d'étude : 1, 2, 3, ...")
    montant       = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    actif         = models.BooleanField(default=True)
    date_creation = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        db_table        = 'inscriptions_grille_frais'
        unique_together = ('institution', 'annee_univ', 'type_diplome', 'niveau')
        ordering        = ['annee_univ', 'type_diplome', 'niveau']
        indexes         = [
            models.Index(fields=['institution', 'annee_univ', 'type_diplome', 'niveau']),
        ]

    def __str__(self):
        return f'{self.get_type_diplome_display()} N{self.niveau} {self.annee_univ} — {self.montant}'

    @classmethod
    def montant_pour(cls, inscription):
        """
        Montant dû (Decimal) pour une InscriptionAdministrative, d'après la grille
        active correspondant à (institution, année, type_diplome, niveau).
        Retourne None si aucun tarif n'est défini.
        """
        grille = cls.objects.filter(
            institution_id=inscription.institution_id,
            annee_univ_id=inscription.annee_univ_id,
            type_diplome=inscription.filiere.type_diplome,
            niveau=inscription.niveau,
            actif=True,
        ).values_list('montant', flat=True).first()
        return grille


class CandidatBac(models.Model):
    """
    Vivier des bacheliers admis (référentiel BAC importé).

    Un CandidatBac N'EST PAS un Etudiant : c'est un candidat issu du fichier
    officiel du BAC. Il devient Etudiant uniquement au moment de l'inscription
    administrative (l'inscription pose `inscrit=True` et lie `etudiant`).
    """
    annee_univ     = models.ForeignKey(
        'parametres.Year', on_delete=models.CASCADE, related_name='candidats_bac',
    )
    institution    = models.ForeignKey(
        'parametres.Institution', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='candidats_bac',
    )
    nni            = models.CharField(max_length=20, blank=True, default='', db_index=True)
    num_bac        = models.CharField(max_length=20, db_index=True)
    nom_fr         = models.CharField(max_length=200, blank=True, default='')
    nom_ar         = models.CharField(max_length=200, blank=True, default='')
    date_naissance = models.DateField(null=True, blank=True)
    lieu_naissance = models.CharField(max_length=200, blank=True, default='')
    sexe           = models.CharField(max_length=1, choices=GENRE_CHOICES, blank=True, default='')
    serie          = models.CharField(max_length=50, blank=True, default='')
    moyenne        = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    mention        = models.CharField(max_length=50, blank=True, default='')
    wilaya         = models.CharField(max_length=100, blank=True, default='')
    # Suivi d'inscription (vivier -> Etudiant)
    inscrit        = models.BooleanField(default=False)
    etudiant       = models.ForeignKey(
        'absence.Etudiant', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='candidat_bac',
    )
    date_import    = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table        = 'inscriptions_candidat_bac'
        unique_together = ('annee_univ', 'num_bac')
        ordering        = ['nom_fr', 'num_bac']
        indexes         = [
            models.Index(fields=['annee_univ', 'inscrit']),
            models.Index(fields=['nni']),
            models.Index(fields=['num_bac']),
        ]

    def __str__(self):
        return f'{self.nom_fr} — BAC {self.num_bac} ({self.annee_univ})'


class InscriptionElement(models.Model):
    """
    Inscription à un élément de module (gestion des dettes UE).
    Un étudiant redoublant réinscrit uniquement les éléments non validés.
    """
    inscription_ped = models.ForeignKey(
        InscriptionPedagogique, on_delete=models.PROTECT,
        related_name='inscriptions_elements',
    )
    element         = models.ForeignKey(
        'modules.ElementModule', on_delete=models.PROTECT,
        related_name='inscriptions_elements',
        help_text='Élément de module LMD.',
        null=True, blank=True,
    )
    em              = models.ForeignKey(
        'em.EM', on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='inscriptions_elements',
        help_text='Élément de module (planification) lié à cette inscription. '
                  'PROTECT : un EM avec des inscriptions ne peut pas être supprimé '
                  '(sinon la reconstruction des dettes, clé sur em, est cassée).',
    )
    est_dette       = models.BooleanField(
        default=False,
        help_text="True si l'étudiant repassé cet élément d'une année antérieure.",
    )
    annee_dette     = models.ForeignKey(
        'parametres.Year', on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='inscriptions_dettes',
        help_text="Année académique d'origine de la dette.",
    )

    class Meta:
        db_table        = 'inscriptions_element'
        unique_together = ('inscription_ped', 'element')

    def clean(self):
        # XOR : une inscription-élément est soit LMD (element) soit planification
        # (em), jamais ni l'un ni l'autre, jamais les deux (source unique de vérité
        # pour le calcul de note). Cohérent avec ajouter_element côté vue.
        from django.core.exceptions import ValidationError
        if not self.element_id and not self.em_id:
            raise ValidationError('Un élément LMD (element) ou une planification (em) est requis.')
        if self.element_id and self.em_id:
            raise ValidationError('Renseigner soit element (LMD) soit em (planification), pas les deux.')

    def __str__(self):
        return f'{self.inscription_ped} → {self.element}'


class Derogation(models.Model):
    """
    Dérogation administrative préalable à la délibération.

    Cas couvert principal : année blanche médicale (Art. 23 Arrêté 562 / Art. 29
    Décret 2018-070) — l'étudiant ne perd pas son droit de redoublement.

    L'enregistrement se fait AVANT la délibération. Lors du calcul des décisions
    (calcul_notes / deliberation_annuelle), si une dérogation 'annee_blanche'
    existe pour (étudiant, année), la décision est forcée à 'annee_blanche'.
    """
    TYPE_CHOICES = [
        ('annee_blanche',           'Année blanche'),
        ('derogation_inscription',  "Dérogation d'inscription"),
        ('autre',                   'Autre dérogation'),
    ]
    STATUT_CHOICES = [
        ('actif',  'Actif'),
        ('annule', 'Annulé'),
    ]

    etudiant       = models.ForeignKey(
        'absence.Etudiant', on_delete=models.PROTECT,
        related_name='derogations_admin',
    )
    annee_univ     = models.ForeignKey(
        'parametres.Year', on_delete=models.PROTECT,
        related_name='derogations_admin',
    )
    # Section 1bis institution_V1
    institution    = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT,
        related_name='derogations',
    )
    type_derogation = models.CharField(
        max_length=30, choices=TYPE_CHOICES, db_index=True,
    )
    motif          = models.TextField(
        help_text="Justification administrative de la dérogation.",
    )
    justificatif   = models.FileField(
        upload_to='derogations/%Y/', blank=True, null=True,
        help_text="Document justificatif (PDF, image) — obligatoire pour année blanche.",
    )
    date_decision  = models.DateField(
        help_text="Date de la décision administrative.",
    )
    decide_par     = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.PROTECT,
        related_name='derogations_decidees',
        help_text="Responsable ayant validé la dérogation (chef d'établissement).",
    )
    statut         = models.CharField(
        max_length=10, choices=STATUT_CHOICES, default='actif', db_index=True,
    )

    date_creation     = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        db_table        = 'inscriptions_derogation'
        unique_together = ('etudiant', 'annee_univ', 'type_derogation')
        indexes = [
            models.Index(fields=['type_derogation', 'statut']),
            models.Index(fields=['annee_univ', 'statut']),
        ]
        ordering = ['-date_decision']

    def __str__(self):
        return (
            f'{self.etudiant.matricule} — {self.get_type_derogation_display()} '
            f'({self.annee_univ.annee})'
        )


class Progression(models.Model):
    """
    Buffer entre la délibération annuelle et la réinscription effective N+1.

    Workflow :
      PV clos → ProgressionService.generer_progressions()
             → Progression (en_attente, filiere_cible = filiere_source par défaut)
             → [admin peut modifier filiere_cible / niveau_cible avant la rentrée]
             → ReinscriptionService.executer()
             → InscriptionAdministrative N+1 créée

    Couvre les 5 décisions : progression, redoublement, annee_blanche, exclusion, diplomation.
    Le matricule est snapshottisé à la création pour traçabilité historique.
    """
    DECISION_CHOICES = [
        ('progression',   'Passage au niveau supérieur'),
        ('redoublement',  'Redoublement'),  # consomme le droit Art. 22/28 — cf. champ consomme_droit_redoublement
        ('annee_blanche', 'Année blanche médicale (Art. 23/29 — ne consomme pas le droit)'),
        ('exclusion',     'Exclusion définitive du cycle'),
        ('diplomation',   'Diplômé — fin de cycle (180 crédits)'),
    ]
    STATUT_CHOICES = [
        ('en_attente', 'En attente de réinscription'),
        ('modifiee',   "Modifiée par l'administration"),
        ('executee',   'Inscription N+1 créée'),
        ('annulee',    'Annulée'),
    ]

    ligne_deliberation = models.OneToOneField(
        'evaluations.LigneDeliberation', on_delete=models.PROTECT,
        related_name='progression', null=True, blank=True,
        help_text="Ligne PV source (null si création administrative hors PV).",
    )
    etudiant  = models.ForeignKey(
        'absence.Etudiant', on_delete=models.PROTECT,
        related_name='progressions',
    )
    matricule = models.CharField(
        max_length=50, db_index=True,
        help_text="Snapshot de etudiant.numero_dossier au moment de la délibération.",
    )

    annee_source = models.ForeignKey(
        'parametres.Year', on_delete=models.PROTECT,
        related_name='progressions_source',
    )
    annee_cible  = models.ForeignKey(
        'parametres.Year', on_delete=models.PROTECT,
        related_name='progressions_cible',
    )
    # Section 1bis institution_V1 — un étudiant ne change pas d'institution
    institution  = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT,
        related_name='progressions',
    )

    filiere_source = models.ForeignKey(
        'scolarite.Filiere', on_delete=models.PROTECT,
        related_name='+',
        help_text="Filière au moment du PV (non modifiable).",
    )
    niveau_source  = models.IntegerField(help_text="Niveau au moment du PV (non modifiable).")
    filiere_cible  = models.ForeignKey(
        'scolarite.Filiere', on_delete=models.PROTECT,
        related_name='progressions_cible',
        null=True, blank=True,
        help_text="Filière d'inscription N+1 — modifiable jusqu'à exécution.",
    )
    niveau_cible   = models.IntegerField(
        null=True, blank=True,
        help_text="Niveau d'inscription N+1 — null si exclusion.",
    )

    decision = models.CharField(max_length=20, choices=DECISION_CHOICES, db_index=True)
    consomme_droit_redoublement = models.BooleanField(
        default=False,
        help_text="True UNIQUEMENT si decision=redoublement. "
                  "Les années blanches ne consomment pas ce droit (Art. 23/29).",
    )

    statut = models.CharField(
        max_length=20, choices=STATUT_CHOICES, default='en_attente', db_index=True,
    )
    inscription_admin_creee = models.OneToOneField(
        'inscriptions.InscriptionAdministrative', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='progression_source',
        help_text="InscriptionAdministrative N+1 créée par ReinscriptionService.",
    )

    motif_modification = models.TextField(
        blank=True, default='',
        help_text="Justification du changement de filière/niveau.",
    )
    modifiee_par = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='progressions_modifiees',
    )
    date_creation     = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        db_table        = 'inscriptions_progression'
        unique_together = ('etudiant', 'annee_cible')
        indexes = [
            models.Index(fields=['statut', 'annee_cible']),
            models.Index(fields=['decision', 'annee_cible']),
        ]

    def __str__(self):
        return (
            f'{self.matricule} '
            f'{self.annee_source}→{self.annee_cible} : '
            f'{self.get_decision_display()}'
        )
