"""
Ajoute les contraintes FK (REFERENCES) aux colonnes
fk_xxx_id deja peuplees par backfill_legacy_fks.

Strategie : ON DELETE SET NULL (cohere avec le comportement
declare dans les modeles siga : ForeignKey(on_delete=SET_NULL)).

Idempotent : verifie si la contrainte existe deja avant l'ALTER.

Usage :
    python manage.py add_fk_constraints_legacy
    python manage.py add_fk_constraints_legacy --dry-run
"""
from __future__ import annotations
from django.core.management.base import BaseCommand
from django.db import connection


# (table, [(col_name, target_table, target_pk)])
CONSTRAINTS = {
    'emplois_emplois': [
        ('fk_prof_id',        'prof',        'id'),
        ('fk_em_id',          'em',          'id'),
        ('fk_salle_id',       'salle',       'id'),
        ('fk_departement_id', 'departement', 'id'),
        ('fk_semestre_id',    'semestre',    'id'),
        ('fk_creneau_id',     'creneau',     'id'),
    ],
    'emplois_emploisarchive': [
        ('fk_prof_id',        'prof',        'id'),
        ('fk_em_id',          'em',          'id'),
        ('fk_salle_id',       'salle',       'id'),
        ('fk_departement_id', 'departement', 'id'),
        ('fk_semestre_id',    'semestre',    'id'),
        ('fk_creneau_id',     'creneau',     'id'),
    ],
    'suivi_suivie': [
        ('fk_prof_id',        'prof',        'id'),
        ('fk_em_id',          'em',          'id'),
        ('fk_salle_id',       'salle',       'id'),
        ('fk_departement_id', 'departement', 'id'),
        ('fk_semestre_id',    'semestre',    'id'),
        ('fk_creneau_id',     'creneau',     'id'),
    ],
    'suivi_suivie_pointage': [
        ('fk_prof_id',        'prof',        'id'),
        ('fk_em_id',          'em',          'id'),
        ('fk_salle_id',       'salle',       'id'),
        ('fk_semestre_id',    'semestre',    'id'),
        ('fk_creneau_id',     'creneau',     'id'),
    ],
}


class Command(BaseCommand):
    help = 'Ajoute les contraintes FK ON DELETE SET NULL sur les colonnes fk_xxx_id.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        cur = connection.cursor()
        dry_run = opts['dry_run']

        # Lister les contraintes deja existantes
        cur.execute("""
            SELECT TABLE_NAME, CONSTRAINT_NAME
            FROM information_schema.TABLE_CONSTRAINTS
            WHERE TABLE_SCHEMA = DATABASE()
              AND CONSTRAINT_TYPE = 'FOREIGN KEY'
        """)
        existing = {(t, c) for t, c in cur.fetchall()}

        # Verifier que les colonnes referencees existent (pour eviter ERROR 1822)
        cur.execute("""
            SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
        """)
        cols_by_table = {}
        for t, c in cur.fetchall():
            cols_by_table.setdefault(t, set()).add(c)

        added = []
        skipped = []
        for table, cstrs in CONSTRAINTS.items():
            for col, tgt_table, tgt_pk in cstrs:
                if col not in cols_by_table.get(table, set()):
                    skipped.append((table, col, 'colonne absente'))
                    continue

                # Nom court pour MySQL (limite 64 chars) : prendre les initiales
                cname = f'fk_{table}__{col}'.replace('emplois_', 'em_').replace('suivi_suivie_pointage', 'ssp').replace('suivi_suivie', 'ss').replace('emplois_emploisarchive', 'eea')[:64]

                if (table, cname) in existing:
                    skipped.append((table, col, f'contrainte {cname} deja presente'))
                    continue

                sql = (
                    f'ALTER TABLE `{table}` '
                    f'ADD CONSTRAINT `{cname}` '
                    f'FOREIGN KEY (`{col}`) REFERENCES `{tgt_table}`(`{tgt_pk}`) '
                    f'ON DELETE SET NULL ON UPDATE CASCADE'
                )
                self.stdout.write(f'  {sql}')
                if not dry_run:
                    try:
                        cur.execute(sql)
                        added.append((table, col, cname))
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(f'    ERREUR : {e}'))
                        skipped.append((table, col, f'erreur: {e}'))
                else:
                    added.append((table, col, cname))

        # Recap
        self.stdout.write(self.style.MIGRATE_HEADING('\n=== Recap ==='))
        self.stdout.write(f'  Contraintes ajoutees : {len(added)}')
        self.stdout.write(f'  Sautees             : {len(skipped)}')
        for t, c, why in skipped:
            self.stdout.write(self.style.WARNING(f'    SKIP {t}.{c} : {why}'))

        if dry_run:
            self.stdout.write(self.style.WARNING('\n  DRY-RUN : aucun ALTER execute.'))
        else:
            self.stdout.write(self.style.SUCCESS('\nOK Contraintes FK ajoutees.'))
