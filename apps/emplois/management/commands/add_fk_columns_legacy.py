"""
Etape DDL : ajoute les colonnes FK manquantes aux 4 tables legacy.

Tables et colonnes a ajouter :
  - emplois_emplois             : prof_id, em_id, salle_id, departement_id, semestre_id, creneau_fk_id
  - emplois_emploisarchive      : idem
  - suivi_suivie                : idem
  - suivi_suivie_pointage       : prof_id, em_id, salle_id, semestre_id, creneau_fk_id
                                  (PAS de departement_id : utilise la M2M suivi_pointage_departements)

Toutes les colonnes sont ajoutees en BIGINT NULL DEFAULT NULL,
SANS contrainte FK pour le moment (la contrainte sera posee
apres backfill et verification — voir add_fk_constraints_legacy).

Idempotent : utilise ALTER TABLE ... ADD COLUMN IF NOT EXISTS (MySQL 8.0.29+).

DDL non-transactionnel en MySQL : chaque ALTER s'auto-commit.
On collecte les operations effectuees pour permettre un rollback manuel
en cas de probleme (DROP COLUMN).

Usage :
    python manage.py add_fk_columns_legacy
    python manage.py add_fk_columns_legacy --dry-run     # liste les ALTER sans les executer
"""
from __future__ import annotations
from django.core.management.base import BaseCommand
from django.db import connection


# (table, [col_name, ...])
FK_COLUMNS_PER_TABLE = {
    'emplois_emplois':         ['prof_id', 'em_id', 'salle_id', 'departement_id', 'semestre_id', 'creneau_fk_id'],
    'emplois_emploisarchive':  ['prof_id', 'em_id', 'salle_id', 'departement_id', 'semestre_id', 'creneau_fk_id'],
    'suivi_suivie':            ['prof_id', 'em_id', 'salle_id', 'departement_id', 'semestre_id', 'creneau_fk_id'],
    'suivi_suivie_pointage':   ['prof_id', 'em_id', 'salle_id',                   'semestre_id', 'creneau_fk_id'],
}


class Command(BaseCommand):
    help = 'Ajoute les colonnes FK (BIGINT NULL) aux 4 tables legacy de emplois/suivi.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='N\'execute pas les ALTER, les liste seulement.')

    def handle(self, *args, **opts):
        cur = connection.cursor()
        dry_run = opts['dry_run']

        # Recuperer les colonnes deja presentes par table
        existing = {}
        for t in FK_COLUMNS_PER_TABLE:
            cur.execute("""
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            """, [t])
            existing[t] = {r[0] for r in cur.fetchall()}

        # Generer et appliquer les ALTER
        applied = []
        skipped = []
        for table, cols in FK_COLUMNS_PER_TABLE.items():
            for col in cols:
                if col in existing[table]:
                    skipped.append((table, col))
                    continue
                sql = f'ALTER TABLE `{table}` ADD COLUMN `{col}` BIGINT NULL DEFAULT NULL'
                self.stdout.write(f'  {sql}')
                if not dry_run:
                    cur.execute(sql)
                applied.append((table, col))

        # Index sur les nouvelles colonnes (acceleration backfill + JOIN)
        index_ops = []
        for table, cols in FK_COLUMNS_PER_TABLE.items():
            for col in cols:
                if col in existing[table]:
                    continue
                idx_name = f'idx_{table}_{col}'
                # Verifier que l'index n'existe pas deja
                cur.execute("""
                    SELECT COUNT(*) FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s
                """, [table, idx_name])
                if cur.fetchone()[0] > 0:
                    continue
                sql = f'CREATE INDEX `{idx_name}` ON `{table}`(`{col}`)'
                self.stdout.write(f'  {sql}')
                if not dry_run:
                    cur.execute(sql)
                index_ops.append((table, col, idx_name))

        # Recap
        self.stdout.write(self.style.MIGRATE_HEADING('\n=== Recap ==='))
        self.stdout.write(f'  Colonnes ajoutees : {len(applied)}')
        self.stdout.write(f'  Colonnes deja presentes : {len(skipped)}')
        self.stdout.write(f'  Index crees : {len(index_ops)}')
        for t, c in skipped:
            self.stdout.write(f'    deja: {t}.{c}')

        if dry_run:
            self.stdout.write(self.style.WARNING('\n  DRY-RUN : aucun ALTER execute.'))
        else:
            self.stdout.write(self.style.SUCCESS('\nOK Schema mis a jour. Etape suivante : backfill_legacy_fks.'))
