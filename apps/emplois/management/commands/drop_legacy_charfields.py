"""
Phase A.8 (IRREVERSIBLE) : drop des CharField legacy sur les 4 tables
emplois/suivi.

Tables et colonnes a supprimer :
  emplois_emplois, emplois_emploisarchive, suivi_suivie, suivi_suivie_pointage
    -> id_prof, id_em, id_salle, id_departement, id_semestre,
       creneau, type_seance, jour

Pre-requis :
  - Phase A.1 a A.7 completes (FK + M2M + state Django alignes)
  - Tests E2E valides en dev
  - 1+ jour de stabilite en pre-prod (recommande)

CETTE COMMANDE NE PEUT PAS ETRE ANNULEE SANS RESTAURATION DEPUIS BACKUP.

Usage :
    python manage.py drop_legacy_charfields --dry-run        # Liste les ALTER (defaut)
    python manage.py drop_legacy_charfields --confirm         # Execute (irreversible)
"""
from __future__ import annotations
from django.core.management.base import BaseCommand
from django.db import connection


TABLES = [
    'emplois_emplois',
    'emplois_emploisarchive',
    'suivi_suivie',
    'suivi_suivie_pointage',
]
LEGACY_COLUMNS = [
    'id_prof', 'id_em', 'id_salle', 'id_departement', 'id_semestre',
    'creneau', 'type_seance', 'jour',
]


class Command(BaseCommand):
    help = 'IRREVERSIBLE : drop des 8 CharField legacy sur les 4 tables emplois/suivi.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', default=True,
                            help='Liste les ALTER sans les executer (defaut).')
        parser.add_argument('--confirm', action='store_true', default=False,
                            help='Execute reellement les DROP COLUMN (IRREVERSIBLE).')

    def handle(self, *args, **opts):
        cur = connection.cursor()
        confirm = opts['confirm']

        # Recuperer les colonnes existantes par table
        existing = {}
        for t in TABLES:
            cur.execute("""
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            """, [t])
            existing[t] = {r[0] for r in cur.fetchall()}

        # Generer les ALTER a executer
        ddl_ops = []
        already_dropped = []
        for table in TABLES:
            cols_to_drop = [c for c in LEGACY_COLUMNS if c in existing[table]]
            if not cols_to_drop:
                already_dropped.append(table)
                continue
            sql = (f'ALTER TABLE `{table}` ' +
                   ', '.join(f'DROP COLUMN `{c}`' for c in cols_to_drop))
            ddl_ops.append((table, cols_to_drop, sql))

        # Affichage
        self.stdout.write(self.style.MIGRATE_HEADING('\n=== Plan de drop ==='))
        for t, cols, sql in ddl_ops:
            self.stdout.write(f'  {t} : {len(cols)} colonnes -> {", ".join(cols)}')
        for t in already_dropped:
            self.stdout.write(self.style.SUCCESS(f'  {t} : deja drop (skip)'))

        if not ddl_ops:
            self.stdout.write(self.style.SUCCESS('\nOK Toutes les colonnes legacy sont deja supprimees.'))
            return

        if not confirm:
            self.stdout.write(self.style.WARNING(
                '\n  DRY-RUN par defaut.\n'
                '  Pour executer reellement (IRREVERSIBLE) :\n'
                '    python manage.py drop_legacy_charfields --confirm\n'
                '\n  Avant l\'execution : faire un backup complet de la BD.'
            ))
            return

        # Execution reelle
        self.stdout.write(self.style.WARNING('\n=== EXECUTION (IRREVERSIBLE) ==='))
        for t, cols, sql in ddl_ops:
            self.stdout.write(f'\n  {sql}')
            try:
                cur.execute(sql)
                self.stdout.write(self.style.SUCCESS(f'  -> {t} : {len(cols)} colonnes supprimees.'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  ECHEC sur {t} : {e}'))
                raise

        # Verification finale
        self.stdout.write(self.style.MIGRATE_HEADING('\n=== Verification finale ==='))
        cur.execute("""
            SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME IN ('emplois_emplois','emplois_emploisarchive','suivi_suivie','suivi_suivie_pointage')
              AND COLUMN_NAME IN ('id_prof','id_em','id_salle','id_departement','id_semestre','creneau','type_seance','jour')
        """)
        rest = cur.fetchall()
        if rest:
            self.stdout.write(self.style.ERROR(f'  ATTENTION : {len(rest)} colonnes legacy persistent : {rest}'))
        else:
            self.stdout.write(self.style.SUCCESS('  OK Aucune colonne legacy ne subsiste sur les 4 tables.'))
            self.stdout.write(self.style.SUCCESS('\n=== PHASE 5 TERMINEE ==='))
