"""
Section 1 — Migration B+C : retirer Semestre.filiere et Semestre.annee_univ
+ ajouter UniqueConstraint(code_semestre, niveau_semestre, type_semestre).

Pré-requis : migration 0005_consolider_semestres_doublons doit avoir été exécutée
(garantit qu'aucun doublon ne viole la nouvelle contrainte d'unicité).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('parametres', '0005_consolider_semestres_doublons'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='semestre',
            name='filiere',
        ),
        migrations.RemoveField(
            model_name='semestre',
            name='annee_univ',
        ),
        migrations.AddConstraint(
            model_name='semestre',
            constraint=models.UniqueConstraint(
                fields=['code_semestre', 'niveau_semestre', 'type_semestre'],
                name='uniq_semestre_code_niveau_type',
            ),
        ),
    ]
