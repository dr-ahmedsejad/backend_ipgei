"""Fix drift moteur : convertit les tables legacy MyISAM en InnoDB.

Symptome : l'import de notes echouait au "Recalcul global" avec
    (1452) Cannot add or update a child row: a foreign key constraint fails
        (evaluations_resultat_module.module_id -> modules_module.id)
alors meme que le module existait bien dans modules_module.

Cause : `modules_module` (et 11 autres tables) etaient restees en moteur
**MyISAM** (heritage du schema legacy gesafped26), tandis que les tables
enfants (evaluations_resultat_module, etc.) sont en InnoDB avec des cles
etrangeres vers elles. Or **une FK InnoDB ne peut pas referencer une table
MyISAM** : InnoDB ne "voit" aucune ligne du parent MyISAM -> tout INSERT dans
l'enfant echoue en 1452, quel que soit l'id.

Correctif : convertir toutes les tables MyISAM de la base en InnoDB. Balayage
global (pas une liste figee) pour rattraper toute table legacy oubliee.

Idempotent : ne convertit que ce qui est encore MyISAM ; ne fait rien si tout
est deja InnoDB. reverse = noop (on ne revient jamais a MyISAM). MySQL only.
"""
from django.db import migrations


def myisam_to_innodb(apps, schema_editor):
    conn = schema_editor.connection
    if conn.vendor != 'mysql':
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND engine = 'MyISAM'
              AND table_type = 'BASE TABLE'
            """
        )
        tables = [row[0] for row in cur.fetchall()]
        if not tables:
            return
        # FK checks off : on convertit parents et enfants sans ordre impose.
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for name in tables:
            # name vient d'information_schema (pas d'entree utilisateur) ; backquote par surete.
            cur.execute("ALTER TABLE `%s` ENGINE=InnoDB" % name.replace('`', ''))
        cur.execute("SET FOREIGN_KEY_CHECKS=1")


class Migration(migrations.Migration):

    # DDL MySQL = commit implicite : pas d'atomicite reelle, on l'explicite.
    atomic = False

    dependencies = [
        ('modules', '0003_module_institution_backfill'),
    ]

    operations = [
        migrations.RunPython(myisam_to_innodb, migrations.RunPython.noop),
    ]
