"""
Pre-check du backfill M2M `suivi_pointage_departements`.

OBJECTIF : Garantir AVANT toute ecriture qu'aucune ligne de
suivi_suivie_pointage ne perdra son lien departement lors de la
migration CharField -> M2M.

Algorithme :
  1. Pour chaque ligne de suivi_suivie_pointage avec id_departement non vide,
     splitter par '/' et resoudre chaque token vers
       SELECT id FROM departement WHERE nom = token AND annee_universitaire = sp.annee_universitaire
  2. Comptabiliser :
       - lignes single-dept    (1 token)
       - lignes multi-dept     (>1 token)
       - tokens resolus
       - tokens NON resolus    -> probleme
       - lignes orphelines     (au moins 1 token non resolu)
  3. Calculer le nombre attendu d'entrees dans la M2M.

Aucun INSERT. Aucune modification de schema. 100 % lecture.

Usage :
    python manage.py precheck_pointage_m2m
    python manage.py precheck_pointage_m2m --report backups/precheck.md
"""
from __future__ import annotations
from collections import Counter, defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = 'Dry-run du backfill M2M suivi_pointage_departements (lecture seule).'

    def add_arguments(self, parser):
        parser.add_argument('--report', type=str, default='', help='Fichier Markdown de rapport.')

    def handle(self, *args, **opts):
        cur = connection.cursor()
        report_lines = ['# Pre-check backfill M2M suivi_pointage_departements', '']

        # ── 1. Construire le cache departement (nom, annee) -> id ──────
        cur.execute("SELECT id, nom, annee_universitaire FROM departement")
        dept_cache = {}                                 # (nom, annee) -> id
        depts_by_year = defaultdict(set)                # annee -> {nom, ...}
        for did, nom, annee in cur.fetchall():
            if nom:
                dept_cache[(nom, annee)] = did
                depts_by_year[annee].add(nom)
        self.stdout.write(f'Cache departement charge : {len(dept_cache)} entrees ((nom, annee) uniques)')

        # ── 2. Lire toutes les lignes pointage avec id_departement non vide ──
        cur.execute(
            "SELECT id, id_departement, annee_universitaire "
            "FROM suivi_suivie_pointage "
            "WHERE id_departement IS NOT NULL AND id_departement != ''"
        )
        rows = cur.fetchall()
        total_lignes = len(rows)
        self.stdout.write(f'Lignes pointage a analyser  : {total_lignes}')

        # ── 3. Analyser chaque ligne ────────────────────────────────────
        single_dept_lines  = 0
        multi_dept_lines   = 0
        resolved_tokens    = 0
        unresolved_tokens  = 0
        m2m_pairs          = set()         # (pointage_id, dept_id) attendues
        orphan_lines       = []            # lignes avec >=1 token non resolu
        unresolved_samples = Counter()     # (token, annee) -> count

        for sp_id, raw_dept, annee in rows:
            tokens = [t.strip() for t in (raw_dept or '').split('/') if t.strip()]
            if not tokens:
                continue
            if len(tokens) == 1:
                single_dept_lines += 1
            else:
                multi_dept_lines += 1

            line_has_unresolved = False
            for tok in tokens:
                # Token numerique (cas peu probable mais on couvre)
                if tok.isdigit():
                    cur.execute("SELECT 1 FROM departement WHERE id = %s", [int(tok)])
                    if cur.fetchone():
                        m2m_pairs.add((sp_id, int(tok)))
                        resolved_tokens += 1
                    else:
                        unresolved_tokens += 1
                        unresolved_samples[(tok, annee)] += 1
                        line_has_unresolved = True
                    continue
                # Token texte (cas dominant)
                dept_id = dept_cache.get((tok, annee))
                if dept_id is not None:
                    m2m_pairs.add((sp_id, dept_id))
                    resolved_tokens += 1
                else:
                    unresolved_tokens += 1
                    unresolved_samples[(tok, annee)] += 1
                    line_has_unresolved = True

            if line_has_unresolved:
                orphan_lines.append((sp_id, raw_dept, annee))

        # ── 4. Synthese ─────────────────────────────────────────────────
        expected_pairs = resolved_tokens   # autant de paires que de tokens resolus
        # NB : `m2m_pairs` peut etre legerement < expected_pairs si la meme
        # paire (sp_id, dept_id) apparait deux fois (token duplique dans le
        # CharField). On reporte les deux pour transparence.

        self.stdout.write(self.style.MIGRATE_HEADING('\n=== Synthese ==='))
        self.stdout.write(f'  Lignes single-dept      : {single_dept_lines}')
        self.stdout.write(f'  Lignes multi-dept       : {multi_dept_lines}')
        self.stdout.write(f'  Tokens resolus          : {resolved_tokens}')
        self.stdout.write(f'  Tokens NON resolus      : {unresolved_tokens}')
        self.stdout.write(f'  Paires M2M attendues    : {len(m2m_pairs)} (uniques) / {expected_pairs} (avec doublons)')
        self.stdout.write(f'  Lignes orphelines       : {len(orphan_lines)}')

        report_lines.append(f'- Lignes single-dept : **{single_dept_lines}**')
        report_lines.append(f'- Lignes multi-dept  : **{multi_dept_lines}**')
        report_lines.append(f'- Tokens resolus     : **{resolved_tokens}**')
        report_lines.append(f'- Tokens non resolus : **{unresolved_tokens}**')
        report_lines.append(f'- Paires M2M uniques attendues : **{len(m2m_pairs)}**')
        report_lines.append(f'- Lignes orphelines (>=1 token non resolvable) : **{len(orphan_lines)}**')
        report_lines.append('')

        # ── 5. Echantillon des problemes ─────────────────────────────────
        if unresolved_samples:
            self.stdout.write(self.style.WARNING('\nTokens non resolvables (top 20) :'))
            report_lines.append('## Tokens non resolvables')
            report_lines.append('')
            report_lines.append('| Token | Annee | Occurrences |')
            report_lines.append('|-------|-------|-------------|')
            for (tok, annee), n in unresolved_samples.most_common(20):
                self.stdout.write(f'  "{tok}" (annee={annee}) -> {n} fois')
                report_lines.append(f'| `{tok}` | {annee} | {n} |')
            report_lines.append('')

        if orphan_lines:
            self.stdout.write(self.style.WARNING(f'\nLignes pointage orphelines (echantillon 10/{len(orphan_lines)}) :'))
            report_lines.append(f'## Lignes pointage orphelines (echantillon)')
            report_lines.append('')
            report_lines.append('| pointage_id | id_departement (raw) | annee |')
            report_lines.append('|-------------|----------------------|-------|')
            for sp_id, raw, annee in orphan_lines[:10]:
                self.stdout.write(f'  pointage_id={sp_id}  raw="{raw}"  annee={annee}')
                report_lines.append(f'| {sp_id} | `{raw}` | {annee} |')
            report_lines.append('')

        # ── 6. Decision ─────────────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING('\n=== Decision ==='))
        if unresolved_tokens == 0:
            self.stdout.write(self.style.SUCCESS('OK Backfill M2M peut s\'executer SANS PERTE.'))
            report_lines.append('## Decision')
            report_lines.append('')
            report_lines.append('**OK** Backfill M2M peut s\'executer sans aucune perte.')
        else:
            self.stdout.write(self.style.ERROR(
                f'STOP {unresolved_tokens} tokens non resolvables sur {len(orphan_lines)} lignes. '
                'Backfill INTERROMPU jusqu\'a resolution manuelle.'
            ))
            report_lines.append('## Decision')
            report_lines.append('')
            report_lines.append(
                f'**STOP** {unresolved_tokens} tokens non resolvables. '
                'Voir tableau ci-dessus.'
            )

        # ── 7. Verification croisee : comptage SQL pur ──────────────────
        self.stdout.write(self.style.MIGRATE_HEADING('\n=== Verification SQL croisee ==='))
        cur.execute("""
            SELECT COUNT(DISTINCT CONCAT(sp.id, '|', d.id))
            FROM suivi_suivie_pointage sp
            JOIN departement d
              ON d.annee_universitaire = sp.annee_universitaire
             AND FIND_IN_SET(d.nom, REPLACE(sp.id_departement, '/', ',')) > 0
            WHERE sp.id_departement IS NOT NULL AND sp.id_departement != ''
        """)
        sql_count = cur.fetchone()[0]
        self.stdout.write(f'  Comptage Python : {len(m2m_pairs)} paires uniques')
        self.stdout.write(f'  Comptage SQL    : {sql_count} paires uniques')
        if sql_count == len(m2m_pairs):
            self.stdout.write(self.style.SUCCESS('  -> Comptages coherents'))
            report_lines.append(f'\n## Verification croisee\n')
            report_lines.append(f'- Comptage Python : **{len(m2m_pairs)}**')
            report_lines.append(f'- Comptage SQL    : **{sql_count}**')
            report_lines.append(f'- Coherence : OK')
        else:
            self.stdout.write(self.style.WARNING(
                f'  ATTENTION Decalage : Python={len(m2m_pairs)} vs SQL={sql_count}'
            ))
            report_lines.append(f'\n## Verification croisee\n')
            report_lines.append(f'- Comptage Python : **{len(m2m_pairs)}**')
            report_lines.append(f'- Comptage SQL    : **{sql_count}**')
            report_lines.append(f'- ATTENTION Decalage detecte')

        # ── 8. Rapport ──────────────────────────────────────────────────
        if opts['report']:
            Path(opts['report']).parent.mkdir(parents=True, exist_ok=True)
            Path(opts['report']).write_text('\n'.join(report_lines), encoding='utf-8')
            self.stdout.write(self.style.SUCCESS(f'\nRapport ecrit : {opts["report"]}'))
