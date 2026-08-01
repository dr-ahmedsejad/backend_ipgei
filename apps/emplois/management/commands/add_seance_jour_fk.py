"""
Phase A.1 : ajoute les colonnes FK type_seance_fk_id et jour_fk_id sur les
4 tables emplois/suivi, backfill depuis les CharField legacy, ajoute les
contraintes FK ON DELETE SET NULL.

Tables :
  emplois_emplois, emplois_emploisarchive, suivi_suivie, suivi_suivie_pointage

Pour chaque table :
  - ALTER TABLE ADD COLUMN type_seance_fk_id BIGINT NULL
  - ALTER TABLE ADD COLUMN jour_fk_id        BIGINT NULL
  - INDEX sur les 2 colonnes
  - UPDATE backfill (type_seance numerique -> seance.id ; jour libelle -> jour.id)
  - ALTER TABLE ADD CONSTRAINT FK ... ON DELETE SET NULL

Idempotent. Verification post-backfill : 0 ligne avec CharField rempli + FK NULL,
sinon RuntimeError + rollback transaction.

Usage :
    python manage.py add_seance_jour_fk
    python manage.py add_seance_jour_fk --dry-run
"""
from __future__ import annotations
from django.core.management.base import BaseCommand
from django.db import connection, transaction


TABLES = [
    'emplois_emplois',
    'emplois_emploisarchive',
    'suivi_suivie',
    'suivi_suivie_pointage',
]


