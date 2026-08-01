"""
Migration 0006 — Refonte SessionEvaluation :
  - Supprime les FK filiere et semestre (sessions globales)
  - Ajoute le champ type_semestre ('Impairs' / 'Pairs')
  - Met à jour unique_together → (annee_univ, type_session, type_semestre)
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('evaluations', '0005_parametrejury_rachatnote'),
        ('parametres', '0003_initial'),
        ('scolarite', '0001_initial'),
    ]

    operations = [
        # 1 — Lever la contrainte unique existante avant de toucher aux colonnes
        migrations.AlterUniqueTogether(
            name='sessionevaluation',
            unique_together=set(),
        ),

        # 2 — Ajouter type_semestre (nullable d'abord pour les lignes existantes)
        migrations.AddField(
            model_name='sessionevaluation',
            name='type_semestre',
            field=models.CharField(
                max_length=10,
                choices=[
                    ('Impairs', 'Semestres impairs (S1, S3, S5)'),
                    ('Pairs',   'Semestres pairs   (S2, S4, S6)'),
                ],
                default='Impairs',
                help_text='Parité des semestres couverts : Impairs (S1,S3,S5) ou Pairs (S2,S4,S6).',
            ),
        ),

        # 3 — Supprimer les FK filiere et semestre
        migrations.RemoveField(
            model_name='sessionevaluation',
            name='filiere',
        ),
        migrations.RemoveField(
            model_name='sessionevaluation',
            name='semestre',
        ),

        # 4 — Rétablir unique_together avec la nouvelle structure
        migrations.AlterUniqueTogether(
            name='sessionevaluation',
            unique_together={('annee_univ', 'type_session', 'type_semestre')},
        ),
    ]
