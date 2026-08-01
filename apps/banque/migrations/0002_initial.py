# Corrige : la table 'banque' existe deja avec le bon nom en DB (prod MySQL).
# Sur MySQL : etat Django seulement, zero SQL. Sur base neuve non-MySQL :
# le RENAME banque_banque -> banque est execute reellement.
from django.db import migrations

from core.migration_ops import DejaAppliqueeSurMySQL


class Migration(migrations.Migration):

    dependencies = [
        ('banque', '0001_initial'),
    ]

    operations = [
        DejaAppliqueeSurMySQL(
            state_operations=[
                migrations.AlterModelTable(name='banque', table='banque'),
            ],
            database_operations=[],
        ),
    ]
