"""
Cree la table `prof_type_history` sur une base neuve.

Le modele `ProfTypeHistory` est `managed = False` cote SIGA : sa table avait ete
creee hors Django par un script de backfill. Sur une base IPGEI partant de zero,
elle n'existe donc pas — et le signal post_save de `Prof` l'interroge a chaque
enregistrement, ce qui faisait echouer toute creation d'enseignant.

On la cree ici en SQL (CREATE TABLE IF NOT EXISTS) : le modele reste non-manage,
Django ne tentera jamais de l'alterer, mais un clone frais est autosuffisant.
"""
from django.db import migrations

SQL_POSTGRES = """
CREATE TABLE IF NOT EXISTS prof_type_history (
    id          BIGSERIAL PRIMARY KEY,
    prof_id     BIGINT       NOT NULL REFERENCES prof(id) ON DELETE CASCADE,
    type        VARCHAR(50)  NOT NULL,
    date_debut  DATE         NOT NULL,
    date_fin    DATE         NULL,
    motif       TEXT         NOT NULL DEFAULT '',
    cree_par    VARCHAR(100) NOT NULL DEFAULT '',
    cree_le     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS prof_type_history_prof_id_idx
    ON prof_type_history (prof_id, date_debut DESC);
"""

SQL_MYSQL = """
CREATE TABLE IF NOT EXISTS prof_type_history (
    id          BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    prof_id     BIGINT       NOT NULL,
    type        VARCHAR(50)  NOT NULL,
    date_debut  DATE         NOT NULL,
    date_fin    DATE         NULL,
    motif       LONGTEXT     NOT NULL,
    cree_par    VARCHAR(100) NOT NULL DEFAULT '',
    cree_le     DATETIME(6)  NOT NULL,
    INDEX prof_type_history_prof_id_idx (prof_id, date_debut),
    CONSTRAINT prof_type_history_prof_fk FOREIGN KEY (prof_id)
        REFERENCES prof(id) ON DELETE CASCADE
) ENGINE=InnoDB;
"""


def creer_table(apps, schema_editor):
    sql = SQL_POSTGRES if schema_editor.connection.vendor == 'postgresql' else SQL_MYSQL
    with schema_editor.connection.cursor() as cursor:
        for instruction in filter(None, (s.strip() for s in sql.split(';'))):
            cursor.execute(instruction)


def supprimer_table(apps, schema_editor):
    # Volontairement no-op : la table porte l'historique des statuts, donnee de
    # paie. Un rollback de migration ne doit pas la detruire.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('prof', '0010_alter_prof_type'),
    ]

    operations = [
        migrations.RunPython(creer_table, supprimer_table),
    ]
