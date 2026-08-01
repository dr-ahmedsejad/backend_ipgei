"""
Cree la table `suivi_pointage_departements` sur une base neuve.

Meme situation que `prof_type_history` : le through-model
`SuiviePointageDepartement` est `managed = False` et sa table avait ete creee
hors Django cote SIGA (cf. migration 0008, `SeparateDatabaseAndState` avec
`database_operations=[]`). Sur une base IPGEI partant de zero, elle n'existe
donc jamais.

Consequence concrete : toute requete touchant `SuiviePointage.departements`
echoue — y compris la verification en cascade lors de la suppression d'un
`Departement`. Le module Suivi etait inutilisable.

On la cree ici en SQL ; le modele reste non-manage, Django ne tentera jamais de
l'alterer, mais un clone frais devient autosuffisant.
"""
from django.db import migrations

SQL_POSTGRES = """
CREATE TABLE IF NOT EXISTS suivi_pointage_departements (
    id                BIGSERIAL PRIMARY KEY,
    suiviepointage_id BIGINT NOT NULL
        REFERENCES suivi_suivie_pointage(id) ON DELETE CASCADE,
    departement_id    BIGINT NOT NULL
        REFERENCES departement(id) ON DELETE RESTRICT,
    CONSTRAINT suivi_pointage_dept_uniq UNIQUE (suiviepointage_id, departement_id)
);
CREATE INDEX IF NOT EXISTS suivi_pointage_dept_pointage_idx
    ON suivi_pointage_departements (suiviepointage_id);
CREATE INDEX IF NOT EXISTS suivi_pointage_dept_dept_idx
    ON suivi_pointage_departements (departement_id);
"""

SQL_MYSQL = """
CREATE TABLE IF NOT EXISTS suivi_pointage_departements (
    id                BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    suiviepointage_id BIGINT NOT NULL,
    departement_id    BIGINT NOT NULL,
    UNIQUE KEY suivi_pointage_dept_uniq (suiviepointage_id, departement_id),
    KEY suivi_pointage_dept_dept_idx (departement_id),
    CONSTRAINT suivi_pointage_dept_fk1 FOREIGN KEY (suiviepointage_id)
        REFERENCES suivi_suivie_pointage(id) ON DELETE CASCADE,
    CONSTRAINT suivi_pointage_dept_fk2 FOREIGN KEY (departement_id)
        REFERENCES departement(id)
) ENGINE=InnoDB;
"""


def creer_table(apps, schema_editor):
    sql = SQL_POSTGRES if schema_editor.connection.vendor == 'postgresql' else SQL_MYSQL
    with schema_editor.connection.cursor() as cursor:
        for instruction in filter(None, (s.strip() for s in sql.split(';'))):
            cursor.execute(instruction)


def supprimer_table(apps, schema_editor):
    # No-op volontaire : la table porte le rattachement multi-departements des
    # pointages, donnee de suivi. Un rollback ne doit pas la vider.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('suivi', '0014_fix_duree_creneau'),
        ('departement', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(creer_table, supprimer_table),
    ]