class Command(BaseCommand):
    help = 'Ajoute type_seance_fk_id + jour_fk_id sur les 4 tables emplois/suivi avec backfill.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Simule (rollback final).')

    def handle(self, *args, **opts):
        cur = connection.cursor()
        dry_run = opts['dry_run']

        # 1) DDL : ajouter les colonnes (non-transactionnel en MySQL, fait hors atomic)
        for tbl in TABLES:
            self._add_column_if_missing(cur, tbl, 'type_seance_fk_id', 'BIGINT NULL DEFAULT NULL')
            self._add_column_if_missing(cur, tbl, 'jour_fk_id',        'BIGINT NULL DEFAULT NULL')
            self._create_index_if_missing(cur, tbl, f'idx_{tbl}_type_seance_fk', 'type_seance_fk_id')
            self._create_index_if_missing(cur, tbl, f'idx_{tbl}_jour_fk',        'jour_fk_id')

        # 2) Backfill (transactionnel)
        try:
            with transaction.atomic():
                for tbl in TABLES:
                    self.stdout.write(self.style.MIGRATE_HEADING(f'\n=== Backfill {tbl} ==='))

                    cur.execute(f"SELECT COUNT(*) FROM `{tbl}`")
                    total = cur.fetchone()[0]
                    self.stdout.write(f'  Total lignes : {total}')
                    if total == 0:
                        continue

                    # type_seance : numerique -> seance.id
                    cur.execute(f"""
                        UPDATE `{tbl}` t
                        JOIN Seance s ON s.id = CAST(t.type_seance AS UNSIGNED)
                        SET t.type_seance_fk_id = s.id
                        WHERE t.type_seance_fk_id IS NULL
                          AND t.type_seance IS NOT NULL
                          AND t.type_seance != ''
                          AND t.type_seance REGEXP '^[0-9]+$'
                    """)
                    n_ts_num = cur.rowcount

                    # type_seance : label (CM/TD/...) -> seance.id
                    cur.execute(f"""
                        UPDATE `{tbl}` t
                        JOIN Seance s ON s.type_seance = t.type_seance
                        SET t.type_seance_fk_id = s.id
                        WHERE t.type_seance_fk_id IS NULL
                          AND t.type_seance IS NOT NULL
                          AND t.type_seance != ''
                          AND t.type_seance NOT REGEXP '^[0-9]+$'
                    """)
                    n_ts_lbl = cur.rowcount
                    self.stdout.write(f'  type_seance : {n_ts_num} num + {n_ts_lbl} label = {n_ts_num + n_ts_lbl}')

                    # jour : libelle -> jour.id
                    cur.execute(f"""
                        UPDATE `{tbl}` t
                        JOIN jour j ON j.jour = t.jour
                        SET t.jour_fk_id = j.id
                        WHERE t.jour_fk_id IS NULL
                          AND t.jour IS NOT NULL
                          AND t.jour != ''
                    """)
                    n_jour = cur.rowcount
                    self.stdout.write(f'  jour        : {n_jour} updates')

                    # Verification : 0 ligne avec CharField rempli + FK NULL
                    cur.execute(f"""
                        SELECT COUNT(*) FROM `{tbl}`
                        WHERE type_seance IS NOT NULL AND type_seance != '' AND type_seance_fk_id IS NULL
                    """)
                    orphans_ts = cur.fetchone()[0]
                    cur.execute(f"""
                        SELECT COUNT(*) FROM `{tbl}`
                        WHERE jour IS NOT NULL AND jour != '' AND jour_fk_id IS NULL
                    """)
                    orphans_jour = cur.fetchone()[0]

                    self.stdout.write(f'  Verif       : type_seance orphelins={orphans_ts}, jour orphelins={orphans_jour}')
                    if orphans_ts > 0 or orphans_jour > 0:
                        raise RuntimeError(
                            f'Backfill incomplet sur {tbl} : '
                            f'type_seance orphelins={orphans_ts}, jour orphelins={orphans_jour}'
                        )

                if dry_run:
                    self.stdout.write(self.style.WARNING('\n  DRY-RUN : rollback final.'))
                    raise _DryRunRollback()

            self.stdout.write(self.style.SUCCESS('\nOK Backfill applique et commit.'))

        except _DryRunRollback:
            self.stdout.write(self.style.SUCCESS('\nOK Dry-run termine, aucune modification persistee.'))
            return
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'\nERREUR : {e}'))
            self.stdout.write(self.style.WARNING('Toutes les operations ont ete annulees (rollback).'))
            raise

        # 3) Contraintes FK (apres backfill, hors transaction MySQL DDL)
        if not dry_run:
            self.stdout.write(self.style.MIGRATE_HEADING('\n=== Ajout contraintes FK ==='))
            for tbl in TABLES:
                self._add_fk_constraint(cur, tbl, 'type_seance_fk_id', 'Seance', f'fk_{tbl[:30]}_ts')
                self._add_fk_constraint(cur, tbl, 'jour_fk_id',        'jour',   f'fk_{tbl[:30]}_jr')

    # ────────────────────────────────────────────────────────────────────
    def _add_column_if_missing(self, cur, table, col, type_def):
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """, [table, col])
        if cur.fetchone()[0] > 0:
            self.stdout.write(f'  [skip] {table}.{col} existe deja')
            return
        sql = f'ALTER TABLE `{table}` ADD COLUMN `{col}` {type_def}'
        self.stdout.write(f'  {sql}')
        cur.execute(sql)

    def _create_index_if_missing(self, cur, table, idx_name, col):
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s
        """, [table, idx_name])
        if cur.fetchone()[0] > 0:
            return
        cur.execute(f'CREATE INDEX `{idx_name}` ON `{table}`(`{col}`)')

    def _add_fk_constraint(self, cur, table, col, ref_table, cstr_name):
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND CONSTRAINT_NAME = %s
        """, [table, cstr_name])
        if cur.fetchone()[0] > 0:
            self.stdout.write(f'  [skip] {table}.{cstr_name} existe deja')
            return
        sql = (
            f'ALTER TABLE `{table}` '
            f'ADD CONSTRAINT `{cstr_name}` '
            f'FOREIGN KEY (`{col}`) REFERENCES `{ref_table}`(`id`) '
            f'ON DELETE SET NULL ON UPDATE CASCADE'
        )
        self.stdout.write(f'  {sql}')
        try:
            cur.execute(sql)
        except Exception as e:
            self.stdout.write(self.style.WARNING(f'    SKIP : {e}'))


class _DryRunRollback(Exception):
    pass
