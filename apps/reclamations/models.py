from django.db import models

from core.validators import validate_document


TYPE_CHOICES = [
    ('absence', 'Absence'),
    ('note',    'Note'),
    ('autre',   'Autre'),
]

STATUT_CHOICES = [
    ('soumise',  'Soumise'),
    ('en_cours', 'En cours'),
    ('acceptee', 'Acceptée'),
    ('rejetee',  'Rejetée'),
]


class Reclamation(models.Model):
    etudiant             = models.ForeignKey(
        'absence.Etudiant',
        on_delete=models.CASCADE,
        related_name='reclamations',
    )
    type_reclamation     = models.CharField(max_length=20, choices=TYPE_CHOICES, default='autre')
    statut               = models.CharField(max_length=20, choices=STATUT_CHOICES, default='soumise')

    # Références optionnelles selon le type
    presence             = models.ForeignKey(
        'absence.Presence',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='reclamations',
    )
    inscription_element  = models.ForeignKey(
        'inscriptions.InscriptionElement',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='reclamations',
    )
    session_evaluation   = models.ForeignKey(
        'evaluations.SessionEvaluation',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='reclamations',
    )

    motif                = models.TextField()
    justificatif         = models.FileField(upload_to='reclamations/justificatifs/', null=True, blank=True,
                                             validators=[validate_document])

    # Traitement staff
    reponse              = models.TextField(blank=True, default='')
    traitee_par          = models.ForeignKey(
        'authentication.CustomUser',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='reclamations_traitees',
    )
    date_soumission      = models.DateTimeField(auto_now_add=True)
    date_traitement      = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'reclamations_reclamation'
        ordering = ['-date_soumission']

    def __str__(self):
        return f'{self.etudiant} — {self.type_reclamation} ({self.statut})'


TYPE_SESSION_CHOICES = [
    ('normale',    'Normale (SN)'),
    ('rattrapage', 'Rattrapage (SR)'),
]

TYPE_SEMESTRE_CHOICES = [
    ('I', 'Impairs (S1 / S3 / S5)'),
    ('P', 'Pairs (S2 / S4 / S6)'),
]


class PeriodeReclamation(models.Model):
    """
    Fenetre temporelle pendant laquelle les etudiants peuvent reclamer
    sur leurs notes pour un perimetre (annee + parite + session) donne.
    Cree par la SG apres cloture d'une session SR pour donner X jours
    aux etudiants pour formuler des reclamations sur leurs notes finales.
    """
    annee_univ      = models.ForeignKey(
        'parametres.Year', on_delete=models.PROTECT,
        related_name='periodes_reclamation',
    )
    type_session    = models.CharField(max_length=20, choices=TYPE_SESSION_CHOICES, default='rattrapage')
    type_semestre   = models.CharField(max_length=1, choices=TYPE_SEMESTRE_CHOICES)
    institution     = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT,
        related_name='periodes_reclamation',
    )
    filiere         = models.ForeignKey(
        'scolarite.Filiere', on_delete=models.PROTECT,
        null=True, blank=True, related_name='periodes_reclamation',
        help_text="Si vide : toutes les filieres de l'institution.",
    )
    niveau          = models.IntegerField(
        null=True, blank=True,
        help_text="Si vide : tous les niveaux.",
    )

    date_ouverture  = models.DateTimeField()
    date_fermeture  = models.DateTimeField()
    actif           = models.BooleanField(default=True)

    cree_par        = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='periodes_reclamation_creees',
    )
    motif           = models.CharField(
        max_length=200, blank=True, default='',
        help_text="Texte court affiche aux etudiants (ex : 'Reclamation SR-I 2025-2026').",
    )
    date_creation     = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'reclamations_periode'
        ordering = ['-date_ouverture']
        indexes = [
            models.Index(fields=['actif', 'date_ouverture', 'date_fermeture']),
            models.Index(fields=['annee_univ', 'type_semestre']),
        ]

    def __str__(self):
        return (
            f'Periode {self.get_type_session_display()} {self.get_type_semestre_display()} '
            f'{self.annee_univ} — {self.date_ouverture:%d/%m %H:%M} → {self.date_fermeture:%d/%m %H:%M}'
        )

    @property
    def est_en_cours(self):
        from django.utils import timezone
        now = timezone.now()
        return self.actif and self.date_ouverture <= now <= self.date_fermeture

    @property
    def statut_temporel(self):
        """'a_venir' | 'en_cours' | 'fermee' | 'inactive'"""
        if not self.actif:
            return 'inactive'
        from django.utils import timezone
        now = timezone.now()
        if now < self.date_ouverture:
            return 'a_venir'
        if now > self.date_fermeture:
            return 'fermee'
        return 'en_cours'
