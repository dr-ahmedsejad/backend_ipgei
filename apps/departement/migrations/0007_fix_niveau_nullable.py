"""Fix drift schema sur la table legacy `departement`.

Deux ecarts entre l'etat des migrations (= modele) et la colonne physique,
hérités de l'alignement/fake initial de la table legacy. Chacun fait echouer
la creation d'un groupe (POST /api/v1/departements/) -> IntegrityError -> 500 :

1. `niveau_id` etait NOT NULL alors que le modele le declare `null=True`
   (on_delete=SET_NULL). Creer un groupe sans niveau ('Niveau : Aucun' dans
   l'UI) levait 1048 ; supprimer un Niveau reference aurait aussi plante.

2. `decalage` (ancienne colonne, supprimee du modele par
   0005_remove_departement_decalage_and_more au profit de decalage_impair/_pair)
   n'a jamais ete droppee physiquement et reste NOT NULL sans default -> tout
   INSERT (qui ne liste pas cette colonne) levait 1364. La colonne n'est exposee
   nulle part (serializer = fields '__all__' sur le modele, qui ne l'a plus).

Idempotent : chaque operation verifie l'etat reel avant d'agir.
reverse = noop : on ne re-impose pas NOT NULL ni ne recree `decalage`.
"""
from django.db import migrations


def fix_departement_drift(apps, schema_editor):
    conn = schema_editor.connection
    if conn.vendor != 'mysql':
        # Drift legacy propre a gesafped26 (MySQL) : inexistant sur une base
        # neuve (sqlite/PostgreSQL) ou les migrations creent le bon schema.
        return
    with conn.cursor() as cur:
        # 1. niveau_id -> NULL si encore NOT NULL
        cur.execute(
            """
            SELECT IS_NULLABLE
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'departement'
              AND COLUMN_NAME = 'niveau_id'
            """
        )
        row = cur.fetchone()
        if row and row[0] == 'NO':
            # MODIFY conserve la cle etrangere, seule la nullabilite change.
            cur.execute("ALTER TABLE departement MODIFY niveau_id BIGINT NULL")

        # 2. drop colonne orpheline `decalage` si elle existe encore
        cur.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'departement'
              AND COLUMN_NAME = 'decalage'
            """
        )
        if cur.fetchone()[0]:
            cur.execute("ALTER TABLE departement DROP COLUMN decalage")


class Migration(migrations.Migration):

    dependencies = [
        ('departement', '0006_alter_departement_decalage_impair_and_more'),
    ]

    operations = [
        migrations.RunPython(fix_departement_drift, migrations.RunPython.noop),
    ]
