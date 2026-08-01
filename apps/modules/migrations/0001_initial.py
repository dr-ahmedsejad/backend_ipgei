from decimal import Decimal
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('parametres', '0003_initial'),
        ('scolarite', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Module',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=20, unique=True)),
                ('intitule_fr', models.CharField(max_length=200)),
                ('intitule_ar', models.CharField(blank=True, default='', max_length=200)),
                ('credits', models.PositiveIntegerField(default=0)),
                ('coefficient', models.DecimalField(decimal_places=2, default=Decimal('1.00'), max_digits=4)),
                ('seuil_compensation', models.DecimalField(decimal_places=2, default=Decimal('10.00'), max_digits=4)),
                ('actif', models.BooleanField(default=True)),
                ('filiere', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='modules',
                    to='scolarite.filiere',
                )),
                ('semestre', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='modules',
                    to='parametres.semestre',
                )),
            ],
            options={
                'verbose_name': 'Module',
                'verbose_name_plural': 'Modules',
                'db_table': 'modules_module',
                'ordering': ['filiere', 'semestre', 'code'],
            },
        ),
        migrations.CreateModel(
            name='ElementModule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=20, unique=True)),
                ('intitule_fr', models.CharField(max_length=200)),
                ('intitule_ar', models.CharField(blank=True, default='', max_length=200)),
                ('credits', models.PositiveIntegerField(default=0)),
                ('coefficient', models.DecimalField(decimal_places=2, default=Decimal('1.00'), max_digits=4)),
                ('poids_cc', models.DecimalField(decimal_places=2, default=Decimal('0.30'), max_digits=3)),
                ('poids_tp', models.DecimalField(decimal_places=2, default=Decimal('0.20'), max_digits=3)),
                ('poids_exam', models.DecimalField(decimal_places=2, default=Decimal('0.50'), max_digits=3)),
                ('seuil_eliminatoire', models.DecimalField(
                    blank=True, decimal_places=2, max_digits=4, null=True,
                    help_text="Note /20 en dessous de laquelle l'élément est éliminatoire.",
                )),
                ('ordre', models.PositiveSmallIntegerField(default=0)),
                ('module', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='elements',
                    to='modules.module',
                )),
            ],
            options={
                'verbose_name': 'Élément de module',
                'verbose_name_plural': 'Éléments de module',
                'db_table': 'modules_element',
                'ordering': ['module', 'ordre', 'code'],
            },
        ),
    ]
