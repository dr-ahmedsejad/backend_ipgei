import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scolarite', '0003_departement_academique'),
        ('parametres', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='departementacademique',
            name='institution',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='departements_academiques',
                to='parametres.institution',
            ),
        ),
    ]
