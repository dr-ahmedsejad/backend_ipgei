from django.db import models
from django.utils import timezone


class Emplois(models.Model):
    annee_universitaire = models.CharField(max_length=20, blank=True, db_index=True)
    type_semestre   = models.CharField(max_length=1, blank=True)
    taux_paiement   = models.FloatField(default=0.0)

    # ForeignKeys — db_column align with siga schema (see migration 0002_db_column_fk).
    prof        = models.ForeignKey('prof.Prof',               on_delete=models.SET_NULL, null=True, blank=True, related_name='emplois', db_column='fk_prof_id')
    em          = models.ForeignKey('em.EM',                   on_delete=models.SET_NULL, null=True, blank=True, related_name='emplois', db_column='fk_em_id')
    departement = models.ForeignKey('departement.Departement', on_delete=models.SET_NULL, null=True, blank=True, related_name='emplois', db_column='fk_departement_id')
    salle       = models.ForeignKey('salle.Salle',             on_delete=models.SET_NULL, null=True, blank=True, related_name='emplois', db_column='fk_salle_id')
    semestre    = models.ForeignKey('parametres.Semestre',     on_delete=models.SET_NULL, null=True, blank=True, related_name='emplois', db_column='fk_semestre_id')
    creneau_fk  = models.ForeignKey('parametres.Creneau',      on_delete=models.SET_NULL, null=True, blank=True, related_name='emplois', db_column='fk_creneau_id')
    type_seance_fk = models.ForeignKey('parametres.Seance',    on_delete=models.SET_NULL, null=True, blank=True, related_name='emplois')
    jour_fk        = models.ForeignKey('parametres.Jour',      on_delete=models.SET_NULL, null=True, blank=True, related_name='emplois')
    # Section 1bis institution_V1 — isolation multi-institution
    institution = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT,
        related_name='emplois',
    )

    class Meta:
        db_table = 'emplois_emplois'
        indexes  = [
            models.Index(
                fields=['annee_universitaire', 'departement', 'semestre'],
                name='emplois_annee_dept_sem_idx',
            ),
            models.Index(
                fields=['annee_universitaire', 'jour_fk', 'creneau_fk'],
                name='emplois_annee_jour_creneau_idx',
            ),
        ]

    def save(self, *args, **kwargs):
        # Auto-populate taux_paiement si non renseigne
        if not self.taux_paiement and self.type_seance_fk_id and self.type_seance_fk:
            from apps.parametres.models import Paiement
            self.taux_paiement = Paiement.get_taux_at(
                self.type_seance_fk.type_seance,
                timezone.now().date(),
            )
        super().save(*args, **kwargs)

    def __str__(self):
        return f'Emplois#{self.pk} {self.prof} / {self.em}'


class EmploisArchive(models.Model):
    annee_universitaire = models.CharField(max_length=20, blank=True)
    type_semestre   = models.CharField(max_length=1, blank=True)
    taux_paiement   = models.FloatField(default=0.0)
    prof        = models.ForeignKey('prof.Prof',               on_delete=models.SET_NULL, null=True, blank=True, related_name='+', db_column='fk_prof_id')
    em          = models.ForeignKey('em.EM',                   on_delete=models.SET_NULL, null=True, blank=True, related_name='+', db_column='fk_em_id')
    departement = models.ForeignKey('departement.Departement', on_delete=models.SET_NULL, null=True, blank=True, related_name='+', db_column='fk_departement_id')
    salle       = models.ForeignKey('salle.Salle',             on_delete=models.SET_NULL, null=True, blank=True, related_name='+', db_column='fk_salle_id')
    semestre    = models.ForeignKey('parametres.Semestre',     on_delete=models.SET_NULL, null=True, blank=True, related_name='+', db_column='fk_semestre_id')
    creneau_fk  = models.ForeignKey('parametres.Creneau',      on_delete=models.SET_NULL, null=True, blank=True, related_name='+', db_column='fk_creneau_id')
    type_seance_fk = models.ForeignKey('parametres.Seance',    on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    jour_fk        = models.ForeignKey('parametres.Jour',      on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    # Section 1bis institution_V1
    institution = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT,
        related_name='emplois_archive',
    )

    class Meta:
        db_table = 'emplois_emploisarchive'
