from django.db import models

class Salle(models.Model):
    nom      = models.CharField(max_length=100, unique=True)
    capacite = models.IntegerField(default=0)

    class Meta:
        db_table = 'salle'
        ordering = ['nom']

    def __str__(self):
        return self.nom
