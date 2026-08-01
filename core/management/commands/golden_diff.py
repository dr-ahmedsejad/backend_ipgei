"""
golden_diff — compare deux extraits produits par golden_extract.

Usage :
    python manage.py golden_diff mysql.json pg.json
    python manage.py golden_diff mysql.json pg.json --max-prints 200
    python manage.py golden_diff mysql.json pg.json --allow-order-diff

Comportement :
  - deep-diff récursif ; chaque delta est imprimé avec son chemin de clés
    naturelles (ex: invariants.resultats_persistes.MAT001|S1|2025-2026|TEST…) ;
  - pour les listes de hashes (digests sha256_sorted) : imprime les hashes
    présents d'un seul côté, plus les counts ;
  - les clés 'label' et '_meta' sont ignorées (seules sections horodatées) ;
    exception : si '_meta.moteur' est IDENTIQUE dans les deux fichiers, un
    warning est émis (« deux extracts du même moteur ? ») sans changer l'exit ;
  - les écarts de 'order_signatures' sont rapportés dans une section séparée
    « ÉCARTS D'ORDRE (collation) » : ce sont les seuls écarts ATTENDUS entre
    MySQL (utf8mb4_*_ci) et PostgreSQL (ICU fr-FR) — tri linguistique proche
    mais égalité stricte d'ordre non garantie. Par défaut le gate reste STRICT
    (exit ≠ 0) ; --allow-order-diff les rétrograde en warnings (exit 0 si ce
    sont les SEULS écarts) — à n'utiliser qu'après examen humain un par un,
    validation documentée (cf. docs/migration_postgres.md, Phase 5) ;
  - entrées 'skipped' (digest de table ou signature d'ordre sautés côté
    extract) : elles sont toujours IMPRIMÉES clairement. Si les skips sont
    STRICTEMENT identiques des deux côtés → simple warning (le diff reste
    aveugle sur ces tables, mais symétriquement : non bloquant). Sinon →
    écart bloquant (exit ≠ 0) : un côté a digéré ce que l'autre a sauté,
    la comparaison est asymétrique donc invalide ;
  - exit ≠ 0 (CommandError) au moindre écart bloquant ;
    sinon « GOLDEN DIFF: OK (0 écart) ».
"""
import json
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

#: Clés racine ignorées par le diff (métadonnées du run, jamais des données).
CLES_IGNOREES = ('label', '_meta')


def _chemin(path):
    """Formate un chemin de clés naturelles pour l'affichage."""
    return '.'.join(path) if path else '<racine>'


def _tronque(valeur, maxi=200):
    """Représentation courte d'une valeur pour l'affichage d'un delta."""
    r = repr(valeur)
    return r if len(r) <= maxi else r[:maxi] + '…'


def deep_diff(a, b, path=None, deltas=None):
    """Deep-diff récursif de deux structures JSON. Retourne la liste des deltas
    (chaînes prêtes à imprimer). Importable par les tests."""
    if path is None:
        path = []
    if deltas is None:
        deltas = []

    if isinstance(a, dict) and isinstance(b, dict):
        cles_a, cles_b = set(a), set(b)
        for k in sorted(cles_a - cles_b):
            deltas.append(f'{_chemin(path + [str(k)])} : présent uniquement dans A')
        for k in sorted(cles_b - cles_a):
            deltas.append(f'{_chemin(path + [str(k)])} : présent uniquement dans B')
        for k in sorted(cles_a & cles_b):
            deep_diff(a[k], b[k], path + [str(k)], deltas)
        return deltas

    if isinstance(a, list) and isinstance(b, list):
        if a == b:
            return deltas
        # Liste de hashes / de chaînes : comparaison ensembliste (multiset),
        # on imprime ce qui n'existe que d'un côté + les counts.
        if all(isinstance(x, str) for x in a) and all(isinstance(x, str) for x in b):
            ca, cb = Counter(a), Counter(b)
            seulement_a = sorted((ca - cb).elements())
            seulement_b = sorted((cb - ca).elements())
            deltas.append(
                f'{_chemin(path)} : listes différentes — counts A={len(a)} / B={len(b)}, '
                f'uniquement A={len(seulement_a)}, uniquement B={len(seulement_b)}'
            )
            for h in seulement_a:
                deltas.append(f'{_chemin(path)} : uniquement dans A → {h}')
            for h in seulement_b:
                deltas.append(f'{_chemin(path)} : uniquement dans B → {h}')
            return deltas
        # Liste structurée : longueurs puis élément par élément.
        if len(a) != len(b):
            deltas.append(
                f'{_chemin(path)} : longueurs différentes — A={len(a)} / B={len(b)}'
            )
        for i in range(min(len(a), len(b))):
            deep_diff(a[i], b[i], path + [f'[{i}]'], deltas)
        return deltas

    if a != b or type(a) is not type(b):
        deltas.append(f'{_chemin(path)} : A={_tronque(a)} != B={_tronque(b)}')
    return deltas


#: Sections susceptibles de contenir des entrées 'skipped' (cf. golden_extract).
SECTIONS_SKIPPABLES = ('digests', 'order_signatures')


def _extraire_skips(extrait):
    """Retire de `extrait` (mutation) les entrées marquées 'skipped' des
    sections digests / order_signatures et les retourne sous forme de dict
    {(section, nom): entrée}. Les entrées skipped sont ainsi comparées par la
    règle dédiée (identiques des deux côtés = warning, sinon écart bloquant)
    et non par le deep-diff générique."""
    skips = {}
    if not isinstance(extrait, dict):
        return skips
    for section in SECTIONS_SKIPPABLES:
        entrees = extrait.get(section)
        if not isinstance(entrees, dict):
            continue
        for nom in [n for n, v in entrees.items()
                    if isinstance(v, dict) and v.get('skipped')]:
            skips[(section, nom)] = entrees.pop(nom)
    return skips


