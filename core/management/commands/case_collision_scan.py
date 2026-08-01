"""
Scan pré-migration PostgreSQL — collisions casse/accents sur les uniques texte.

Contexte : sous MySQL, les index UNIQUE en collation *_ci sont insensibles à la
casse (et aux accents en *_ai_ci) ; sous PostgreSQL, UNIQUE est strictement
binaire. Deux valeurs comme 'Amphi A' / 'AMPHI a' ou 'Département' /
'Departement' peuvent donc cohabiter (ou pas) selon le moteur : ce scan détecte
toute paire de lignes dont les valeurs deviennent identiques après
normalisation (casefold + suppression des accents), afin de fiabiliser la
bascule vers un index unique fonctionnel (lower/unaccent ou citext) côté PG.

STRICTEMENT READ-ONLY : uniquement des SELECT via .values_list().iterator().
Vendor-neutre : la normalisation est faite en PYTHON (unicodedata + casefold),
jamais en SQL — la commande marche à l'identique sur mysql, sqlite, postgresql.

Les cibles sont construites DYNAMIQUEMENT par introspection du registre Django
(apps.get_models() + _meta) — aucune liste codée en dur :
  (a) chaque champ texte (Char/Email/Slug/TextField) avec unique=True,
      y compris CustomUser.username / CustomUser.email ;
  (b) chaque unique_together / UniqueConstraint contenant au moins un champ
      texte : la clé normalisée = tuple (champs texte normalisés, autres
      champs tels quels).

Usage :
    python manage.py case_collision_scan
    python manage.py case_collision_scan --json rapport_collisions.json

Exit code : 0 si aucune collision, != 0 (CommandError) sinon.
"""
import json
import unicodedata
from collections import defaultdict

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, models

#: Types de champ texte concernés (EmailField/SlugField héritent de CharField).
TEXT_FIELD_TYPES = (models.CharField, models.TextField)


def normaliser(valeur):
    """Normalise une valeur texte comme le ferait une collation MySQL *_ai_ci :
    décomposition NFKD + suppression des diacritiques + casefold (casse)."""
    decompose = unicodedata.normalize('NFKD', str(valeur))
    sans_accents = ''.join(c for c in decompose if not unicodedata.combining(c))
    return sans_accents.casefold()


