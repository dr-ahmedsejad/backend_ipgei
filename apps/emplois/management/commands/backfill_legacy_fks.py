"""
Backfill DML : remplit les FK ajoutees par add_fk_columns_legacy
en lisant les CharField legacy et en resolvant vers les tables cibles.

Pour chaque table source, applique les UPDATE atomiques suivants
(dans une transaction par table) :

  prof_id            <- prof.id            via id_prof = prof.id (numerique)
                                            ou id_prof = prof.nom
  em_id              <- em.id              via id_em = em.id (numerique)
                                            ou id_em = em.code_em
  salle_id           <- salle.id           via id_salle = salle.id (numerique)
                                            ou id_salle = salle.nom
  departement_id     <- departement.id     via id_departement = departement.nom
                                            AND annee_universitaire = departement.annee_universitaire
                                            (PAS appliquee a suivi_suivie_pointage : utilise M2M)
  semestre_id        <- semestre.id        via id_semestre = semestre.code_semestre
                                            ou id_semestre = semestre.semestre
  creneau_fk_id      <- creneau.id         via creneau = creneau.creneau

Le CharField source est CONSERVE intact (filet de securite).

Idempotent : les UPDATE filtrent sur `WHERE FK IS NULL` -> pas de doublon
si on relance la commande.

Verifications post-backfill :
  - Compter les lignes ou CharField rempli mais FK NULL apres backfill
    (doivent etre 0 pour chaque colonne, sinon ROLLBACK).

Usage :
    python manage.py backfill_legacy_fks
    python manage.py backfill_legacy_fks --dry-run
"""
from __future__ import annotations
from django.core.management.base import BaseCommand
from django.db import connection, transaction


# (source_table, [ {fk_col, charfield, target_table, by_id, by_name, by_fallback, needs_year}, ... ])
BACKFILL_PLAN = {
    'emplois_emplois':         True,   # toutes les FK
    'emplois_emploisarchive':  True,
    'suivi_suivie':            True,
    'suivi_suivie_pointage':   False,  # pas de departement_id (M2M)
}


