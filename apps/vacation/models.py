from django.db import models
from django.utils import timezone


class Surveillance(models.Model):
    prof        = models.ForeignKey('prof.Prof',               on_delete=models.PROTECT, related_name='surveillances')
    departement = models.ForeignKey('departement.Departement', on_delete=models.PROTECT, related_name='surveillances')
    duree       = models.FloatField(default=2.0)
    date        = models.DateField()
    annee_univ  = models.CharField(max_length=20)
    # Section 1bis institution_V1
    institution = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT,
        related_name='surveillances',
    )

    class Meta:
        db_table = 'vacation_surveillance'
        ordering = ['-date']


class Vacation(models.Model):
    prof         = models.ForeignKey('prof.Prof',           on_delete=models.PROTECT, related_name='vacations')
    departements = models.ManyToManyField('departement.Departement', blank=True, related_name='vacations')
    em           = models.ForeignKey('em.EM',               on_delete=models.SET_NULL, null=True, blank=True, related_name='vacations')
    type         = models.ForeignKey('parametres.Seance',   on_delete=models.SET_NULL, null=True, blank=True, related_name='vacations')
    duree        = models.FloatField(default=1.5)
    date         = models.DateField()
    annee_univ   = models.CharField(max_length=20)
    taux_paiement = models.FloatField(default=0.0)
    # Section 1bis institution_V1
    institution = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT,
        related_name='vacations',
    )

    class Meta:
        db_table = 'vacation_vacation'
        ordering = ['-date']

    def save(self, *args, **kwargs):
        if not self.taux_paiement and self.type:
            from apps.parametres.models import Paiement
            self.taux_paiement = Paiement.get_taux_at(self.type.type_seance, self.date or timezone.now().date())
        super().save(*args, **kwargs)

    @property
    def montant(self):
        return round(self.duree * self.taux_paiement, 2)

# Note : Vacation est deja branche aux signals d'audit globaux via
# core/signals.py:TRACKED_MODELS — pas besoin d'ajouter de post_delete ici
# (ferait du doublon).
