"""
Section 1bis Groupe 4 institution_V1 — Découplage EM/Departement.

EM.departement passe de CASCADE à SET_NULL (et nullable). Permet aux EMs
(stables d'année en année) de survivre à la suppression d'un Departement
annuel (par exemple lors d'une purge d'année via management command).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('em', '0007_em_has_tp'),
        ('departement', '0003_institution_not_null'),
    ]

    operations = [
        migrations.AlterField(
            model_name='em',
            name='departement',
            field=models.ForeignKey(
                to='departement.departement',
                on_delete=models.deletion.SET_NULL,
                related_name='ems',
                null=True, blank=True,
            ),
        ),
    ]
