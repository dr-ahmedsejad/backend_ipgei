"""
Phase 5 (state-only) : aligne le state Django avec les modeles refactores
qui n'ont plus les CharField legacy + introduit les FK type_seance_fk + jour_fk.

DDL deja applique manuellement :
  - Colonnes type_seance_fk_id, jour_fk_id ajoutees + backfillees + FK posees
    via management command `add_seance_jour_fk`.
  - Suppression des CharField : NON ENCORE faite (Phase A.8 = drop_legacy_charfields).

=> Sur MySQL : database_operations=[] : zero SQL execute.
   Apres `python manage.py migrate suivi 0009 --fake`, l'etat Django sera
   coherent avec les modeles ; la DB conservera les CharField legacy en
   attendant le drop final.
   Sur base neuve non-MySQL : drop des CharField legacy + ajout des FK
   executes reellement.
"""
from django.db import migrations, models
import django.db.models.deletion

from core.migration_ops import DejaAppliqueeSurMySQL


class Migration(migrations.Migration):

    dependencies = [
        ('suivi', '0008_add_suiviepointage_departements_m2m'),
        ('parametres', '0006_semestre_simplification'),
    ]

    operations = [
        DejaAppliqueeSurMySQL(
            database_operations=[],
            state_operations=[
                # ── Suivie ───────────────────────────────────────────────────
                migrations.RemoveField(model_name='suivie', name='id_prof'),
                migrations.RemoveField(model_name='suivie', name='id_em'),
                migrations.RemoveField(model_name='suivie', name='id_salle'),
                migrations.RemoveField(model_name='suivie', name='id_departement'),
                migrations.RemoveField(model_name='suivie', name='id_semestre'),
                migrations.RemoveField(model_name='suivie', name='creneau'),
                migrations.RemoveField(model_name='suivie', name='type_seance'),
                migrations.RemoveField(model_name='suivie', name='jour'),
                migrations.AddField(
                    model_name='suivie', name='type_seance_fk',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='suivies', to='parametres.seance',
                    ),
                ),
                migrations.AddField(
                    model_name='suivie', name='jour_fk',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='suivies', to='parametres.jour',
                    ),
                ),
                # ── SuiviePointage ────────────────────────────────────────────
                migrations.RemoveField(model_name='suiviepointage', name='id_prof'),
                migrations.RemoveField(model_name='suiviepointage', name='id_em'),
                migrations.RemoveField(model_name='suiviepointage', name='id_salle'),
                migrations.RemoveField(model_name='suiviepointage', name='id_departement'),
                migrations.RemoveField(model_name='suiviepointage', name='id_semestre'),
                migrations.RemoveField(model_name='suiviepointage', name='creneau'),
                migrations.RemoveField(model_name='suiviepointage', name='type_seance'),
                migrations.RemoveField(model_name='suiviepointage', name='jour'),
                migrations.AddField(
                    model_name='suiviepointage', name='type_seance_fk',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='+', to='parametres.seance',
                    ),
                ),
                migrations.AddField(
                    model_name='suiviepointage', name='jour_fk',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='+', to='parametres.jour',
                    ),
                ),
            ],
        ),
    ]