class Command(BaseCommand):
    help = 'Backfill les FK depuis les CharField legacy (rollback automatique en cas d\'incoherence).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Simule (rollback final).')

    def handle(self, *args, **opts):
        cur = connection.cursor()
        dry_run = opts['dry_run']

        global_summary = []

        for table, has_dept in BACKFILL_PLAN.items():
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n=== {table} ==='))
            cur.execute(f"SELECT COUNT(*) FROM `{table}`")
            total = cur.fetchone()[0]
            self.stdout.write(f'  Total lignes : {total}')

            if total == 0:
                self.stdout.write('  (table vide, rien a faire)')
                continue

            try:
                with transaction.atomic():
                    self._backfill_table(cur, table, has_dept)
                    self._verify_table(cur, table, has_dept)
                    global_summary.append((table, total))
                    if dry_run:
                        raise _DryRunRollback()
            except _DryRunRollback:
                self.stdout.write(self.style.WARNING('  DRY-RUN : rollback final pour cette table.'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  ERREUR : {e}'))
                self.stdout.write(self.style.WARNING('  Toutes les operations sur cette table ont ete annulees.'))
                raise

        self.stdout.write(self.style.MIGRATE_HEADING('\n=== Recap global ==='))
        for t, n in global_summary:
            self.stdout.write(f'  {t}: {n} lignes traitees')

        if dry_run:
            self.stdout.write(self.style.SUCCESS('\nOK Dry-run termine. Aucune modification persistee.'))
        else:
            self.stdout.write(self.style.SUCCESS('\nOK Backfill applique et commit.'))

    # ────────────────────────────────────────────────────────────────────
    def _backfill_table(self, cur, table, has_dept):
        """UPDATEs idempotents : ne touche que les lignes ou la FK est encore NULL."""

        # 1. prof_id : numerique vers prof.id
        n = self._update(cur, f"""
            UPDATE `{table}` s
            JOIN prof p ON p.id = CAST(s.id_prof AS UNSIGNED)
            SET s.prof_id = p.id
            WHERE s.prof_id IS NULL AND s.id_prof REGEXP '^[0-9]+$'
        """)
        self.stdout.write(f'  prof_id (numerique)         : {n} updates')

        # prof_id par nom (fallback texte)
        n = self._update(cur, f"""
            UPDATE `{table}` s
            JOIN prof p ON p.nom = s.id_prof
            SET s.prof_id = p.id
            WHERE s.prof_id IS NULL AND s.id_prof != '' AND s.id_prof NOT REGEXP '^[0-9]+$'
        """)
        self.stdout.write(f'  prof_id (par nom)           : {n} updates')

        # 2. em_id : numerique vers em.id
        n = self._update(cur, f"""
            UPDATE `{table}` s
            JOIN em e ON e.id = CAST(s.id_em AS UNSIGNED)
            SET s.em_id = e.id
            WHERE s.em_id IS NULL AND s.id_em REGEXP '^[0-9]+$'
        """)
        self.stdout.write(f'  em_id (numerique)           : {n} updates')

        n = self._update(cur, f"""
            UPDATE `{table}` s
            JOIN em e ON e.code_em = s.id_em
            SET s.em_id = e.id
            WHERE s.em_id IS NULL AND s.id_em != '' AND s.id_em NOT REGEXP '^[0-9]+$'
        """)
        self.stdout.write(f'  em_id (par code_em)         : {n} updates')

        # 3. salle_id
        n = self._update(cur, f"""
            UPDATE `{table}` s
            JOIN salle sa ON sa.id = CAST(s.id_salle AS UNSIGNED)
            SET s.salle_id = sa.id
            WHERE s.salle_id IS NULL AND s.id_salle REGEXP '^[0-9]+$'
        """)
        self.stdout.write(f'  salle_id (numerique)        : {n} updates')

        n = self._update(cur, f"""
            UPDATE `{table}` s
            JOIN salle sa ON sa.nom = s.id_salle
            SET s.salle_id = sa.id
            WHERE s.salle_id IS NULL AND s.id_salle != '' AND s.id_salle NOT REGEXP '^[0-9]+$'
        """)
        self.stdout.write(f'  salle_id (par nom)          : {n} updates')

        # 4. departement_id (sauf SuiviePointage : utilise M2M)
        if has_dept:
            # Resolution texte avec annee_universitaire
            n = self._update(cur, f"""
                UPDATE `{table}` s
                JOIN departement d
                  ON d.nom = s.id_departement
                 AND d.annee_universitaire = s.annee_universitaire
                SET s.departement_id = d.id
                WHERE s.departement_id IS NULL
                  AND s.id_departement != ''
                  AND s.id_departement NOT REGEXP '^[0-9]+$'
            """)
            self.stdout.write(f'  departement_id (par nom+annee) : {n} updates')

            # Cas (rare) numerique
            n = self._update(cur, f"""
                UPDATE `{table}` s
                JOIN departement d ON d.id = CAST(s.id_departement AS UNSIGNED)
                SET s.departement_id = d.id
                WHERE s.departement_id IS NULL
                  AND s.id_departement REGEXP '^[0-9]+$'
            """)
            self.stdout.write(f'  departement_id (numerique)  : {n} updates')

        # 5. semestre_id : par code_semestre puis par semestre (libelle)
        n = self._update(cur, f"""
            UPDATE `{table}` s
            JOIN semestre sm ON sm.code_semestre = s.id_semestre
            SET s.semestre_id = sm.id
            WHERE s.semestre_id IS NULL AND s.id_semestre != ''
              AND s.id_semestre NOT REGEXP '^[0-9]+$'
        """)
        self.stdout.write(f'  semestre_id (code_semestre) : {n} updates')

        n = self._update(cur, f"""
            UPDATE `{table}` s
            JOIN semestre sm ON sm.semestre = s.id_semestre
            SET s.semestre_id = sm.id
            WHERE s.semestre_id IS NULL AND s.id_semestre != ''
              AND s.id_semestre NOT REGEXP '^[0-9]+$'
        """)
        self.stdout.write(f'  semestre_id (semestre lbl)  : {n} updates')

        n = self._update(cur, f"""
            UPDATE `{table}` s
            JOIN semestre sm ON sm.id = CAST(s.id_semestre AS UNSIGNED)
            SET s.semestre_id = sm.id
            WHERE s.semestre_id IS NULL AND s.id_semestre REGEXP '^[0-9]+$'
        """)
        self.stdout.write(f'  semestre_id (numerique)     : {n} updates')

        # 6. creneau_fk_id : par libelle creneau
        n = self._update(cur, f"""
            UPDATE `{table}` s
            JOIN creneau c ON c.creneau = s.creneau
            SET s.creneau_fk_id = c.id
            WHERE s.creneau_fk_id IS NULL AND s.creneau != ''
              AND s.creneau NOT REGEXP '^[0-9]+$'
        """)
        self.stdout.write(f'  creneau_fk_id (par lbl)     : {n} updates')

        n = self._update(cur, f"""
            UPDATE `{table}` s
            JOIN creneau c ON c.id = CAST(s.creneau AS UNSIGNED)
            SET s.creneau_fk_id = c.id
            WHERE s.creneau_fk_id IS NULL AND s.creneau REGEXP '^[0-9]+$'
        """)
        self.stdout.write(f'  creneau_fk_id (numerique)   : {n} updates')

    # ────────────────────────────────────────────────────────────────────
    def _verify_table(self, cur, table, has_dept):
        """Verifie qu'aucune ligne n'a CharField rempli + FK NULL."""
        checks = [
            ('id_prof',        'prof_id'),
            ('id_em',          'em_id'),
            ('id_salle',       'salle_id'),
            ('id_semestre',    'semestre_id'),
            ('creneau',        'creneau_fk_id'),
        ]
        if has_dept:
            checks.append(('id_departement', 'departement_id'))

        anomalies = []
        for char_col, fk_col in checks:
            cur.execute(f"""
                SELECT COUNT(*) FROM `{table}`
                WHERE `{char_col}` IS NOT NULL
                  AND `{char_col}` != ''
                  AND `{fk_col}` IS NULL
            """)
            n = cur.fetchone()[0]
            status = 'OK' if n == 0 else f'ANOMALIE ({n})'
            self.stdout.write(f'  Verif {char_col} -> {fk_col}: {status}')
            if n > 0:
                anomalies.append((char_col, fk_col, n))

        if anomalies:
            details = ', '.join(f'{c}->{f}={n}' for c, f, n in anomalies)
            raise RuntimeError(f'PERTE potentielle dans `{table}` : {details}')

    # ────────────────────────────────────────────────────────────────────
    def _update(self, cur, sql):
        """Execute un UPDATE et retourne le rowcount."""
        cur.execute(sql)
        return cur.rowcount


class _DryRunRollback(Exception):
    pass
