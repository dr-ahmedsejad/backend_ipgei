"""
State-only : aligne le state Django avec semaine.jour -> FK Jour.

DDL deja applique manuellement (Sprint 3) sur MySQL :
  - Colonne jour_fk_id ajoutee + backfillee + FK posee
  - Colonne `jour` (CharField legacy) pas encore drop (pourra l'etre apres validation)
Sur base neuve non-MySQL : drop `jour` + ajout `jour_fk` executes reellement.
"""
from django.db import migrations, models
import django.db.models.deletion

from core.migration_ops import DejaAppliqueeSurMySQL


class Migration(migrations.Migration):

    dependencies = [
        ('parametres', '0006_semestre_simplification'),
    ]

    operations = [
        DejaAppliqueeSurMySQL(
            database_operations=[],
            state_operations=[
                migrations.RemoveField(model_name='semaine', name='jour'),
                migrations.AddField(
                    model_name='semaine', name='jour_fk',
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name='semaines', to='parametres.jour',
                    ),
                ),
            ],
        ),
    ]
