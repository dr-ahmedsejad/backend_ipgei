"""
Migration 0002 : aligne db_column des ForeignKeys de Suivie et SuiviePointage
avec les noms de colonnes réels créés par GesAFPED (fk_prof_id, fk_em_id, ...).

Sur MySQL : la base de données N'EST PAS modifiée (les colonnes existent déjà
avec les bons noms), seul l'état Django est mis à jour.
Sur base neuve non-MySQL : les renommages de colonnes sont exécutés réellement.
"""
from django.db import migrations, models
import django.db.models.deletion

from core.migration_ops import DejaAppliqueeSurMySQL


class Migration(migrations.Migration):

    dependencies = [
        ('departement', '0001_initial'),
        ('em',          '0001_initial'),
        ('parametres',  '0001_initial'),
        ('prof',        '0001_initial'),
        ('salle',       '0001_initial'),
        ('suivi',       '0001_initial'),
    ]

    operations = [
        # ── Aucune SQL sur MySQL : la DB a déjà les bonnes colonnes ──
        DejaAppliqueeSurMySQL(
            database_operations=[],   # ne rien faire sur la DB MySQL
            state_operations=[

                # ── Suivie ──────────────────────────────────────────────────
                migrations.AlterField(
                    model_name='suivie',
                    name='prof',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='suivies', to='prof.prof',
                        db_column='fk_prof_id',
                    ),
                ),
                migrations.AlterField(
                    model_name='suivie',
                    name='em',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='suivies', to='em.em',
                        db_column='fk_em_id',
                    ),
                ),
                migrations.AlterField(
                    model_name='suivie',
                    name='departement',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='suivies', to='departement.departement',
                        db_column='fk_departement_id',
                    ),
                ),
                migrations.AlterField(
                    model_name='suivie',
                    name='salle',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='suivies', to='salle.salle',
                        db_column='fk_salle_id',
                    ),
                ),
                migrations.AlterField(
                    model_name='suivie',
                    name='semestre',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='suivies', to='parametres.semestre',
                        db_column='fk_semestre_id',
                    ),
                ),
                migrations.AlterField(
                    model_name='suivie',
                    name='creneau_fk',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='suivies', to='parametres.creneau',
                        db_column='fk_creneau_id',
                    ),
                ),

                # ── SuiviePointage ───────────────────────────────────────────
                migrations.AlterField(
                    model_name='suiviepointage',
                    name='prof',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to='prof.prof', db_column='fk_prof_id',
                    ),
                ),
                migrations.AlterField(
                    model_name='suiviepointage',
                    name='em',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to='em.em', db_column='fk_em_id',
                    ),
                ),
                migrations.AlterField(
                    model_name='suiviepointage',
                    name='salle',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to='salle.salle', db_column='fk_salle_id',
                    ),
                ),
                migrations.AlterField(
                    model_name='suiviepointage',
                    name='semestre',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to='parametres.semestre', db_column='fk_semestre_id',
                    ),
                ),
                migrations.AlterField(
                    model_name='suiviepointage',
                    name='creneau_fk',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to='parametres.creneau', db_column='fk_creneau_id',
                    ),
                ),
                migrations.AlterField(
                    model_name='suiviepointage',
                    name='departements',
                    field=models.ManyToManyField(
                        blank=True,
                        related_name='pointages',
                        to='departement.departement',
                        db_table='suivi_pointage_departements',
                    ),
                ),
            ],
        ),
    ]