class Command(BaseCommand):
    help = (
        "Scan READ-ONLY des collisions casse/accents sur les champs texte "
        "uniques et les contraintes composites à composante texte "
        "(pré-migration MySQL *_ci -> PostgreSQL)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--json', type=str, default=None, metavar='FICHIER',
            help="Écrit un rapport machine-readable (JSON, UTF-8) dans FICHIER.",
        )

    # ── Construction dynamique des cibles ────────────────────────────────────
    def _collecter_cibles(self):
        """Retourne une liste de cibles {model, fields, text_fields, kind}.

        Aucune liste codée en dur : tout modèle ajouté au registre avec un
        unique texte ou une contrainte composite texte sera scanné.
        """
        cibles = []
        self._cibles_non_resolues = []   # composites dont un champ est introuvable
        for model in apps.get_models():
            meta = model._meta

            # (a) champs texte unique=True
            for field in meta.concrete_fields:
                if isinstance(field, TEXT_FIELD_TYPES) and field.unique:
                    cibles.append({
                        'model': model,
                        'fields': (field.name,),
                        'text_fields': {field.name},
                        'kind': 'unique',
                    })

            # (b) unique_together + UniqueConstraint (avec champs nommés,
            #     sans condition — une contrainte partielle serait un faux
            #     positif si scannée globalement)
            groupes = [tuple(ut) for ut in meta.unique_together]
            for contrainte in meta.constraints:
                if (isinstance(contrainte, models.UniqueConstraint)
                        and contrainte.fields
                        and contrainte.condition is None):
                    groupes.append(tuple(contrainte.fields))

            for groupe in groupes:
                try:
                    champs = [meta.get_field(nom) for nom in groupe]
                except Exception as exc:  # expression / attname / champ exotique
                    # Ne pas avaler silencieusement : journaliser pour que la
                    # cible apparaisse dans le rapport (targets_skipped).
                    self._cibles_non_resolues.append({
                        'model': meta.label,
                        'fields': list(groupe),
                        'erreur': 'champ non résolu : %s' % exc,
                    })
                    continue
                texte = {f.name for f in champs
                         if isinstance(f, TEXT_FIELD_TYPES)}
                if texte:
                    cibles.append({
                        'model': model,
                        'fields': groupe,
                        'text_fields': texte,
                        'kind': 'composite',
                    })
        return cibles

    # ── Scan d'une cible (SELECT seulement) ──────────────────────────────────
    def _scanner_cible(self, cible):
        """Itère les lignes en lecture seule et regroupe par clé normalisée.

        Retourne la liste des collisions : [(cle_normalisee, [(pk, brut), ...])].
        """
        model = cible['model']
        fields = cible['fields']
        text_fields = cible['text_fields']

        seaux = defaultdict(list)
        qs = (model._base_manager
              .values_list('pk', *fields)
              .order_by('pk')
              .iterator(chunk_size=2000))
        for ligne in qs:
            pk, valeurs = ligne[0], ligne[1:]
            # NULL n'entre jamais en collision (NULLs distincts en MySQL
            # comme en PostgreSQL) : on ignore la ligne.
            if any(v is None for v in valeurs):
                continue
            cle = tuple(
                normaliser(v) if nom in text_fields else v
                for nom, v in zip(fields, valeurs)
            )
            brut = valeurs[0] if len(valeurs) == 1 else valeurs
            seaux[cle[0] if len(cle) == 1 else cle].append((pk, brut))

        return [(cle, lignes) for cle, lignes in seaux.items() if len(lignes) > 1]

    # ── Sérialisation JSON-safe (dates, Decimal, FK id...) ───────────────────
    @staticmethod
    def _json_safe(valeur):
        if isinstance(valeur, (list, tuple)):
            return [Command._json_safe(v) for v in valeur]
        if valeur is None or isinstance(valeur, (str, int, float, bool)):
            return valeur
        return str(valeur)

    # ── Point d'entrée ───────────────────────────────────────────────────────
    def handle(self, *args, **opts):
        cibles = self._collecter_cibles()

        collisions = []      # entrées détaillées pour affichage + JSON
        # cibles dont la table est absente (managed=False...) OU dont un champ
        # composite n'a pas pu être résolu (attname, expression) — collectées
        # dans _collecter_cibles ci-dessus.
        ignorees = list(getattr(self, '_cibles_non_resolues', []))
        nb_scannees = 0

        for cible in cibles:
            model = cible['model']
            etiquette = model._meta.label            # ex. 'salle.Salle'
            try:
                trouvees = self._scanner_cible(cible)
                nb_scannees += 1
            except DatabaseError as exc:
                # Modèles managed=False dont la table peut être absente
                # (ex. prof_type_history, suivi_pointage_departements) — on
                # protège sans échouer : le scan reste read-only et best-effort.
                ignorees.append({'model': etiquette,
                                 'fields': list(cible['fields']),
                                 'erreur': str(exc)})
                continue

            for cle, lignes in trouvees:
                collisions.append({
                    'model': etiquette,
                    'fields': list(cible['fields']),
                    'kind': cible['kind'],
                    'normalized': self._json_safe(cle),
                    'rows': [[pk, self._json_safe(brut)] for pk, brut in lignes],
                })

        # ── Affichage lisible ────────────────────────────────────────────────
        for coll in collisions:
            self.stdout.write(self.style.ERROR(
                "COLLISION %s (%s) — clé normalisée : %r" % (
                    coll['model'], ', '.join(coll['fields']), coll['normalized'])
            ))
            for pk, brut in coll['rows']:
                self.stdout.write("    pk=%s  %r" % (pk, brut))

        total = len(collisions)
        for ign in ignorees:
            self.stdout.write(self.style.WARNING(
                "IGNORÉ %s (%s) : table inaccessible (%s)" % (
                    ign['model'], ', '.join(ign['fields']), ign['erreur'])
            ))

        # ── Rapport JSON optionnel (écrit AVANT l'exit non-zéro) ─────────────
        chemin_json = opts.get('json')
        if chemin_json:
            rapport = {
                'targets_total': len(cibles),
                'targets_scanned': nb_scannees,
                'targets_skipped': ignorees,
                'total_collisions': total,
                'collisions': collisions,
            }
            with open(chemin_json, 'w', encoding='utf-8') as fh:
                json.dump(rapport, fh, ensure_ascii=False, indent=2)
            self.stdout.write("Rapport JSON écrit : %s" % chemin_json)

        # ── Résumé + exit code ───────────────────────────────────────────────
        if total:
            self.stdout.write(self.style.ERROR(
                "Total : %d collision(s) casse/accents sur %d cible(s) scannée(s)."
                % (total, nb_scannees)
            ))
            raise CommandError(
                "%d collision(s) casse/accents détectée(s) — à résoudre avant "
                "la migration PostgreSQL." % total
            )

        self.stdout.write(self.style.SUCCESS(
            "Aucune collision détectée (%d cible(s) scannée(s), %d ignorée(s))."
            % (nb_scannees, len(ignorees))
        ))
