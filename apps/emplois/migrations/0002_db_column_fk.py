from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('emplois', '0001_initial'),
        ('em', '0001_initial'),
        ('prof', '0001_initial'),
        ('salle', '0001_initial'),
        ('parametres', '0001_initial'),
        ('departement', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='emplois',
            name='prof',
            field=models.ForeignKey('prof.Prof', on_delete=django.db.models.deletion.SET_NULL, null=True, blank=True, related_name='emplois', db_column='fk_prof_id'),
        ),
        migrations.AlterField(
            model_name='emplois',
            name='em',
            field=models.ForeignKey('em.EM', on_delete=django.db.models.deletion.SET_NULL, null=True, blank=True, related_name='emplois', db_column='fk_em_id'),
        ),
        migrations.AlterField(
            model_name='emplois',
            name='departement',
            field=models.ForeignKey('departement.Departement', on_delete=django.db.models.deletion.SET_NULL, null=True, blank=True, related_name='emplois', db_column='fk_departement_id'),
        ),
        migrations.AlterField(
            model_name='emplois',
            name='salle',
            field=models.ForeignKey('salle.Salle', on_delete=django.db.models.deletion.SET_NULL, null=True, blank=True, related_name='emplois', db_column='fk_salle_id'),
        ),
        migrations.AlterField(
            model_name='emplois',
            name='semestre',
            field=models.ForeignKey('parametres.Semestre', on_delete=django.db.models.deletion.SET_NULL, null=True, blank=True, related_name='emplois', db_column='fk_semestre_id'),
        ),
        migrations.AlterField(
            model_name='emplois',
            name='creneau_fk',
            field=models.ForeignKey('parametres.Creneau', on_delete=django.db.models.deletion.SET_NULL, null=True, blank=True, related_name='emplois', db_column='fk_creneau_id'),
        ),
        migrations.AlterField(
            model_name='emploisarchive',
            name='prof',
            field=models.ForeignKey('prof.Prof', on_delete=django.db.models.deletion.SET_NULL, null=True, blank=True, db_column='fk_prof_id'),
        ),
        migrations.AlterField(
            model_name='emploisarchive',
            name='em',
            field=models.ForeignKey('em.EM', on_delete=django.db.models.deletion.SET_NULL, null=True, blank=True, db_column='fk_em_id'),
        ),
        migrations.AlterField(
            model_name='emploisarchive',
            name='departement',
            field=models.ForeignKey('departement.Departement', on_delete=django.db.models.deletion.SET_NULL, null=True, blank=True, db_column='fk_departement_id'),
        ),
        migrations.AlterField(
            model_name='emploisarchive',
            name='salle',
            field=models.ForeignKey('salle.Salle', on_delete=django.db.models.deletion.SET_NULL, null=True, blank=True, db_column='fk_salle_id'),
        ),
        migrations.AlterField(
            model_name='emploisarchive',
            name='semestre',
            field=models.ForeignKey('parametres.Semestre', on_delete=django.db.models.deletion.SET_NULL, null=True, blank=True, db_column='fk_semestre_id'),
        ),
        migrations.AlterField(
            model_name='emploisarchive',
            name='creneau_fk',
            field=models.ForeignKey('parametres.Creneau', on_delete=django.db.models.deletion.SET_NULL, null=True, blank=True, db_column='fk_creneau_id'),
        ),
    ]
