"""
Migration 0003 : ajoute les champs manquants dans l'état Django pour SuiviePointage
(commentaire, date_suivie, type_semestre, duree_creneau, taux_paiement).

Ces colonnes existent déjà dans la table suivi_suivie_pointage (créée par GesAFPED).
=> Sur MySQL : zéro SQL exécuté. Sur base neuve non-MySQL : colonnes ajoutées réellement.
"""
from django.db import migrations, models

from core.migration_ops import DejaAppliqueeSurMySQL


class Migration(migrations.Migration):

    dependencies = [
        ('suivi', '0002_db_column_fk'),
    ]

    operations = [
        DejaAppliqueeSurMySQL(
            database_operations=[],
            state_operations=[
                migrations.AddField(
                    model_name='suiviepointage',
                    name='commentaire',
                    field=models.CharField(blank=True, default='Non fait', max_length=100),
                ),
                migrations.AddField(
                    model_name='suiviepointage',
                    name='date_suivie',
                    field=models.DateField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name='suiviepointage',
                    name='type_semestre',
                    field=models.CharField(blank=True, default='I', max_length=10),
                ),
                migrations.AddField(
                    model_name='suiviepointage',
                    name='duree_creneau',
                    field=models.FloatField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name='suiviepointage',
                    name='taux_paiement',
                    field=models.FloatField(blank=True, null=True),
                ),
            ],
        ),
    ]
