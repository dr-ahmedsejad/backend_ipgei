"""
Ajoute les colonnes siga manquantes (au-dela des FK) pour aligner
la BD legacy avec ce que les modeles Django attendent.

Sans ces colonnes, l'ORM Django leve `Champ inconnu dans field list`
des qu'il essaie de SELECT * une ligne.

Tables et colonnes ajoutees (toutes nullables, valeur par defaut neutre) :
  emplois_emplois
    + taux_paiement   DOUBLE NULL DEFAULT 0
    + institution_id  BIGINT NULL
  emplois_emploisarchive
    + taux_paiement   DOUBLE NULL DEFAULT 0
    + institution_id  BIGINT NULL
  suivi_suivie
    + taux_paiement   DOUBLE NULL DEFAULT 0
    + institution_id  BIGINT NULL
  suivi_suivie_pointage
    + taux_paiement      DOUBLE NULL
    + reclamation_motif  TEXT NULL
    + reclamation_statut VARCHAR(20) NOT NULL DEFAULT ''
    + institution_id     BIGINT NULL

Note : `institution_id` reste NULL — le user devra UPDATE manuellement
les valeurs en fonction de l'institution proprietaire des donnees.
Aucun backfill automatique pour ne pas inventer de la donnee.

Idempotent : utilise IF NOT EXISTS (MySQL 8.0.29+).

Usage :
    python manage.py add_remaining_siga_columns
    python manage.py add_remaining_siga_columns --dry-run
"""
from __future__ import annotations
from django.core.management.base import BaseCommand
from django.db import connection


# (table, [(col, type_def)])
COLUMNS_PER_TABLE = {
    'emplois_emplois': [
        ('taux_paiement',  'DOUBLE NULL DEFAULT 0'),
        ('institution_id', 'BIGINT NULL'),
    ],
    'emplois_emploisarchive': [
        ('taux_paiement',  'DOUBLE NULL DEFAULT 0'),
        ('institution_id', 'BIGINT NULL'),
    ],
    'suivi_suivie': [
        ('taux_paiement',  'DOUBLE NULL DEFAULT 0'),
        ('institution_id', 'BIGINT NULL'),
    ],
    'suivi_suivie_pointage': [
        ('taux_paiement',      'DOUBLE NULL'),
        ('reclamation_motif',  'TEXT NULL'),
        ('reclamation_statut', "VARCHAR(20) NOT NULL DEFAULT ''"),
        ('institution_id',     'BIGINT NULL'),
    ],
}


class Command(BaseCommand):
    help = 'Ajoute les colonnes siga manquantes (taux_paiement, reclamation_*, institution_id).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        cur = connection.cursor()
        dry_run = opts['dry_run']

        # Recuperer colonnes existantes par table
        existing = {}
        for t in COLUMNS_PER_TABLE:
            cur.execute("""
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            """, [t])
            existing[t] = {r[0] for r in cur.fetchall()}

        added = []
        skipped = []
        for table, cols in COLUMNS_PER_TABLE.items():
            for col, type_def in cols:
                if col in existing[table]:
                    skipped.append((table, col))
                    continue
                sql = f'ALTER TABLE `{table}` ADD COLUMN `{col}` {type_def}'
                self.stdout.write(f'  {sql}')
                if not dry_run:
                    cur.execute(sql)
                added.append((table, col))

        self.stdout.write(self.style.MIGRATE_HEADING('\n=== Recap ==='))
        self.stdout.write(f'  Colonnes ajoutees    : {len(added)}')
        self.stdout.write(f'  Deja presentes       : {len(skipped)}')
        for t, c in skipped:
            self.stdout.write(f'    deja: {t}.{c}')

        if dry_run:
            self.stdout.write(self.style.WARNING('\n  DRY-RUN : aucun ALTER execute.'))
        else:
            self.stdout.write(self.style.SUCCESS('\nOK Schema aligne avec siga.'))
            self.stdout.write(self.style.WARNING(
                '\nRAPPEL : `institution_id` est NULL pour toutes les lignes existantes.\n'
                '         Le user doit UPDATE manuellement les valeurs avant que le code\n'
                '         siga avec InstitutionScopedMixin ne devienne strict.'
            ))
