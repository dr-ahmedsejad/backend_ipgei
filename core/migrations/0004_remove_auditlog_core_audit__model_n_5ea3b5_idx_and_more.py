"""
State-only : aligne le state Django avec le schema reel.

Reconciliations :
- L'index auto-genere sur auditlog (model_name, object_id) avait un nom auto
  Django remplace par l'index nomme idx_audit_model_obj_ts dans 0002.
  RemoveIndex ici : noop cote DB (l'index n'existe pas avec ce nom auto).
- L'index user_id sur auditlogarchive a ete renomme par auto-detection.
  RenameIndex : noop cote DB (l'index existant est deja sous le nom correct).
- AlterField user sur auditlog : noop (la colonne FK existe deja).

A appliquer en --fake sur MySQL. Sur base neuve non-MySQL, les operations
sont executees reellement (les index existent alors sous leurs noms d'origine).
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from core.migration_ops import DejaAppliqueeSurMySQL


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_audit_log_archive'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        DejaAppliqueeSurMySQL(
            database_operations=[],
            state_operations=[
                migrations.RemoveIndex(
                    model_name='auditlog',
                    name='core_audit__model_n_5ea3b5_idx',
                ),
                migrations.RenameIndex(
                    model_name='auditlogarchive',
                    new_name='core_audit__user_id_c17241_idx',
                    old_name='core_auditl_user_id_archive_idx',
                ),
                migrations.AlterField(
                    model_name='auditlog',
                    name='user',
                    field=models.ForeignKey(
                        blank=True, null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='+', to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
    ]
