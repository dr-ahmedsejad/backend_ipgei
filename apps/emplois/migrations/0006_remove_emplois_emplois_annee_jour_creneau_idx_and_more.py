"""
State-only : aligne le state Django avec le schema reel de gesafped26.

Le DDL est deja en place :
- 6 colonnes FK sur emplois_emploisarchive existent (fk_prof_id, fk_em_id, ...)
- L'ancien index emplois_annee_jour_creneau_idx n'existe pas en BD
- Le nouveau index (annee_universitaire, jour_fk, creneau_fk) n'existe pas non plus

Ce qui se passe ici :
- RemoveIndex : noop (l'index n'existe pas en BD)
- AlterField x6 : noop (les colonnes existent deja avec les bons db_column)
- AddIndex : noop ICI (volontairement omis cote DB ; un futur sprint perf pourra
  l'ajouter via une migration RunSQL/ALTER si benchmark le justifie)

A appliquer en --fake.

Sur base neuve non-MySQL : toutes les operations (RemoveIndex, AlterField x6,
AddIndex) sont executees reellement pour que le schema physique colle au state.
"""
import django.db.models.deletion
from django.db import migrations, models

from core.migration_ops import DejaAppliqueeSurMySQL


class Migration(migrations.Migration):

    dependencies = [
        ('departement', '0003_institution_not_null'),
        ('em', '0008_em_departement_set_null'),
        ('emplois', '0005_drop_legacy_charfields'),
        ('parametres', '0008_alter_semaine_annee_universitaire'),
        ('prof', '0003_enseignant_role_prof_user'),
        ('salle', '0002_alter_salle_table'),
    ]

    operations = [
        # NB : le RemoveIndex(emplois_annee_jour_creneau_idx) historique a ete
        # deplace dans 0005 (il doit preceder le RemoveField(jour) dans le
        # state pour etre rejouable sur base neuve). Etat final identique.
        DejaAppliqueeSurMySQL(
            database_operations=[],
            state_operations=[
                migrations.AlterField(
                    model_name='emploisarchive',
                    name='creneau_fk',
                    field=models.ForeignKey(
                        blank=True, db_column='fk_creneau_id', null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='+', to='parametres.creneau',
                    ),
                ),
                migrations.AlterField(
                    model_name='emploisarchive',
                    name='departement',
                    field=models.ForeignKey(
                        blank=True, db_column='fk_departement_id', null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='+', to='departement.departement',
                    ),
                ),
                migrations.AlterField(
                    model_name='emploisarchive',
                    name='em',
                    field=models.ForeignKey(
                        blank=True, db_column='fk_em_id', null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='+', to='em.em',
                    ),
                ),
                migrations.AlterField(
                    model_name='emploisarchive',
                    name='prof',
                    field=models.ForeignKey(
                        blank=True, db_column='fk_prof_id', null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='+', to='prof.prof',
                    ),
                ),
                migrations.AlterField(
                    model_name='emploisarchive',
                    name='salle',
                    field=models.ForeignKey(
                        blank=True, db_column='fk_salle_id', null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='+', to='salle.salle',
                    ),
                ),
                migrations.AlterField(
                    model_name='emploisarchive',
                    name='semestre',
                    field=models.ForeignKey(
                        blank=True, db_column='fk_semestre_id', null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='+', to='parametres.semestre',
                    ),
                ),
                migrations.AddIndex(
                    model_name='emplois',
                    index=models.Index(
                        fields=['annee_universitaire', 'jour_fk', 'creneau_fk'],
                        name='emplois_annee_jour_creneau_idx',
                    ),
                ),
            ],
        ),
    ]
