from django.db import models
from django.utils import timezone


class Suivie(models.Model):
    annee_universitaire = models.CharField(max_length=20, blank=True, db_index=True)
    numero_semaine      = models.IntegerField(default=0)
    commentaire         = models.TextField(blank=True, default='')
    date_suivie         = models.DateField(null=True, blank=True)
    type_semestre       = models.CharField(max_length=1, blank=True)
    duree_creneau       = models.FloatField(default=1.5)
    taux_paiement       = models.FloatField(default=0.0)

    prof        = models.ForeignKey('prof.Prof',               on_delete=models.PROTECT,  null=True, blank=True, related_name='suivies',  db_column='fk_prof_id')
    em          = models.ForeignKey('em.EM',                   on_delete=models.SET_NULL, null=True, blank=True, related_name='suivies',  db_column='fk_em_id')
    departement = models.ForeignKey('departement.Departement', on_delete=models.SET_NULL, null=True, blank=True, related_name='suivies',  db_column='fk_departement_id')
    salle       = models.ForeignKey('salle.Salle',             on_delete=models.SET_NULL, null=True, blank=True, related_name='suivies',  db_column='fk_salle_id')
    semestre    = models.ForeignKey('parametres.Semestre',     on_delete=models.SET_NULL, null=True, blank=True, related_name='suivies',  db_column='fk_semestre_id')
    creneau_fk  = models.ForeignKey('parametres.Creneau',      on_delete=models.SET_NULL, null=True, blank=True, related_name='suivies',  db_column='fk_creneau_id')
    type_seance_fk = models.ForeignKey('parametres.Seance',    on_delete=models.SET_NULL, null=True, blank=True, related_name='suivies')
    jour_fk        = models.ForeignKey('parametres.Jour',      on_delete=models.SET_NULL, null=True, blank=True, related_name='suivies')
    institution = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT,
        related_name='suivies',
    )

    class Meta:
        db_table = 'suivi_suivie'
        ordering = ['-annee_universitaire', '-numero_semaine']

    def save(self, *args, **kwargs):
        # Auto-fill duree_creneau et taux_paiement si non renseignes
        if self.creneau_fk_id and self.creneau_fk:
            self.duree_creneau = self.creneau_fk.duree
        if not self.taux_paiement and self.type_seance_fk_id and self.type_seance_fk:
            from apps.parametres.models import Paiement
            self.taux_paiement = Paiement.get_taux_at(
                self.type_seance_fk.type_seance,
                self.date_suivie or timezone.now().date(),
            )
        super().save(*args, **kwargs)


