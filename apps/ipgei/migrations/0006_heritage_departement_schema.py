"""
Étape 2/3 : conversion de schéma.

`Classe` et `SousGroupeTP` abandonnent leur clé primaire propre au profit de
celle du département parent, et perdent les champs qui vivent désormais sur le
parent : `institution`, `annee_universitaire`, `niveau` (qui devient une clé
étrangère vers `parametres.Niveau`) et `libelle` (repris par `groupe`).

Le `departement_miroir` disparaît : il n'y a plus deux objets à relier.

Les `default=1` sur les liens de parenté ne s'appliquent jamais — la migration
0005 a vidé les deux tables — mais Django les exige pour ajouter une colonne
non nulle. `preserve_default=False` les retire aussitôt du schéma.

Les contraintes d'unicité d'origine mêlaient champs locaux et champs du parent,
ce que Django interdit : elles sont remplacées par la colonne dérivée
`cle_unicite`, garantie unique en base et recalculée à chaque enregistrement.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('departement', '0007_fix_niveau_nullable'),
        ('ipgei', '0005_heritage_departement_sauvegarde'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='classe',
            options={'ordering': ['-annee_universitaire', 'nom'], 'verbose_name': 'classe IPGEI', 'verbose_name_plural': 'classes IPGEI'},
        ),
        migrations.AlterModelOptions(
            name='sousgroupetp',
            options={'ordering': ['classe', 'groupe'], 'verbose_name': 'sous-groupe de TP', 'verbose_name_plural': 'sous-groupes de TP'},
        ),
        migrations.RemoveConstraint(
            model_name='classe',
            name='uniq_ipgei_classe',
        ),
        migrations.RemoveConstraint(
            model_name='sousgroupetp',
            name='uniq_ipgei_sous_groupe',
        ),
        migrations.RemoveIndex(
            model_name='classe',
            name='ipgei_class_annee_u_35db5f_idx',
        ),
        migrations.RemoveField(
            model_name='classe',
            name='annee_universitaire',
        ),
        migrations.RemoveField(
            model_name='classe',
            name='departement_miroir',
        ),
        migrations.RemoveField(
            model_name='classe',
            name='id',
        ),
        migrations.RemoveField(
            model_name='classe',
            name='institution',
        ),
        migrations.RemoveField(
            model_name='classe',
            name='libelle',
        ),
        migrations.RemoveField(
            model_name='classe',
            name='niveau',
        ),
        migrations.RemoveField(
            model_name='sousgroupetp',
            name='id',
        ),
        migrations.RemoveField(
            model_name='sousgroupetp',
            name='libelle',
        ),
        migrations.AddField(
            model_name='classe',
            name='cle_unicite',
            field=models.CharField(default='', editable=False, max_length=120, unique=True),
        ),
        migrations.AddField(
            model_name='classe',
            name='departement_ptr',
            field=models.OneToOneField(default=1, on_delete=django.db.models.deletion.CASCADE, parent_link=True, primary_key=True, related_name='classe_ipgei', serialize=False, to='departement.departement'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='sousgroupetp',
            name='cle_unicite',
            field=models.CharField(default='', editable=False, max_length=120, unique=True),
        ),
        migrations.AddField(
            model_name='sousgroupetp',
            name='departement_ptr',
            field=models.OneToOneField(default=1, on_delete=django.db.models.deletion.CASCADE, parent_link=True, primary_key=True, related_name='sous_groupe_ipgei', serialize=False, to='departement.departement'),
            preserve_default=False,
        ),
    ]
