from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('emplois', '0002_db_column_fk'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='emplois',
            index=models.Index(
                fields=['annee_universitaire', 'departement', 'semestre'],
                name='emplois_annee_dept_sem_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='emplois',
            index=models.Index(
                fields=['annee_universitaire', 'jour', 'creneau_fk'],
                name='emplois_annee_jour_creneau_idx',
            ),
        ),
    ]
