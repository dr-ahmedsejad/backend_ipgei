# Index de consultation (10/10/2026, comme à l'ISS) : AJOUT d'index, aucune
# ligne touchée.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('suivi', '0015_creer_pointage_departements'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='suivie',
            index=models.Index(fields=['departement', 'annee_universitaire', 'numero_semaine'],
                               name='suivie_dept_annee_sem_idx'),
        ),
    ]
