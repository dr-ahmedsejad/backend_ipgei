"""
Ajoute EM.module_lmd → modules.Module (FK nullable).
Un Module LMD est composé d'un ensemble d'EMs de planification.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('em', '0002_em_coefficient_em_credits_em_est_element_module_and_more'),
        ('modules', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='em',
            name='module_lmd',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='ems_planification',
                to='modules.module',
                help_text='Module LMD auquel cet EM est rattaché (crédits, filière, semestre).',
            ),
        ),
    ]
