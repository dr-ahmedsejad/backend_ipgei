"""
Migration 0004 : supprime le champ ManyToMany `departements` de l'état Django.

La table suivi_pointage_departements a été créée par GesAFPED avec la colonne
`suivie_pointage_id` (modèle GesAFPED : Suivie_pointage), mais Django dans siga
cherche `suiviepointage_id` (modèle : SuiviePointage sans underscore) → erreur SQL.

On retire ce champ du modèle Django ; on utilise à la place le CharField
`id_departement` qui stocke les noms séparés par '/'.
=> Sur MySQL : zéro SQL exécuté. Sur base neuve non-MySQL : la table through
auto-créée par 0001 est réellement supprimée (elle sera remplacée plus tard
par la table unmanaged suivi_pointage_departements, cf. 0008).
"""
from django.db import migrations

from core.migration_ops import DejaAppliqueeSurMySQL


class Migration(migrations.Migration):

    dependencies = [
        ('suivi', '0003_suiviepointage_missing_fields'),
    ]

    operations = [
        DejaAppliqueeSurMySQL(
            database_operations=[],   # ne rien toucher en DB MySQL
            state_operations=[
                migrations.RemoveField(
                    model_name='suiviepointage',
                    name='departements',
                ),
            ],
        ),
    ]
