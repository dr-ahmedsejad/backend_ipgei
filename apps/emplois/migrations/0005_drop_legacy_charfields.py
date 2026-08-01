"""
Phase 5 (state-only) : aligne le state Django avec les modeles refactores
sans CharField legacy + introduit les FK type_seance_fk + jour_fk.

Voir suivi/0009_drop_legacy_charfields.py pour les details.
Sur base neuve non-MySQL : drop des CharField legacy + ajout des FK
executes reellement.
"""
from django.db import migrations, models
import django.db.models.deletion

from core.migration_ops import DejaAppliqueeSurMySQL


class Migration(migrations.Migration):

    dependencies = [
        ('emplois', '0004_add_institution_fk'),
        ('parametres', '0006_semestre_simplification'),
    ]

    operations = [
        DejaAppliqueeSurMySQL(
            database_operations=[],
            state_operations=[
                # ── Emplois ──────────────────────────────────────────────────
                # RemoveIndex deplace ici depuis 0006 : l'index (cree en 0003
                # sur le CharField `jour`) doit sortir du state AVANT le
                # RemoveField(jour), sinon la recreation de table sqlite le
                # regenere et le DROP COLUMN echoue. Etat final inchange
                # (0006 ne le retire plus). Sur MySQL : toujours zero SQL.
                migrations.RemoveIndex(
                    model_name='emplois',
                    name='emplois_annee_jour_creneau_idx',
                ),
                migrations.RemoveField(model_name='emplois', name='id_prof'),
                migrations.RemoveField(model_name='emplois', name='id_em'),
                migrations.RemoveField(model_name='emplois', name='id_salle'),
                migrations.RemoveField(model_name='emplois', name='id_departement'),
                migrations.RemoveField(model_name='emplois', name='id_semestre'),
                migrations.RemoveField(model_name='emplois', name='creneau'),
                migrations.RemoveField(model_name='emplois', name='type_seance'),
                migrations.RemoveField(model_name='emplois', name='jour'),
                migrations.AddField(
                    model_name='emplois', name='type_seance_fk',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='emplois', to='parametres.seance',
                    ),
                ),
                migrations.AddField(
                    model_name='emplois', name='jour_fk',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='emplois', to='parametres.jour',
                    ),
                ),
                # ── EmploisArchive ───────────────────────────────────────────
                migrations.RemoveField(model_name='emploisarchive', name='id_prof'),
                migrations.RemoveField(model_name='emploisarchive', name='id_em'),
                migrations.RemoveField(model_name='emploisarchive', name='id_salle'),
                migrations.RemoveField(model_name='emploisarchive', name='id_departement'),
                migrations.RemoveField(model_name='emploisarchive', name='id_semestre'),
                migrations.RemoveField(model_name='emploisarchive', name='creneau'),
                migrations.RemoveField(model_name='emploisarchive', name='type_seance'),
                migrations.RemoveField(model_name='emploisarchive', name='jour'),
                migrations.AddField(
                    model_name='emploisarchive', name='type_seance_fk',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='+', to='parametres.seance',
                    ),
                ),
                migrations.AddField(
                    model_name='emploisarchive', name='jour_fk',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='+', to='parametres.jour',
                    ),
                ),
            ],
        ),
    ]
