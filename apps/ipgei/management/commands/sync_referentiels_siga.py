"""
Recopie les référentiels de planification de SIGA (`siga_pg`) dans IPGEI :
créneaux horaires, types de séance et, à la demande, salles.

    python manage.py sync_referentiels_siga [--dry-run] [--exclusif] [--avec-salles]

Pourquoi une copie plutôt qu'une lecture à la volée : les séances IPGEI portent
une clé étrangère vers `parametres.Creneau`. PostgreSQL ne sait pas référencer
une table d'une autre base, donc les créneaux DOIVENT exister dans `IPGEI`.
Cette commande en fait un **miroir** : SIGA reste la source de vérité, IPGEI ne
fait que refléter.

Garantie d'innocuité : la connexion à `siga_pg` est ouverte en `read_only` et ne
contient que des `SELECT`. Aucune écriture n'est possible sur la base de SIGA,
même par erreur de programmation — PostgreSQL rejetterait la transaction.
"""
import unicodedata

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.parametres.models import Creneau, Seance
from apps.salle.models import Salle

# Base SIGA à lire. Volontairement en dur plutôt que configurable : cette
# commande n'a de sens que pour ce couple de bases, et un paramètre libre
# inviterait à la pointer ailleurs.
BASE_SIGA = 'siga_pg'

REQUETE_CRENEAUX = """
    SELECT creneau, duree, ordre, is_actif
    FROM creneau
    ORDER BY ordre, creneau
"""

REQUETE_SEANCES = """
    SELECT type_seance, is_special
    FROM "Seance"
    ORDER BY type_seance
"""

REQUETE_SALLES = """
    SELECT nom, capacite
    FROM salle
    ORDER BY nom
"""


def _type_depuis_heure(libelle: str) -> str:
    """
    Déduit matin / après-midi / soir de l'heure de début.

    SIGA stocke `type_creneau = 'NORMAL'`, valeur absente des choix du modèle
    (reliquat historique). Plutôt que de recopier une donnée invalide, on la
    recalcule depuis le libellé, qui lui est fiable.
    """
    try:
        heure = int(libelle.strip()[:2])
    except (ValueError, IndexError):
        return 'matin'
    if heure < 12:
        return 'matin'
    return 'apres-midi' if heure < 18 else 'soir'


