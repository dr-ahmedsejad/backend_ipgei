"""
Applique le backfill de la table M2M `suivi_pointage_departements`
en preservant 100 % des liens departement de suivi_suivie_pointage.

OPERATIONS (idempotentes, dans une transaction unique) :
  1. CREATE TABLE IF NOT EXISTS suivi_pointage_departements
  2. INSERT IGNORE des paires (suiviepointage_id, departement_id) calculees
     a partir de id_departement (split sur '/')
  3. Verification :
       - count_inserted == count_attendu (precheck)
       - chaque ligne pointage avec id_departement non vide a >= 1 entree M2M
  4. ROLLBACK en cas d'incoherence.

Le CharField `id_departement` est CONSERVE (pas touche). Il pourra etre
supprime plus tard, apres validation de la phase 4.

Usage :
    python manage.py apply_pointage_m2m_backfill          # ecrit
    python manage.py apply_pointage_m2m_backfill --dry-run # simule (rollback final)
"""
from __future__ import annotations
from django.core.management.base import BaseCommand
from django.db import connection, transaction


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS suivi_pointage_departements (
    id                 BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    suiviepointage_id  BIGINT NOT NULL,
    departement_id     BIGINT NOT NULL,
    UNIQUE KEY uk_pointage_dept (suiviepointage_id, departement_id),
    KEY idx_dept (departement_id),
    CONSTRAINT fk_spd_pointage    FOREIGN KEY (suiviepointage_id) REFERENCES suivi_suivie_pointage(id) ON DELETE CASCADE,
    CONSTRAINT fk_spd_departement FOREIGN KEY (departement_id)    REFERENCES departement(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# Backfill : pour chaque pointage, splitter id_departement sur '/' et resoudre
# chaque token vers departement.nom + annee_universitaire.
BACKFILL_SQL = """
INSERT IGNORE INTO suivi_pointage_departements (suiviepointage_id, departement_id)
SELECT DISTINCT sp.id, d.id
FROM suivi_suivie_pointage sp
JOIN departement d
  ON d.annee_universitaire = sp.annee_universitaire
 AND FIND_IN_SET(d.nom, REPLACE(sp.id_departement, '/', ',')) > 0
WHERE sp.id_departement IS NOT NULL AND sp.id_departement != ''
"""


class Command(BaseCommand):
    help = 'Applique le backfill M2M suivi_pointage_departements (CharField -> M2M).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Simule le backfill (rollback final), aucune ecriture persistee.')

    def handle(self, *args, **opts):
        dry_run = opts['dry_run']
        cur = connection.cursor()

        # ── 0. Etat initial ─────────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING('=== Etat initial ==='))
        cur.execute("SELECT COUNT(*) FROM suivi_suivie_pointage WHERE id_departement IS NOT NULL AND id_departement != ''")
        n_pointage_with_dept = cur.fetchone()[0]
        self.stdout.write(f'  Lignes pointage avec id_departement non vide : {n_pointage_with_dept}')

        # ── 1. Verifier si la table existe deja ─────────────────────────
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'suivi_pointage_departements'
        """)
        table_exists = cur.fetchone()[0] > 0

        n_existing_pairs = 0
        if table_exists:
            cur.execute("SELECT COUNT(*) FROM suivi_pointage_departements")
            n_existing_pairs = cur.fetchone()[0]
            self.stdout.write(f'  Table M2M existe deja, {n_existing_pairs} paires presentes.')
        else:
            self.stdout.write('  Table M2M absente, sera creee.')

        # ── 2. Calculer le nombre de paires attendu ─────────────────────
        cur.execute("""
            SELECT COUNT(DISTINCT CONCAT(sp.id, '|', d.id))
            FROM suivi_suivie_pointage sp
            JOIN departement d
              ON d.annee_universitaire = sp.annee_universitaire
             AND FIND_IN_SET(d.nom, REPLACE(sp.id_departement, '/', ',')) > 0
            WHERE sp.id_departement IS NOT NULL AND sp.id_departement != ''
        """)
        n_expected = cur.fetchone()[0]
        self.stdout.write(f'  Paires M2M attendues : {n_expected}')

        # ── 3. Operations transactionnelles ─────────────────────────────
        try:
            with transaction.atomic():
                # 3a. Creer la table si necessaire
                if not table_exists:
                    self.stdout.write('  -> CREATE TABLE suivi_pointage_departements')
                    cur.execute(CREATE_TABLE_SQL)

                # 3b. Backfill (INSERT IGNORE -> idempotent)
                self.stdout.write('  -> INSERT IGNORE backfill')
                cur.execute(BACKFILL_SQL)
                n_inserted = cur.rowcount
                self.stdout.write(f'     {n_inserted} lignes inserees.')

                # 3c. Verifier le total
                cur.execute("SELECT COUNT(*) FROM suivi_pointage_departements")
                n_after = cur.fetchone()[0]
                self.stdout.write(f'  Total paires apres backfill : {n_after}')

                if n_after != n_expected:
                    raise RuntimeError(
                        f'INCOHERENCE : total apres backfill ({n_after}) != attendu ({n_expected}). '
                        'Rollback automatique.'
                    )

                # 3d. Verifier qu'aucune ligne pointage avec id_departement non vide
                #     ne se retrouve sans entree M2M (= aucune perte)
                cur.execute("""
                    SELECT COUNT(*) FROM suivi_suivie_pointage sp
                    WHERE sp.id_departement IS NOT NULL AND sp.id_departement != ''
                      AND NOT EXISTS (
                        SELECT 1 FROM suivi_pointage_departements spd
                        WHERE spd.suiviepointage_id = sp.id
                      )
                """)
                n_orphan = cur.fetchone()[0]
                self.stdout.write(f'  Lignes pointage orphelines apres backfill : {n_orphan}')

                if n_orphan > 0:
                    raise RuntimeError(
                        f'PERTE DE DONNEES : {n_orphan} lignes pointage sans entree M2M. '
                        'Rollback automatique.'
                    )

                # 3e. Stats : repartition single vs multi
                cur.execute("""
                    SELECT cnt, COUNT(*) FROM (
                        SELECT suiviepointage_id, COUNT(*) AS cnt
                        FROM suivi_pointage_departements
                        GROUP BY suiviepointage_id
                    ) x GROUP BY cnt ORDER BY cnt
                """)
                self.stdout.write('  Distribution depts/pointage :')
                for cnt, n in cur.fetchall():
                    self.stdout.write(f'    {cnt} dept(s) -> {n} pointages')

                if dry_run:
                    self.stdout.write(self.style.WARNING('\n  DRY-RUN : rollback final.'))
                    raise _DryRunRollback()

            self.stdout.write(self.style.SUCCESS('\nOK Backfill applique et commit.'))

        except _DryRunRollback:
            self.stdout.write(self.style.SUCCESS('\nOK Dry-run OK, transaction annulee. Aucune modification persistee.'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'\nERREUR : {e}'))
            self.stdout.write(self.style.WARNING('Toutes les operations ont ete annulees (rollback).'))
            raise


class _DryRunRollback(Exception):
    pass
