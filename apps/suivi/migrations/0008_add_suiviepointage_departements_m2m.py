"""
Migration 0008 : ajoute le M2M `departements` sur SuiviePointage
(remplace l'usage du CharField `id_departement` qui stockait
plusieurs noms de departements separes par `/`).

La table physique `suivi_pointage_departements` a ete creee
et peuplee hors-Django (cf. backups/audit_*.md).
=> SeparateDatabaseAndState : zero SQL execute.

Le through-model `SuiviePointageDepartement` est `managed=False`
donc Django ne tente jamais de creer/modifier la table.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('suivi', '0007_add_institution_fk'),
        ('departement', '0001_initial'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],   # ne rien toucher en DB (table deja existante)
            state_operations=[
                migrations.CreateModel(
                    name='SuiviePointageDepartement',
                    fields=[
                        ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('suiviepointage', models.ForeignKey(
                            'suivi.SuiviePointage', on_delete=models.deletion.CASCADE,
                            db_column='suiviepointage_id', related_name='+',
                        )),
                        ('departement', models.ForeignKey(
                            'departement.Departement', on_delete=models.deletion.PROTECT,
                            db_column='departement_id', related_name='+',
                        )),
                    ],
                    options={
                        'db_table': 'suivi_pointage_departements',
                        'managed': False,
                        'unique_together': {('suiviepointage', 'departement')},
                    },
                ),
                migrations.AddField(
                    model_name='suiviepointage',
                    name='departements',
                    field=models.ManyToManyField(
                        through='suivi.SuiviePointageDepartement',
                        related_name='suivies_pointage',
                        to='departement.departement',
                        blank=True,
                    ),
                ),
            ],
        ),
    ]