class Command(BaseCommand):
    help = "Miroir des créneaux horaires depuis la base SIGA (lecture seule)."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Affiche ce qui serait fait, sans rien écrire.')
        parser.add_argument('--exclusif', action='store_true',
                            help="Désactive les créneaux IPGEI absents de SIGA "
                                 "(ils ne sont jamais supprimés).")
        parser.add_argument('--avec-salles', action='store_true',
                            help="Reprend aussi les salles de SIGA. Hors de ce "
                                 "drapeau, les salles restent propres à IPGEI.")

    def _dire(self, message, style=None):
        """Console Windows non-UTF8 : on translittère plutôt que d'échouer."""
        try:
            self.stdout.write(style(message) if style else message)
        except UnicodeEncodeError:
            ascii_ = unicodedata.normalize('NFKD', message).encode('ascii', 'ignore').decode()
            self.stdout.write(style(ascii_) if style else ascii_)

    def handle(self, *args, **options):
        if settings.DATABASES['default']['NAME'].strip().lower() == BASE_SIGA:
            raise CommandError(
                'La base courante EST siga_pg — cette commande écrirait sur SIGA. Abandon.'
            )

        lignes   = self._lire_siga(REQUETE_CRENEAUX)
        seances  = self._lire_siga(REQUETE_SEANCES)
        salles   = self._lire_siga(REQUETE_SALLES) if options['avec_salles'] else []
        self._dire(f'{len(lignes)} créneau(x), {len(seances)} type(s) de séance'
                   + (f', {len(salles)} salle(s)' if options['avec_salles'] else '')
                   + f' lus dans {BASE_SIGA} (lecture seule).')

        if options['dry_run']:
            for libelle, duree, ordre, actif in lignes:
                self._dire(f'  [simulation] créneau {libelle} · {duree} h · ordre {ordre} · '
                           f'{"actif" if actif else "inactif"}')
            for type_seance, special in seances:
                self._dire(f'  [simulation] séance {type_seance}'
                           f'{" (spéciale)" if special else ""}')
            for nom, capacite in salles:
                self._dire(f'  [simulation] salle {nom} ({capacite} places)')
            return

        crees, majs, desactives = self._ecrire_ipgei(lignes, options['exclusif'])
        self._ecrire_seances(seances)
        if options['avec_salles']:
            self._ecrire_salles(salles)
        if desactives:
            self._dire(f'{desactives} créneau(x) propre(s) à IPGEI désactivé(s) '
                       '(conservés : des séances peuvent les référencer).')
        self._dire(
            f'Miroir à jour : {crees} créé(s), {majs} mis à jour, '
            f'{Creneau.objects.filter(is_actif=True).count()} actif(s) au total.',
            self.style.SUCCESS,
        )

    def _lire_siga(self, requete):
        """Ouvre `siga_pg` en lecture seule et exécute une requête de lecture."""
        cfg = settings.DATABASES['default']
        try:
            import psycopg
        except ImportError as exc:                      # pragma: no cover
            raise CommandError('psycopg est requis pour lire la base SIGA.') from exc

        try:
            with psycopg.connect(
                dbname=BASE_SIGA, user=cfg['USER'], password=cfg['PASSWORD'],
                host=cfg['HOST'], port=cfg['PORT'],
            ) as conn:
                # Verrou au niveau de la session : toute écriture serait refusée
                # par PostgreSQL lui-même, pas seulement par convention.
                conn.read_only = True
                with conn.cursor() as cur:
                    cur.execute(requete)
                    return cur.fetchall()
        except psycopg.Error as exc:
            raise CommandError(f'Lecture de {BASE_SIGA} impossible : {exc}') from exc

    @transaction.atomic
    def _ecrire_seances(self, lignes):
        """
        Types de séance (CM, TD, TP, DS…).

        Sans eux, la colonne « Type séance » de l'écran « Gérer les emplois »
        reste vide et aucune séance ne peut être enregistrée. `is_special`
        distingue les types qui occupent un créneau sans prof ni salle (Sport,
        Instruction militaire).
        """
        for type_seance, special in lignes:
            Seance.objects.update_or_create(
                type_seance=type_seance, defaults={'is_special': bool(special)},
            )
        self._dire(f'{Seance.objects.count()} type(s) de séance disponibles.')

    @transaction.atomic
    def _ecrire_salles(self, lignes):
        """
        Salles.

        Reprises seulement sur demande explicite : les salles sont un bien
        physique propre à chaque établissement, et une capacité erronée fausse
        les affectations. La capacité existante n'est jamais écrasée.
        """
        crees = 0
        for nom, capacite in lignes:
            _, cree = Salle.objects.get_or_create(
                nom=nom, defaults={'capacite': capacite or 0},
            )
            crees += int(cree)
        self._dire(f'{crees} salle(s) ajoutée(s) — {Salle.objects.count()} au total.')

    @transaction.atomic
    def _ecrire_ipgei(self, lignes, exclusif=False):
        """
        Aligne les créneaux IPGEI sur ceux de SIGA.

        Le libellé sert de clé : c'est lui qui identifie un créneau pour un
        utilisateur, et il est unique côté modèle. Les créneaux IPGEI absents de
        SIGA sont laissés en place — des séances peuvent déjà les référencer,
        les supprimer casserait des emplois du temps existants.

        La **durée n'est jamais réécrite** sur un créneau existant : elle se
        saisit dans IPGEI, et une resynchronisation ne doit pas effacer ce
        réglage.
        """
        crees = majs = desactives = 0
        libelles_siga = {l[0] for l in lignes}
        for libelle, duree, ordre, actif in lignes:
            # La duree est une donnee SAISIE par creneau cote IPGEI : la
            # resynchronisation ne doit donc pas ecraser ce qui a ete renseigne.
            # SIGA ne sert que de valeur initiale, a la creation.
            existant = Creneau.objects.filter(creneau=libelle).first()
            if existant is None:
                Creneau.objects.create(
                    creneau=libelle,
                    duree=float(duree),
                    ordre=int(ordre),
                    is_actif=bool(actif),
                    type_creneau=_type_depuis_heure(libelle),
                )
                crees += 1
            else:
                existant.ordre        = int(ordre)
                existant.is_actif     = bool(actif)
                existant.type_creneau = _type_depuis_heure(libelle)
                existant.save(update_fields=['ordre', 'is_actif', 'type_creneau'])
                majs += 1

        if exclusif:
            # Désactivation et non suppression : un créneau déjà utilisé par une
            # séance ne doit pas disparaître, sous peine de casser des emplois du
            # temps existants. Désactivé, il sort simplement de la grille.
            desactives = (
                Creneau.objects.filter(is_actif=True)
                .exclude(creneau__in=libelles_siga)
                .update(is_actif=False)
            )
        return crees, majs, desactives