class SuiviePointage(models.Model):
    """Suivi multi-departements (pointage). Le multi-dept est gere par la M2M `departements`."""
    annee_universitaire = models.CharField(max_length=20, blank=True)
    numero_semaine      = models.IntegerField(default=0)
    commentaire         = models.CharField(max_length=100, blank=True, default='Non fait')
    date_suivie         = models.DateField(null=True, blank=True)
    type_semestre       = models.CharField(max_length=10, blank=True, default='I')
    duree_creneau       = models.FloatField(null=True, blank=True)
    taux_paiement       = models.FloatField(null=True, blank=True)
    reclamation_motif   = models.TextField(blank=True, default='')
    reclamation_statut  = models.CharField(
        max_length=20, blank=True, default='',
        choices=[('', 'Aucune'), ('en_attente', 'En attente'), ('acceptee', 'Acceptée'), ('rejetee', 'Rejetée')],
    )

    prof        = models.ForeignKey('prof.Prof',               on_delete=models.PROTECT,  null=True, blank=True, db_column='fk_prof_id')
    em          = models.ForeignKey('em.EM',                   on_delete=models.SET_NULL, null=True, blank=True, db_column='fk_em_id')
    salle       = models.ForeignKey('salle.Salle',             on_delete=models.SET_NULL, null=True, blank=True, db_column='fk_salle_id')
    semestre    = models.ForeignKey('parametres.Semestre',     on_delete=models.SET_NULL, null=True, blank=True, db_column='fk_semestre_id')
    creneau_fk  = models.ForeignKey('parametres.Creneau',      on_delete=models.SET_NULL, null=True, blank=True, db_column='fk_creneau_id')
    type_seance_fk = models.ForeignKey('parametres.Seance',    on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    jour_fk        = models.ForeignKey('parametres.Jour',      on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    institution = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT,
        related_name='suivies_pointage',
    )

    # Multi-departement (remplace le CharField id_departement multi-valeurs).
    departements = models.ManyToManyField(
        'departement.Departement',
        through='SuiviePointageDepartement',
        related_name='suivies_pointage',
        blank=True,
    )

    class Meta:
        db_table = 'suivi_suivie_pointage'
        indexes = [
            # Chemin chaud : calcul de charge / états de paiement filtrent par
            # prof + année + statut de pointage (_compute_charge_permanents, etc.).
            models.Index(fields=['prof', 'annee_universitaire', 'commentaire'],
                         name='sp_prof_annee_comm_idx'),
        ]


class SuiviePointageDepartement(models.Model):
    """Through-model M2M SuiviePointage <-> Departement.

    La table physique est creee et peuplee hors-Django (cf. apply_pointage_m2m_backfill).
    `managed = False` : Django ne tente jamais de creer/migrer la table.
    """
    suiviepointage = models.ForeignKey(
        SuiviePointage, on_delete=models.CASCADE,
        db_column='suiviepointage_id', related_name='+',
    )
    departement = models.ForeignKey(
        'departement.Departement', on_delete=models.PROTECT,
        db_column='departement_id', related_name='+',
    )

    class Meta:
        db_table = 'suivi_pointage_departements'
        managed = False
        unique_together = ('suiviepointage', 'departement')


class ChargeInstitution(models.Model):
    institution         = models.ForeignKey('parametres.Institution', on_delete=models.CASCADE, related_name='charges')
    prof                = models.ForeignKey('prof.Prof',              on_delete=models.PROTECT, related_name='charges_institution')
    charge_cm           = models.IntegerField(default=0)
    annee_universitaire = models.CharField(max_length=20)

    class Meta:
        db_table       = 'suivi_chargeinstitution'
        unique_together = ('prof', 'institution', 'annee_universitaire')


class SuiviGenerationAuthorization(models.Model):
    """Autorisation ponctuelle accordee par un admin a un user pour generer
    le suivi d'une semaine cloturee (rattrapage hors fenetre temporelle normale).

    Workflow :
      1. Admin accorde via /parametres/permissions-suivi (POST grant)
      2. User voit la semaine devenir cliquable cote /suivi/ajouter
      3. User genere -> used_at est rempli, l'autorisation est consommee
      4. Admin peut revoquer (DELETE) tant que pas used_at
    """
    user = models.ForeignKey(
        'authentication.CustomUser',
        on_delete=models.CASCADE,
        related_name='suivi_auths',
    )
    annee_universitaire = models.CharField(max_length=20)
    type_semestre       = models.CharField(max_length=1)
    numero_semaine      = models.IntegerField()
    granted_by = models.ForeignKey(
        'authentication.CustomUser',
        on_delete=models.PROTECT,
        related_name='suivi_auths_granted',
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    used_at    = models.DateTimeField(null=True, blank=True)
    note       = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        db_table = 'suivi_generation_authorization'
        unique_together = ('user', 'annee_universitaire', 'type_semestre', 'numero_semaine')
        indexes = [
            models.Index(fields=['user', 'annee_universitaire', 'type_semestre']),
        ]

    def __str__(self):
        return f'Auth #{self.pk} user={self.user_id} sem={self.numero_semaine} ({self.annee_universitaire}/{self.type_semestre})'

    @property
    def is_used(self) -> bool:
        return self.used_at is not None
