"""
Migration 0005 : crée physiquement la table suivi_chargeinstitution.

Cette table n'existe pas dans GesAFPED (nouveau modèle SIGA uniquement).
La migration 0001 avait été appliquée en --fake sur MySQL, donc le CREATE TABLE
n'avait jamais été exécuté — d'où le RunSQL MySQL ci-dessous.

Sur toute base neuve non-MySQL (sqlite de test, PostgreSQL), le CreateModel de
suivi/0001 crée déjà réellement la table : cette migration est alors un no-op
(un CREATE TABLE ici ferait doublon et échouerait).
"""
from django.db import migrations


SQL_MYSQL = """
                CREATE TABLE IF NOT EXISTS `suivi_chargeinstitution` (
                    `id`                  bigint NOT NULL AUTO_INCREMENT,
                    `charge_cm`           int NOT NULL DEFAULT 0,
                    `annee_universitaire` varchar(20) NOT NULL,
                    `institution_id`      bigint NOT NULL,
                    `prof_id`             bigint NOT NULL,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `suivi_chargeinstitution_prof_id_institution_id_annee_uniq`
                        (`prof_id`, `institution_id`, `annee_universitaire`),
                    CONSTRAINT `fk_charge_institution`
                        FOREIGN KEY (`institution_id`) REFERENCES `institution` (`id`)
                        ON DELETE CASCADE,
                    CONSTRAINT `fk_charge_prof`
                        FOREIGN KEY (`prof_id`) REFERENCES `prof` (`id`)
                        ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """


def creer_table_mysql(apps, schema_editor):
    """MySQL uniquement : rattrapage du 0001 appliqué en --fake."""
    if schema_editor.connection.vendor != 'mysql':
        return  # base neuve : table déjà créée par suivi/0001
    schema_editor.execute(SQL_MYSQL)


def supprimer_table_mysql(apps, schema_editor):
    if schema_editor.connection.vendor != 'mysql':
        return
    schema_editor.execute("DROP TABLE IF EXISTS `suivi_chargeinstitution`;")


class Migration(migrations.Migration):

    dependencies = [
        ('suivi',       '0004_remove_suiviepointage_departements_m2m'),
        ('prof',        '0001_initial'),
        ('parametres',  '0001_initial'),
    ]

    operations = [
        migrations.RunPython(creer_table_mysql, supprimer_table_mysql),
    ]
