"""
Triggers pour rendre la table backup_download_log strictement
APPEND-ONLY : aucun UPDATE ni DELETE possible, meme par root.

Une fois un telechargement loggé, il est inalterable. Source de verite
pour audit / forensics.

Vendor-aware :
  - MySQL      : triggers SIGNAL SQLSTATE '45000' (SQL historique inchange).
  - PostgreSQL : fonction plpgsql RAISE EXCEPTION + 2 triggers BEFORE
                 UPDATE/DELETE (meme semantique append-only).
  - sqlite     : no-op (base de test jetable, pas de besoin forensics).
"""
from django.db import migrations


# ── MySQL (SQL historique, octet pour octet identique) ─────────────────────
SQL_UP_MYSQL = [
    # Trigger anti-UPDATE
    """
    CREATE TRIGGER backup_download_log_no_update
    BEFORE UPDATE ON backup_download_log
    FOR EACH ROW
    BEGIN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'backup_download_log est append-only : UPDATE interdit';
    END
    """,
    # Trigger anti-DELETE
    """
    CREATE TRIGGER backup_download_log_no_delete
    BEFORE DELETE ON backup_download_log
    FOR EACH ROW
    BEGIN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'backup_download_log est append-only : DELETE interdit';
    END
    """,
]

SQL_DOWN_MYSQL = [
    "DROP TRIGGER IF EXISTS backup_download_log_no_update",
    "DROP TRIGGER IF EXISTS backup_download_log_no_delete",
]

# ── PostgreSQL (meme semantique append-only) ────────────────────────────────
SQL_UP_PG = [
    """
    CREATE OR REPLACE FUNCTION backup_download_log_append_only()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'UPDATE' THEN
            RAISE EXCEPTION 'backup_download_log est append-only : UPDATE interdit';
        ELSIF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'backup_download_log est append-only : DELETE interdit';
        END IF;
        RETURN NULL;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE TRIGGER backup_download_log_no_update
    BEFORE UPDATE ON backup_download_log
    FOR EACH ROW EXECUTE FUNCTION backup_download_log_append_only()
    """,
    """
    CREATE TRIGGER backup_download_log_no_delete
    BEFORE DELETE ON backup_download_log
    FOR EACH ROW EXECUTE FUNCTION backup_download_log_append_only()
    """,
]

SQL_DOWN_PG = [
    "DROP TRIGGER IF EXISTS backup_download_log_no_update ON backup_download_log",
    "DROP TRIGGER IF EXISTS backup_download_log_no_delete ON backup_download_log",
    "DROP FUNCTION IF EXISTS backup_download_log_append_only()",
]


def creer_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'mysql':
        for sql in SQL_UP_MYSQL:
            schema_editor.execute(sql)
    elif vendor == 'postgresql':
        for sql in SQL_UP_PG:
            schema_editor.execute(sql)
    # sqlite : no-op


def supprimer_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == 'mysql':
        for sql in SQL_DOWN_MYSQL:
            schema_editor.execute(sql)
    elif vendor == 'postgresql':
        for sql in SQL_DOWN_PG:
            schema_editor.execute(sql)
    # sqlite : no-op


class Migration(migrations.Migration):

    dependencies = [
        ('backup', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(creer_triggers, supprimer_triggers),
    ]
