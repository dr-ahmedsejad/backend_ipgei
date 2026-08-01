from django.db import models

class Banque(models.Model):
    nom         = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'banque'
        ordering = ['nom']

    def __str__(self):
        return self.nom
