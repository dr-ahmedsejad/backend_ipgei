"""
Phase 1 — Audit READ-ONLY (SQL pur) des CharField legacy sur :
  - emplois_emplois            (table siga moderne, peut etre vide)
  - emplois_emploisarchive
  - suivi_suivie
  - suivi_suivie_pointage      (id_departement multi-valeurs separees par '/')

Approche : SQL brut via connection.cursor(), aucune dependance au modele Django.
Cela evite tout drift de schema. Lit ce qui existe reellement en BD.

Pour chaque CharField, mesure :
  * total / non-vide / vide
  * valeurs distinctes
  * resolvabilite par PK (CharField numerique -> table cible.id)
  * resolvabilite par nom/code/label (CharField texte -> table cible.nom_attr)
  * non-resolvables (impossible a backfiller automatiquement)

Pour id_departement : tient compte de annee_universitaire pour desambiguïser.
Pour suivi_suivie_pointage.id_departement : analyse la distribution multi-valeurs.

Usage :
    python manage.py audit_legacy_charfields
    python manage.py audit_legacy_charfields --csv backups/audit.csv
    python manage.py audit_legacy_charfields --markdown backups/audit.md
"""
from __future__ import annotations
import csv
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import connection


# Tables source a auditer
SOURCE_TABLES = ['emplois_emplois', 'emplois_emploisarchive', 'suivi_suivie', 'suivi_suivie_pointage']

# (charfield, lookup_table, lookup_id_col, lookup_name_col, fallback_name_col)
CHARFIELD_TARGETS = [
    ('id_prof',        'prof',        'id', 'nom',           None),
    ('id_em',          'em',          'id', 'code_em',       None),
    ('id_salle',       'salle',       'id', 'nom',           None),
    ('id_departement', 'departement', 'id', 'nom',           None),  # cas special : utilise annee_universitaire
    ('id_semestre',    'semestre',    'id', 'code_semestre', 'semestre'),
    ('creneau',        'creneau',     'id', 'creneau',       None),
    ('type_seance',    'seance',      'id', 'type_seance',   None),
    ('jour',           'jour',        'id', 'jour',          None),
]


