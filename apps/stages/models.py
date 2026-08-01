from django.db import models

from core.validators import validate_document


STATUT_CONVENTION_CHOICES = [
    ('brouillon',  'Brouillon'),
    ('soumise',    'Soumise'),
    ('en_cours',   'En cours'),
    ('terminee',   'Terminée'),
    ('annulee',    'Annulée'),
]

STATUT_DEROGATION_CHOICES = [
    ('soumise',   'Soumise'),
    ('approuvee', 'Approuvée'),
    ('refusee',   'Refusée'),
]


class ConventionStage(models.Model):
    etudiant            = models.ForeignKey(
        'absence.Etudiant', on_delete=models.PROTECT, related_name='conventions_stage',
    )
    entreprise_nom      = models.CharField(max_length=200)
    entreprise_adresse  = models.TextField(blank=True, default='')
    tuteur_entreprise   = models.CharField(max_length=200, blank=True, default='')
    tuteur_academique   = models.ForeignKey(
        'prof.Prof', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='conventions_tuteur',
    )
    date_debut          = models.DateField()
    date_fin            = models.DateField()
    sujet               = models.CharField(max_length=500)
    convention_fichier  = models.FileField(upload_to='stages/conventions/', null=True, blank=True,
                                            validators=[validate_document])
    statut              = models.CharField(
        max_length=20, choices=STATUT_CONVENTION_CHOICES, default='brouillon',
    )
    est_pfe             = models.BooleanField(default=False)
    created_at          = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'stages_convention'
        ordering = ['-created_at']

    def __str__(self):
        return f'Stage {self.etudiant} chez {self.entreprise_nom}'


class EvaluationStage(models.Model):
    convention          = models.OneToOneField(
        ConventionStage, on_delete=models.CASCADE, related_name='evaluation',
    )
    note_entreprise     = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    note_rapport        = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    note_soutenance     = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    note_finale         = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    jury                = models.ManyToManyField('prof.Prof', blank=True, related_name='evaluations_jury')
    est_valide_pfe      = models.BooleanField(default=False)
    date_soutenance     = models.DateField(null=True, blank=True)
    observations        = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'stages_evaluation'

    def __str__(self):
        return f'Évaluation stage {self.convention}'


class DerogationMedicale(models.Model):
    etudiant        = models.ForeignKey(
        'absence.Etudiant', on_delete=models.PROTECT, related_name='derogations',
    )
    motif           = models.TextField()
    date_debut      = models.DateField()
    date_fin        = models.DateField()
    justificatif    = models.FileField(upload_to='stages/derogations/', null=True, blank=True,
                                        validators=[validate_document])
    statut          = models.CharField(
        max_length=20, choices=STATUT_DEROGATION_CHOICES, default='soumise',
    )
    decision_motif  = models.TextField(blank=True, default='')
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'stages_derogation'
        ordering = ['-created_at']

    def __str__(self):
        return f'Dérogation {self.etudiant} ({self.statut})'
