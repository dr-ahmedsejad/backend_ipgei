"""
Analyse chaque migration Django non appliquee et propose une decision :
  FAKE      : DDL deja en place (CreateModel d'une table existante,
              AddField d'une colonne existante, SeparateDatabaseAndState pur)
  APPLY     : Schema reellement absent (CreateModel d'une nouvelle table,
              AddField d'une nouvelle colonne)
  MIXED     : Mix de FAKE + APPLY → besoin d'une intervention manuelle
  REVIEW    : Operations RunPython/RunSQL → revue humaine necessaire

Strictement READ-ONLY. Aucune modification. Produit un rapport Markdown.

Usage :
    python manage.py analyze_migrations --report backups/migration_plan.md
"""
from __future__ import annotations
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import (
    CreateModel, DeleteModel, AddField, RemoveField, AlterField,
    RenameField, AlterModelTable, RunPython, RunSQL,
    SeparateDatabaseAndState, AddIndex, RemoveIndex, AlterUniqueTogether,
    AlterIndexTogether, AlterModelOptions, RenameModel, AlterOrderWithRespectTo,
)


class Command(BaseCommand):
    help = 'Analyse les migrations non appliquees et propose une decision.'

    def add_arguments(self, parser):
        parser.add_argument('--report', type=str, default='', help='Fichier Markdown de sortie.')

    def handle(self, *args, **opts):
        cur = connection.cursor()
        loader = MigrationLoader(connection)

        # Schema actuel : tables + colonnes
        cur.execute("""
            SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
        """)
        cols_by_table = defaultdict(set)
        for t, c in cur.fetchall():
            cols_by_table[t].add(c)
        existing_tables = set(cols_by_table.keys())

        # Migrations non appliquees
        applied = loader.applied_migrations
        unapplied = []
        for key in loader.graph.nodes:
            if key not in applied:
                node = loader.graph.nodes[key]
                if node:
                    unapplied.append((key, node))

        # Trier dans l'ordre du graphe pour respecter les dependances
        # (forwards plan depuis l'etat applique vers les feuilles)
        try:
            plan_nodes = loader.graph._generate_plan(loader.graph.leaf_nodes(), True)
            order_index = {key: i for i, (key, _) in enumerate(plan_nodes)}
        except Exception:
            order_index = {}
        unapplied.sort(key=lambda x: order_index.get(x[0], 99999))

        # Analyse
        rows = []  # liste de dicts pour le rapport
        for key, migration in unapplied:
            app, name = key
            decision, ops_summary, notes = self._classify(migration, existing_tables, cols_by_table)
            rows.append({
                'app':      app,
                'name':     name,
                'decision': decision,
                'ops':      ops_summary,
                'notes':    notes,
            })

        # Affichage console
        self.stdout.write(self.style.MIGRATE_HEADING(f'\n=== {len(rows)} migrations non appliquees ==='))
        by_decision = defaultdict(int)
        for r in rows:
            by_decision[r['decision']] += 1
        for d, n in sorted(by_decision.items()):
            self.stdout.write(f'  {d:<8} : {n}')

        # Markdown
        lines = ['# Plan d\'application des migrations Django', '']
        lines.append(f'Total : {len(rows)} migrations non appliquees.')
        lines.append('')
        lines.append('Categories :')
        lines.append('- **FAKE** : DDL deja en place, marquer applique sans executer SQL (`migrate <app> <num> --fake`).')
        lines.append('- **APPLY** : Schema reellement absent, executer la migration normalement.')
        lines.append('- **MIXED** : Operations mixtes — analyser au cas par cas.')
        lines.append('- **REVIEW** : Contient RunPython/RunSQL — revue humaine.')
        lines.append('')
        lines.append('| App | Migration | Decision | Operations | Notes |')
        lines.append('|-----|-----------|----------|------------|-------|')
        for r in rows:
            n = r['notes'].replace('|', '\\|')
            ops = r['ops'].replace('|', '\\|')
            lines.append(f"| {r['app']} | `{r['name']}` | **{r['decision']}** | {ops} | {n} |")

        # Plan d'execution suggere
        lines.append('\n## Plan d\'execution suggere\n')
        lines.append('### Groupe 1 — FAKE (schema deja en place)')
        for r in rows:
            if r['decision'] == 'FAKE':
                lines.append(f'- `python manage.py migrate {r["app"]} {r["name"][:4]} --fake`')
        lines.append('\n### Groupe 2 — APPLY (nouvelles tables)')
        for r in rows:
            if r['decision'] == 'APPLY':
                lines.append(f'- `python manage.py migrate {r["app"]} {r["name"][:4]}`')
        lines.append('\n### Groupe 3 — MIXED (revue manuelle)')
        for r in rows:
            if r['decision'] == 'MIXED':
                lines.append(f'- `{r["app"]}.{r["name"]}` : {r["notes"]}')
        lines.append('\n### Groupe 4 — REVIEW (RunPython/RunSQL — verifier la logique)')
        for r in rows:
            if r['decision'] == 'REVIEW':
                lines.append(f'- `{r["app"]}.{r["name"]}` : {r["notes"]}')

        if opts['report']:
            Path(opts['report']).parent.mkdir(parents=True, exist_ok=True)
            Path(opts['report']).write_text('\n'.join(lines), encoding='utf-8')
            self.stdout.write(self.style.SUCCESS(f'\nRapport ecrit : {opts["report"]}'))

    # ────────────────────────────────────────────────────────────────────
    def _classify(self, migration, existing_tables, cols_by_table):
        """Retourne (decision, ops_summary, notes)."""
        ops_summary_parts = []
        per_op_decisions = []
        notes = []

        for op in migration.operations:
            d, summary = self._classify_op(op, existing_tables, cols_by_table, notes)
            per_op_decisions.append(d)
            ops_summary_parts.append(summary)

        ops_summary = ', '.join(ops_summary_parts)
        unique_decisions = set(per_op_decisions)

        if not per_op_decisions:
            return 'FAKE', '(aucune op)', '; '.join(notes) or 'migration vide'

        if unique_decisions == {'FAKE'}:
            decision = 'FAKE'
        elif unique_decisions == {'APPLY'}:
            decision = 'APPLY'
        elif 'REVIEW' in unique_decisions:
            decision = 'REVIEW'
        elif unique_decisions == {'FAKE', 'APPLY'} or 'MIXED' in unique_decisions:
            decision = 'MIXED'
        else:
            decision = 'MIXED'

        return decision, ops_summary, '; '.join(notes) or '-'

    # ────────────────────────────────────────────────────────────────────
    def _classify_op(self, op, existing_tables, cols_by_table, notes):
        """Decision pour UNE operation : ('FAKE'|'APPLY'|'REVIEW', summary)."""
        cls = op.__class__.__name__

        if isinstance(op, SeparateDatabaseAndState):
            # state-only si database_operations vide
            db_ops = list(getattr(op, 'database_operations', []) or [])
            if not db_ops:
                return 'FAKE', f'{cls}(state-only)'
            # Sinon recurse sur les db_operations
            sub_decisions = []
            for sub in db_ops:
                d, _ = self._classify_op(sub, existing_tables, cols_by_table, notes)
                sub_decisions.append(d)
            if set(sub_decisions) == {'FAKE'}:
                return 'FAKE', f'{cls}(db:FAKE)'
            return 'MIXED', f'{cls}(db:{",".join(sub_decisions)})'

        if isinstance(op, CreateModel):
            db_table = op.options.get('db_table') if op.options else None
            tname = db_table or f'{op.options.get("app_label","")}_{op.name.lower()}'
            # Heuristique : si la table n'existe pas, APPLY ; sinon FAKE
            if tname in existing_tables:
                return 'FAKE', f'CreateModel({op.name},exists)'
            # Verifier aussi avec convention "<app>_<modelname>"
            return 'APPLY', f'CreateModel({op.name})'

        if isinstance(op, DeleteModel):
            return 'APPLY', f'DeleteModel({op.name})'

        if isinstance(op, AddField):
            # Trouver la table
            tbl = self._guess_table(op.model_name, existing_tables, cols_by_table)
            col = self._field_column(op.field, op.name)
            if tbl and col in cols_by_table.get(tbl, set()):
                return 'FAKE', f'AddField({op.model_name}.{op.name})'
            return 'APPLY', f'AddField({op.model_name}.{op.name})'

        if isinstance(op, RemoveField):
            tbl = self._guess_table(op.model_name, existing_tables, cols_by_table)
            # On ne peut pas savoir le nom de colonne sans field_object — heuristique
            # Si la colonne existe encore, APPLY (drop). Sinon FAKE.
            # Default DD: utiliser op.name + '_id' pour FK
            possible = {op.name, op.name + '_id'}
            present = bool(possible & cols_by_table.get(tbl or '', set()))
            return ('APPLY' if present else 'FAKE'), f'RemoveField({op.model_name}.{op.name})'

        if isinstance(op, (AlterField, RenameField, AlterModelTable, AlterUniqueTogether,
                           AlterIndexTogether, AlterModelOptions, RenameModel,
                           AlterOrderWithRespectTo, AddIndex, RemoveIndex)):
            # Toutes ces alterations sont generalement deja faites ou sans effet
            # sur le contenu data. On considere FAKE par defaut (DDL aura ete fait
            # ou est cosmetique cote Django state).
            notes.append(f'cosmetique: {cls}')
            return 'FAKE', cls

        if isinstance(op, RunPython):
            # Data migration → revue humaine
            return 'REVIEW', f'RunPython({op.code.__name__ if hasattr(op.code, "__name__") else "?"})'

        if isinstance(op, RunSQL):
            return 'REVIEW', 'RunSQL'

        return 'REVIEW', cls

    def _guess_table(self, model_name, existing_tables, cols_by_table):
        """Devine le db_table d'un model_name (heuristique : 'app_modelname' ou modelname)."""
        candidates = [model_name, f'parametres_{model_name}', f'{model_name}']
        for c in candidates:
            if c in existing_tables:
                return c
        # Recherche fuzzy
        for t in existing_tables:
            if t.endswith('_' + model_name) or t == model_name:
                return t
        return None

    def _field_column(self, field, fallback_name):
        """Retourne le nom de colonne d'un Django Field."""
        try:
            db_col = getattr(field, 'db_column', None)
            if db_col:
                return db_col
            # FK / OneToOne ajoutent _id
            from django.db.models import ForeignKey, OneToOneField
            if isinstance(field, (ForeignKey, OneToOneField)):
                return f'{fallback_name}_id'
            return fallback_name
        except Exception:
            return fallback_name