class Command(BaseCommand):
    help = 'Audit READ-ONLY (SQL pur) des CharField legacy sur les 4 tables emplois/suivi.'

    def add_arguments(self, parser):
        parser.add_argument('--csv',      type=str, default='', help='Fichier CSV de sortie.')
        parser.add_argument('--markdown', type=str, default='', help='Fichier Markdown recapitulatif.')
        parser.add_argument('--samples',  type=int, default=10, help='Nombre d\'exemples a afficher pour les non-resolvables.')

    def handle(self, *args, **opts):
        self.cur = connection.cursor()
        self.csv_path = opts['csv']
        self.md_path  = opts['markdown']
        self.n_samples = opts['samples']

        all_rows = []  # pour CSV / Markdown
        md_lines = ['# Audit Phase 1 — CharField legacy', '']

        # ── Sanity check : compter les tables source ────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING('\n=== Tailles des tables source ==='))
        sizes = {}
        for t in SOURCE_TABLES:
            n = self._count(t)
            sizes[t] = n
            self.stdout.write(f'  {t:<26} : {n:>8} lignes')
        md_lines.append('## Tailles des tables source\n')
        md_lines.append('| Table | Lignes |')
        md_lines.append('|-------|--------|')
        for t, n in sizes.items():
            md_lines.append(f'| `{t}` | {n} |')
        md_lines.append('')

        # ── Audit champ par champ ───────────────────────────────────────
        for table in SOURCE_TABLES:
            if sizes[table] == 0:
                self.stdout.write(self.style.WARNING(f'\n=== {table} : VIDE — saute ==='))
                continue
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n=== {table} ({sizes[table]} lignes) ==='))
            md_lines.append(f'## `{table}` ({sizes[table]} lignes)\n')
            md_lines.append('| CharField | non-vide | distinct | num | nom | resolu PK | resolu nom | **non-resolu** |')
            md_lines.append('|-----------|----------|----------|-----|-----|-----------|------------|----------------|')

            for char_col, tgt_table, tgt_id, tgt_name, tgt_fallback in CHARFIELD_TARGETS:
                stats = self._audit_charfield(table, char_col, tgt_table, tgt_id, tgt_name, tgt_fallback)
                self._print_stats(char_col, tgt_table, stats)
                all_rows.append({
                    'source_table': table,
                    'charfield':    char_col,
                    'target':       tgt_table,
                    **stats,
                })
                md_lines.append(
                    f'| `{char_col}` -> `{tgt_table}` | {stats["non_empty"]} | {stats["distinct"]} | '
                    f'{stats["numeric"]} | {stats["non_numeric"]} | '
                    f'{stats["resolved_by_id"]} | {stats["resolved_by_name"]} | '
                    f'**{stats["unresolvable"]}** |'
                )

                # Échantillon des non-resolvables
                if stats['unresolvable'] > 0:
                    samples = self._sample_unresolvable(table, char_col, tgt_table, tgt_id, tgt_name, tgt_fallback)
                    if samples:
                        self.stdout.write(self.style.WARNING(f'    Exemples non-resolvables :'))
                        for s in samples[:self.n_samples]:
                            self.stdout.write(f'      "{s}"')
            md_lines.append('')

        # ── Audit specifique id_departement multi-valeurs (suivi_suivie_pointage) ──
        if sizes.get('suivi_suivie_pointage', 0) > 0:
            self.stdout.write(self.style.MIGRATE_HEADING('\n=== suivi_suivie_pointage.id_departement (multi-valeurs) ==='))
            md_lines.append('## `suivi_suivie_pointage.id_departement` — multi-valeurs separees par `/`\n')
            distrib, samples_multi, unresolved_tokens = self._audit_pointage_multidept()

            self.stdout.write(f'  Distribution nb tokens / ligne : {dict(distrib)}')
            md_lines.append('| Nb tokens | Lignes |')
            md_lines.append('|-----------|--------|')
            for k in sorted(distrib.keys()):
                md_lines.append(f'| {k} | {distrib[k]} |')
            md_lines.append('')

            if samples_multi:
                self.stdout.write(f'  Exemples multi-dept :')
                md_lines.append('### Exemples multi-departement')
                md_lines.append('```')
                for raw, tokens in samples_multi[:5]:
                    line = f'  "{raw}" -> {tokens}'
                    self.stdout.write(line)
                    md_lines.append(line.strip())
                md_lines.append('```')

            if unresolved_tokens:
                self.stdout.write(self.style.WARNING(f'  Tokens non resolvables : {len(unresolved_tokens)}'))
                md_lines.append(f'\n### Tokens non resolvables ({len(unresolved_tokens)})')
                md_lines.append('```')
                for tok, year in unresolved_tokens[:self.n_samples]:
                    line = f'  token="{tok}" annee={year}'
                    self.stdout.write(line)
                    md_lines.append(line.strip())
                md_lines.append('```')

        # ── Synthese globale ────────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING('\n=== Synthese globale ==='))
        total_unresolvable = sum(r['unresolvable'] for r in all_rows)
        total_non_empty    = sum(r['non_empty']     for r in all_rows)
        pct = (100.0 * total_unresolvable / total_non_empty) if total_non_empty else 0.0

        self.stdout.write(f'  Total occurrences CharField non vides   : {total_non_empty}')
        self.stdout.write(f'  Total non-resolvables (perte potentielle): {total_unresolvable} ({pct:.2f} %)')

        md_lines.append('\n## Synthese globale')
        md_lines.append(f'- Total occurrences CharField non vides : **{total_non_empty}**')
        md_lines.append(f'- Total non-resolvables : **{total_unresolvable}** (**{pct:.2f}%**)')

        if total_unresolvable == 0:
            self.stdout.write(self.style.SUCCESS('\n✅ Tous les CharField sont resolvables — Phase 2 (backfill) peut demarrer en confiance.'))
            md_lines.append('\n✅ **Tous les CharField sont resolvables — Phase 2 (backfill) peut demarrer.**')
        else:
            self.stdout.write(self.style.WARNING(f'\n⚠ {total_unresolvable} occurrence(s) non resolvables. À traiter avant backfill.'))
            md_lines.append(f'\n⚠ **{total_unresolvable} occurrence(s) non resolvables — a traiter manuellement avant backfill.**')

        # ── Sortie fichiers ─────────────────────────────────────────────
        if self.csv_path:
            self._write_csv(all_rows)
            self.stdout.write(self.style.SUCCESS(f'\nCSV : {self.csv_path}'))
        if self.md_path:
            Path(self.md_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.md_path).write_text('\n'.join(md_lines), encoding='utf-8')
            self.stdout.write(self.style.SUCCESS(f'Markdown : {self.md_path}'))

    # ────────────────────────────────────────────────────────────────────
    def _count(self, table):
        self.cur.execute(f'SELECT COUNT(*) FROM `{table}`')
        return self.cur.fetchone()[0]

    # ────────────────────────────────────────────────────────────────────
    def _audit_charfield(self, src_table, char_col, tgt_table, tgt_id, tgt_name, tgt_fallback):
        """Mesure pour un seul CharField -> renvoie un dict de stats."""
        # 1. Total non-vide
        self.cur.execute(
            f"SELECT COUNT(*) FROM `{src_table}` WHERE `{char_col}` IS NOT NULL AND `{char_col}` != ''"
        )
        non_empty = self.cur.fetchone()[0]

        # 2. Distinct
        self.cur.execute(
            f"SELECT COUNT(DISTINCT `{char_col}`) FROM `{src_table}` "
            f"WHERE `{char_col}` IS NOT NULL AND `{char_col}` != ''"
        )
        distinct = self.cur.fetchone()[0]

        # 3. Numerique vs nom
        self.cur.execute(
            f"SELECT COUNT(*) FROM `{src_table}` "
            f"WHERE `{char_col}` IS NOT NULL AND `{char_col}` != '' "
            f"  AND `{char_col}` REGEXP '^[0-9]+$'"
        )
        numeric = self.cur.fetchone()[0]
        non_numeric = non_empty - numeric

        # 4. Resolvable par PK (numerique)
        self.cur.execute(
            f"SELECT COUNT(*) FROM `{src_table}` s "
            f"INNER JOIN `{tgt_table}` t ON CAST(s.`{char_col}` AS UNSIGNED) = t.`{tgt_id}` "
            f"WHERE s.`{char_col}` REGEXP '^[0-9]+$'"
        )
        resolved_by_id = self.cur.fetchone()[0]

        # 5. Resolvable par nom
        # Cas special Departement : besoin de annee_universitaire
        if tgt_table == 'departement':
            self.cur.execute(
                f"SELECT COUNT(*) FROM `{src_table}` s "
                f"INNER JOIN `{tgt_table}` t "
                f"  ON s.`{char_col}` = t.`{tgt_name}` "
                f"  AND s.`annee_universitaire` = t.`annee_universitaire` "
                f"WHERE s.`{char_col}` IS NOT NULL AND s.`{char_col}` != '' "
                f"  AND s.`{char_col}` NOT REGEXP '^[0-9]+$'"
            )
        else:
            cond = f"s.`{char_col}` = t.`{tgt_name}`"
            if tgt_fallback:
                cond = f"(s.`{char_col}` = t.`{tgt_name}` OR s.`{char_col}` = t.`{tgt_fallback}`)"
            self.cur.execute(
                f"SELECT COUNT(*) FROM `{src_table}` s "
                f"INNER JOIN `{tgt_table}` t ON {cond} "
                f"WHERE s.`{char_col}` IS NOT NULL AND s.`{char_col}` != '' "
                f"  AND s.`{char_col}` NOT REGEXP '^[0-9]+$'"
            )
        resolved_by_name = self.cur.fetchone()[0]

        unresolvable = non_empty - resolved_by_id - resolved_by_name

        return {
            'non_empty':        non_empty,
            'distinct':         distinct,
            'numeric':          numeric,
            'non_numeric':      non_numeric,
            'resolved_by_id':   resolved_by_id,
            'resolved_by_name': resolved_by_name,
            'unresolvable':     max(unresolvable, 0),
        }

    # ────────────────────────────────────────────────────────────────────
    def _sample_unresolvable(self, src_table, char_col, tgt_table, tgt_id, tgt_name, tgt_fallback, limit=10):
        """Retourne jusqu'a `limit` valeurs distinctes non-resolvables."""
        if tgt_table == 'departement':
            sql = (
                f"SELECT DISTINCT s.`{char_col}` FROM `{src_table}` s "
                f"WHERE s.`{char_col}` IS NOT NULL AND s.`{char_col}` != '' "
                f"  AND NOT EXISTS ("
                f"    SELECT 1 FROM `{tgt_table}` t "
                f"    WHERE (s.`{char_col}` REGEXP '^[0-9]+$' AND t.`{tgt_id}` = CAST(s.`{char_col}` AS UNSIGNED)) "
                f"       OR (s.`{char_col}` NOT REGEXP '^[0-9]+$' AND t.`{tgt_name}` = s.`{char_col}` AND t.`annee_universitaire` = s.`annee_universitaire`)"
                f"  ) LIMIT {limit}"
            )
        else:
            extra = ''
            if tgt_fallback:
                extra = f" OR t.`{tgt_fallback}` = s.`{char_col}`"
            sql = (
                f"SELECT DISTINCT s.`{char_col}` FROM `{src_table}` s "
                f"WHERE s.`{char_col}` IS NOT NULL AND s.`{char_col}` != '' "
                f"  AND NOT EXISTS ("
                f"    SELECT 1 FROM `{tgt_table}` t "
                f"    WHERE (s.`{char_col}` REGEXP '^[0-9]+$' AND t.`{tgt_id}` = CAST(s.`{char_col}` AS UNSIGNED)) "
                f"       OR (s.`{char_col}` NOT REGEXP '^[0-9]+$' AND (t.`{tgt_name}` = s.`{char_col}`{extra}))"
                f"  ) LIMIT {limit}"
            )
        self.cur.execute(sql)
        return [r[0] for r in self.cur.fetchall()]

    # ────────────────────────────────────────────────────────────────────
    def _print_stats(self, char_col, tgt_table, stats):
        self.stdout.write(
            f'  [{char_col:<16} -> {tgt_table:<12}] '
            f'non_vide={stats["non_empty"]:>5}  '
            f'distinct={stats["distinct"]:>4}  '
            f'num={stats["numeric"]:>5}  '
            f'nom={stats["non_numeric"]:>5}  '
            f'resolu_PK={stats["resolved_by_id"]:>5}  '
            f'resolu_nom={stats["resolved_by_name"]:>5}  '
            f'non_resolu={stats["unresolvable"]:>5}'
        )

    # ────────────────────────────────────────────────────────────────────
    def _audit_pointage_multidept(self):
        """Distribution du nombre de tokens dans suivi_suivie_pointage.id_departement."""
        self.cur.execute(
            "SELECT id_departement, annee_universitaire FROM suivi_suivie_pointage "
            "WHERE id_departement IS NOT NULL AND id_departement != ''"
        )
        distrib = Counter()
        sample_multi = []
        unresolved_tokens = []

        # Pre-charger le cache departement (id, nom, annee)
        self.cur.execute("SELECT id, nom, annee_universitaire FROM departement")
        dept_rows = self.cur.fetchall()
        dept_ids = {row[0] for row in dept_rows}
        dept_by_nom_year = {(row[1], row[2]) for row in dept_rows}

        # Re-executer la requete source
        self.cur.execute(
            "SELECT id_departement, annee_universitaire FROM suivi_suivie_pointage "
            "WHERE id_departement IS NOT NULL AND id_departement != ''"
        )
        for raw, year in self.cur.fetchall():
            tokens = [t.strip() for t in (raw or '').split('/') if t.strip()]
            distrib[len(tokens)] += 1
            if len(tokens) >= 2 and len(sample_multi) < 10:
                sample_multi.append((raw, tokens))
            for t in tokens:
                if t.isdigit():
                    if int(t) not in dept_ids and (t, year) not in unresolved_tokens:
                        if len(unresolved_tokens) < 50:
                            unresolved_tokens.append((t, year))
                else:
                    if (t, year) not in dept_by_nom_year:
                        if (t, year) not in unresolved_tokens and len(unresolved_tokens) < 50:
                            unresolved_tokens.append((t, year))
        return distrib, sample_multi, unresolved_tokens

    # ────────────────────────────────────────────────────────────────────
    def _write_csv(self, rows):
        Path(self.csv_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.csv_path, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=[
                'source_table', 'charfield', 'target',
                'non_empty', 'distinct', 'numeric', 'non_numeric',
                'resolved_by_id', 'resolved_by_name', 'unresolvable',
            ])
            w.writeheader()
            for r in rows:
                w.writerow(r)