class Command(BaseCommand):
    help = ("Deep-diff de deux extraits golden_extract. Exit != 0 au moindre écart. "
            "Ignore 'label' et '_meta'.")

    def add_arguments(self, parser):
        parser.add_argument('fichier_a', help='Extrait A (ex: mysql.json).')
        parser.add_argument('fichier_b', help='Extrait B (ex: pg.json).')
        parser.add_argument('--max-prints', type=int, default=50,
                            help='Nombre maximum de deltas imprimés (défaut 50).')
        parser.add_argument(
            '--allow-order-diff', action='store_true',
            help="Rétrograde les écarts de 'order_signatures' (collation) en "
                 "warnings : exit 0 s'ils sont les SEULS écarts. À n'utiliser "
                 "qu'après examen humain documenté (cf. docs/migration_postgres.md).",
        )

    def _moteur(self, extrait):
        """Lit '_meta.moteur' sans planter si absent."""
        if isinstance(extrait, dict) and isinstance(extrait.get('_meta'), dict):
            return extrait['_meta'].get('moteur')
        return None

    def handle(self, *args, **opts):
        try:
            with open(opts['fichier_a'], encoding='utf-8') as fh:
                a = json.load(fh)
            with open(opts['fichier_b'], encoding='utf-8') as fh:
                b = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f'Lecture des extraits impossible : {exc}')

        warnings = []

        # Garde-fou : deux extraits du MÊME moteur ne prouvent rien (erreur
        # d'opérateur courante) — on avertit sans changer l'exit code.
        moteur_a, moteur_b = self._moteur(a), self._moteur(b)
        if moteur_a and moteur_b and moteur_a == moteur_b:
            warnings.append(
                f"Les deux extraits proviennent du même moteur ({moteur_a!r}) — "
                "un golden_diff n'a de sens qu'entre MySQL et PostgreSQL."
            )

        for cle in CLES_IGNOREES:
            if isinstance(a, dict):
                a.pop(cle, None)
            if isinstance(b, dict):
                b.pop(cle, None)

        # ── Entrées 'skipped' : règle dédiée (avant tout deep_diff) ───────────
        skips_a = _extraire_skips(a)
        skips_b = _extraire_skips(b)
        skip_deltas = []          # bloquants (asymétrie sauté/digéré)
        for cle in sorted(set(skips_a) | set(skips_b), key=lambda c: (c[0], c[1])):
            section, nom = cle
            in_a, in_b = cle in skips_a, cle in skips_b
            if in_a and in_b:
                warnings.append(
                    f'{section}.{nom} : sauté des DEUX côtés — comparaison aveugle '
                    'sur cette entrée (symétrique, non bloquant).'
                )
            else:
                # Sauté d'un seul côté, digéré de l'autre → asymétrie. On retire
                # aussi l'entrée digérée pour éviter un delta deep_diff confus,
                # et on émet un message clair et BLOQUANT.
                sauté = 'A' if in_a else 'B'
                digéré = 'B' if in_a else 'A'
                (b if in_a else a).get(section, {}).pop(nom, None)
                skip_deltas.append(
                    f'{section}.{nom} : sauté côté {sauté} mais digéré côté '
                    f'{digéré} — comparaison asymétrique, invalide.'
                )

        # ── Écarts d'ordre (collation) isolés du reste ───────────────────────
        order_a = a.pop('order_signatures', {}) if isinstance(a, dict) else {}
        order_b = b.pop('order_signatures', {}) if isinstance(b, dict) else {}
        order_deltas = deep_diff(order_a, order_b)

        # ── Reste : écarts toujours bloquants ────────────────────────────────
        deltas = deep_diff(a, b) + skip_deltas

        allow_order = opts['allow_order_diff']
        bloquants = list(deltas)
        if not allow_order:
            bloquants += order_deltas

        max_prints = max(opts['max_prints'], 0)

        def _imprimer(entetes, lignes, style):
            if not lignes:
                return
            self.stdout.write(style(entetes))
            for ligne in lignes[:max_prints]:
                self.stdout.write(style(f'  {ligne}'))
            if len(lignes) > max_prints:
                self.stdout.write(self.style.WARNING(
                    f'  … sortie tronquée : {len(lignes) - max_prints} de plus '
                    f'non affiché(s) (total {len(lignes)}).'
                ))

        # Warnings (moteur, skips symétriques, écarts d'ordre tolérés)
        for w in warnings:
            self.stdout.write(self.style.WARNING(f'AVERTISSEMENT {w}'))
        if allow_order and order_deltas:
            _imprimer("ÉCARTS D'ORDRE (collation) — tolérés (--allow-order-diff) :",
                      order_deltas, self.style.WARNING)

        if not bloquants:
            self.stdout.write(self.style.SUCCESS('GOLDEN DIFF: OK (0 écart)'))
            return

        _imprimer('DELTAS bloquants :',
                  [d for d in bloquants if d not in order_deltas], self.style.ERROR)
        if not allow_order and order_deltas:
            _imprimer("ÉCARTS D'ORDRE (collation) — bloquants "
                      "(lever avec --allow-order-diff après examen) :",
                      order_deltas, self.style.ERROR)
        raise CommandError(f'GOLDEN DIFF: {len(bloquants)} écart(s) bloquant(s) détecté(s).')
