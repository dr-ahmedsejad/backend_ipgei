"""
Backfill `institution_id` (NULL) sur les 4 tables emplois/suivi
+ vacation_*.

Stratégie en 2 etapes :

  1. Premier passage : si la ligne a un `departement_id` ET ce departement
     a une `institution_id` non NULL, on copie cette valeur.
     -> evite les conflits cross-institution si departement scoping est deja fait.

  2. Second passage : pour toutes les lignes encore NULL,
     on assigne `--institution-id` (par defaut 2 = ESP).

Idempotent : ne touche que les lignes ou institution_id IS NULL.

Tables traitees :
  - emplois_emplois, emplois_emploisarchive
  - suivi_suivie, suivi_suivie_pointage
  - vacation_vacation, vacation_surveillance
  - departement (premiere fois : assigne la default institution)

Usage :
    python manage.py backfill_institution_id              # default = 2 (ESP)
    python manage.py backfill_institution_id --institution-id 1
    python manage.py backfill_institution_id --dry-run
"""
from __future__ import annotations
from django.core.management.base import BaseCommand
from django.db import connection, transaction


# Tables avec colonne institution_id et departement_id (pour heritage)
TABLES_WITH_DEPT = [
    'emplois_emplois', 'emplois_emploisarchive',
    'suivi_suivie',
]
# Tables sans departement_id direct (institution forcee par defaut)
TABLES_DIRECT = [
    'suivi_suivie_pointage',  # pas de departement_id (M2M)
    'vacation_vacation',
    'vacation_surveillance',
]


class Command(BaseCommand):
    help = 'Backfill institution_id (NULL) en heritant via departement_id si possible.'

    def add_arguments(self, parser):
        parser.add_argument('--institution-id', type=int, default=2,
                            help='Institution par defaut pour les lignes sans departement (default=2=ESP).')
        parser.add_argument('--dry-run', action='store_true', help='Simule (rollback final).')

    def handle(self, *args, **opts):
        cur = connection.cursor()
        default_inst = opts['institution_id']
        dry_run = opts['dry_run']

        # Verifier que l'institution existe
        cur.execute("SELECT acronyme, nom FROM institution WHERE id = %s", [default_inst])
        row = cur.fetchone()
        if not row:
            self.stdout.write(self.style.ERROR(f'Institution {default_inst} introuvable.'))
            return
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\nBackfill avec institution_id={default_inst} ({row[0]} - {row[1]})'
        ))

        try:
            with transaction.atomic():
                # 0) Backfill departement.institution_id avec la default
                cur.execute(
                    "UPDATE departement SET institution_id = %s WHERE institution_id IS NULL",
                    [default_inst],
                )
                self.stdout.write(f'  departement.institution_id : {cur.rowcount} updates')

                # 1) Tables avec departement_id : tenter heritage
                for tbl in TABLES_WITH_DEPT:
                    cur.execute(f"""
                        UPDATE `{tbl}` t
                        JOIN departement d ON d.id = t.fk_departement_id
                        SET t.institution_id = d.institution_id
                        WHERE t.institution_id IS NULL AND d.institution_id IS NOT NULL
                    """)
                    n_inherit = cur.rowcount

                    # Fallback : reste des NULL -> default
                    cur.execute(
                        f"UPDATE `{tbl}` SET institution_id = %s WHERE institution_id IS NULL",
                        [default_inst],
                    )
                    n_default = cur.rowcount
                    self.stdout.write(f'  {tbl} : {n_inherit} via dept, {n_default} via default')

                # 2) Tables sans departement_id : default direct
                for tbl in TABLES_DIRECT:
                    cur.execute(
                        f"UPDATE `{tbl}` SET institution_id = %s WHERE institution_id IS NULL",
                        [default_inst],
                    )
                    self.stdout.write(f'  {tbl} : {cur.rowcount} via default')

                # Verification post-backfill : aucune ligne ne doit rester NULL
                self.stdout.write(self.style.MIGRATE_HEADING('\n=== Verification ==='))
                anomalies = []
                for tbl in TABLES_WITH_DEPT + TABLES_DIRECT + ['departement']:
                    cur.execute(f"SELECT COUNT(*) FROM `{tbl}` WHERE institution_id IS NULL")
                    n = cur.fetchone()[0]
                    status = 'OK' if n == 0 else f'ANOMALIE ({n})'
                    self.stdout.write(f'  {tbl}.institution_id NULL : {status}')
                    if n > 0:
                        anomalies.append((tbl, n))

                if anomalies:
                    raise RuntimeError(f'Lignes NULL persistantes : {anomalies}')

                if dry_run:
                    self.stdout.write(self.style.WARNING('\n  DRY-RUN : rollback final.'))
                    raise _DryRunRollback()

            self.stdout.write(self.style.SUCCESS('\nOK Backfill institution_id applique.'))

        except _DryRunRollback:
            self.stdout.write(self.style.SUCCESS('\nOK Dry-run termine, aucune modification persistee.'))


class _DryRunRollback(Exception):
    pass
