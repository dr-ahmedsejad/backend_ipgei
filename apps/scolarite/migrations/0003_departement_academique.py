"""
Migration additive : DepartementAcademique + FK nullable sur Filiere.
Aucune donnée pédagogique existante n'est affectée.
"""
from django.conf import settings
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scolarite', '0002_alter_filiere_type_diplome'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='DepartementAcademique',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=20, unique=True)),
                ('intitule_fr', models.CharField(max_length=200)),
                ('intitule_ar', models.CharField(blank=True, default='', max_length=200)),
                ('actif', models.BooleanField(default=True)),
                ('responsable', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='departements_diriges',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'verbose_name': 'Département académique',
                'verbose_name_plural': 'Départements académiques',
                'db_table': 'scolarite_departement_academique',
                'ordering': ['code'],
            },
        ),
        migrations.AddField(
            model_name='filiere',
            name='departement_academique',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='filieres',
                to='scolarite.departementacademique',
            ),
        ),
    ]
