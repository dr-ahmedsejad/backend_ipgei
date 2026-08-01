"""
Renomme les colonnes FK creees par add_fk_columns_legacy
pour s'aligner avec ce qu'attend le code Django siga
(`db_column='fk_xxx_id'` dans les modeles).

Mapping (colonne actuelle -> colonne attendue par siga) :
  prof_id        -> fk_prof_id
  em_id          -> fk_em_id
  salle_id       -> fk_salle_id
  departement_id -> fk_departement_id  (sauf suivi_suivie_pointage)
  semestre_id    -> fk_semestre_id
  creneau_fk_id  -> fk_creneau_id

Idempotent : verifie si la colonne source existe avant le RENAME.
N'efface aucune donnee — RENAME COLUMN preserve les valeurs.

Usage :
    python manage.py align_fk_column_names
    python manage.py align_fk_column_names --dry-run
"""
from __future__ import annotations
from django.core.management.base import BaseCommand
from django.db import connection


# (table, [(old_name, new_name), ...])
RENAME_MAP = {
    'emplois_emplois': [
        ('prof_id',        'fk_prof_id'),
        ('em_id',          'fk_em_id'),
        ('salle_id',       'fk_salle_id'),
        ('departement_id', 'fk_departement_id'),
        ('semestre_id',    'fk_semestre_id'),
        ('creneau_fk_id',  'fk_creneau_id'),
    ],
    'emplois_emploisarchive': [
        ('prof_id',        'fk_prof_id'),
        ('em_id',          'fk_em_id'),
        ('salle_id',       'fk_salle_id'),
        ('departement_id', 'fk_departement_id'),
        ('semestre_id',    'fk_semestre_id'),
        ('creneau_fk_id',  'fk_creneau_id'),
    ],
    'suivi_suivie': [
        ('prof_id',        'fk_prof_id'),
        ('em_id',          'fk_em_id'),
        ('salle_id',       'fk_salle_id'),
        ('departement_id', 'fk_departement_id'),
        ('semestre_id',    'fk_semestre_id'),
        ('creneau_fk_id',  'fk_creneau_id'),
    ],
    'suivi_suivie_pointage': [
        ('prof_id',        'fk_prof_id'),
        ('em_id',          'fk_em_id'),
        ('salle_id',       'fk_salle_id'),
        ('semestre_id',    'fk_semestre_id'),
        ('creneau_fk_id',  'fk_creneau_id'),
    ],
}


class Command(BaseCommand):
    help = 'Renomme les colonnes FK pour s\'aligner avec db_column siga.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Liste les RENAME sans les executer.')

    def handle(self, *args, **opts):
        cur = connection.cursor()
        dry_run = opts['dry_run']

        # Recuperer les colonnes actuelles
        existing = {}
        for t in RENAME_MAP:
            cur.execute("""
                SELECT COLUMN_NAME FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            """, [t])
            existing[t] = {r[0] for r in cur.fetchall()}

        renamed = []
        already_ok = []
        skipped = []

        for table, pairs in RENAME_MAP.items():
            for old, new in pairs:
                cols = existing[table]
                if new in cols and old not in cols:
                    already_ok.append((table, old, new))
                    continue
                if old not in cols:
                    skipped.append((table, old, new, 'colonne source absente'))
                    continue
                if new in cols and old in cols:
                    skipped.append((table, old, new, 'collision : les 2 noms coexistent'))
                    continue
                sql = f'ALTER TABLE `{table}` RENAME COLUMN `{old}` TO `{new}`'
                self.stdout.write(f'  {sql}')
                if not dry_run:
                    cur.execute(sql)
                renamed.append((table, old, new))

        # Recap
        self.stdout.write(self.style.MIGRATE_HEADING('\n=== Recap ==='))
        self.stdout.write(f'  Colonnes renommees      : {len(renamed)}')
        self.stdout.write(f'  Deja au bon nom         : {len(already_ok)}')
        self.stdout.write(f'  Sautees (anomalies)     : {len(skipped)}')
        for t, o, n, why in skipped:
            self.stdout.write(self.style.WARNING(f'    SKIP {t}.{o}->{n} : {why}'))

        if dry_run:
            self.stdout.write(self.style.WARNING('\n  DRY-RUN : aucun RENAME execute.'))
        else:
            self.stdout.write(self.style.SUCCESS('\nOK Colonnes renommees.'))
