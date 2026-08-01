# Migration corrigee pour cohabiter avec la DB de production.
#
# Situation :
#   - La table 'institution' existe deja avec les bons noms de colonnes
#     (quelqu'un les a ajoutes manuellement avant cette migration).
#   - Les tables 'semestre' et 'annee' n'ont PAS encore les nouveaux champs.
#   - Les noms de tables en DB sont 'institution', 'semestre', 'annee'
#     mais l'etat Django (depuis 0001) les connait sous 'parametres_institution',
#     'parametres_semestre', 'parametres_year' → on corrige via SeparateDatabaseAndState.

from django.db import migrations, models
import django.db.models.deletion

from core.migration_ops import DejaAppliqueeSurMySQL


class Migration(migrations.Migration):

    dependencies = [
        ('parametres', '0001_initial'),
    ]

    operations = [
        # Etape 1 : corriger l'etat Django pour les 3 tables concernees par ce
        # fichier. Aucune SQL executee sur MySQL (tables deja aux bons noms) ;
        # sur base neuve (sqlite/PostgreSQL), les RENAME sont executes reellement.
        DejaAppliqueeSurMySQL(
            state_operations=[
                migrations.AlterModelTable(name='institution', table='institution'),
                migrations.AlterModelTable(name='semestre',    table='semestre'),
                migrations.AlterModelTable(name='year',        table='annee'),
            ],
            database_operations=[],
        ),

        # Etape 2 : champs Institution — deja presents en DB MySQL (etat-seulement) ;
        # ajoutes physiquement sur base neuve non-MySQL.
        DejaAppliqueeSurMySQL(
            state_operations=[
                migrations.AddField(model_name='institution', name='adresse_ar',         field=models.TextField(blank=True, default='')),
                migrations.AddField(model_name='institution', name='adresse_fr',         field=models.TextField(blank=True, default='')),
                migrations.AddField(model_name='institution', name='code_etablissement', field=models.CharField(blank=True, default='', max_length=20)),
                migrations.AddField(model_name='institution', name='devise_ar',          field=models.CharField(blank=True, default='', max_length=200)),
                migrations.AddField(model_name='institution', name='devise_fr',          field=models.CharField(blank=True, default='', max_length=200)),
                migrations.AddField(model_name='institution', name='directeur_nom_ar',   field=models.CharField(blank=True, default='', max_length=200)),
                migrations.AddField(model_name='institution', name='directeur_nom_fr',   field=models.CharField(blank=True, default='', max_length=200)),
                migrations.AddField(model_name='institution', name='directeur_signature',field=models.ImageField(blank=True, null=True, upload_to='institutions/signatures/')),
                migrations.AddField(model_name='institution', name='directeur_titre_ar', field=models.CharField(blank=True, default='', max_length=100)),
                migrations.AddField(model_name='institution', name='directeur_titre_fr', field=models.CharField(blank=True, default='', max_length=100)),
                migrations.AddField(model_name='institution', name='email',              field=models.EmailField(blank=True, default='', max_length=254)),
                migrations.AddField(model_name='institution', name='est_principale',     field=models.BooleanField(default=True)),
                migrations.AddField(model_name='institution', name='favicon',            field=models.ImageField(blank=True, null=True, upload_to='institutions/favicons/')),
                migrations.AddField(model_name='institution', name='fax',               field=models.CharField(blank=True, default='', max_length=20)),
                migrations.AddField(model_name='institution', name='logo',              field=models.ImageField(blank=True, null=True, upload_to='institutions/logos/')),
                migrations.AddField(model_name='institution', name='logo_republique',   field=models.ImageField(blank=True, null=True, upload_to='institutions/sceaux/')),
                migrations.AddField(model_name='institution', name='ministere_ar',      field=models.CharField(blank=True, default='', max_length=200)),
                migrations.AddField(model_name='institution', name='ministere_fr',      field=models.CharField(blank=True, default='', max_length=200)),
                migrations.AddField(model_name='institution', name='nom_ar',            field=models.CharField(blank=True, default='', max_length=200)),
                migrations.AddField(model_name='institution', name='nom_complet_ar',    field=models.CharField(blank=True, default='', max_length=500)),
                migrations.AddField(model_name='institution', name='nom_complet_fr',    field=models.CharField(blank=True, default='', max_length=500)),
                migrations.AddField(model_name='institution', name='nom_fr',            field=models.CharField(blank=True, default='', max_length=200)),
                migrations.AddField(model_name='institution', name='pays_ar',           field=models.CharField(blank=True, default='موريتانيا', max_length=100)),
                migrations.AddField(model_name='institution', name='pays_fr',           field=models.CharField(blank=True, default='Mauritanie', max_length=100)),
                migrations.AddField(model_name='institution', name='site_web',          field=models.URLField(blank=True, default='')),
                migrations.AddField(model_name='institution', name='telephone',         field=models.CharField(blank=True, default='', max_length=20)),
                migrations.AddField(model_name='institution', name='type_etablissement',field=models.CharField(choices=[('universite', 'Université'), ('ecole', 'École Supérieure'), ('institut', 'Institut')], default='ecole', max_length=30)),
                migrations.AddField(model_name='institution', name='ville_ar',          field=models.CharField(blank=True, default='', max_length=100)),
                migrations.AddField(model_name='institution', name='ville_fr',          field=models.CharField(blank=True, default='', max_length=100)),
            ],
            database_operations=[],  # colonnes deja presentes en DB MySQL
        ),

        # Etape 3 : champs Semestre — pas encore en DB, executes normalement.
        migrations.AddField(
            model_name='semestre',
            name='annee_univ',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='semestres', to='parametres.year'),
        ),
        migrations.AddField(
            model_name='semestre',
            name='credits',
            field=models.IntegerField(default=30),
        ),
    ]
