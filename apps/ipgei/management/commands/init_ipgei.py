"""
Initialise une base IPGEI vierge : institution principale, paramètres du
cursus, référentiels de calendrier (jours / créneaux) et maquette des matières.

    python manage.py init_ipgei --annee 2025-2026

Idempotent : relancer la commande ne duplique rien et n'écrase pas ce qui a
été personnalisé (matières existantes, pondérations ajustées…).
"""
import unicodedata
from datetime import date
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.ipgei.models import (
    Classe, Matiere, ParametresIPGEI, SemestreIPGEI,
    PCT_DEFAUT_AVEC_TP, PCT_DEFAUT_SANS_TP,
)
from apps.ipgei.services import generer_semaines
from apps.parametres.models import Creneau, Institution, Jour, Year

JOURS = ['Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi']

# Grille horaire de repli, utilisee seulement si `siga_pg` est injoignable.
# Identique a celle de SIGA : seances de 1h30, memes bornes horaires.
CRENEAUX = [
    ('08h00-09h30', 'matin',      1),
    ('09h45-11h15', 'matin',      2),
    ('11h30-13h00', 'matin',      3),
    ('15h10-16h40', 'apres-midi', 4),
    ('17h00-18h30', 'apres-midi', 5),
]

# Maquettes relevées sur les relevés de notes réels de l'IPGEI (2024-2025).
# Les coefficients sont vérifiés par le calcul — cumul imprimé / total des
# coefficients doit rendre la moyenne imprimée :
#   S1  49 coef  800,56 / 49 = 16,34      S3  28 coef   91,53 / 28 = 3,27
#   S2  49 coef  784,20 / 49 = 16,00      S4  21 coef  123,28 / 21 = 5,87
#
# La maquette est PAR SEMESTRE : S3 porte huit matières — dont un second cours
# de mathématiques et un second de physique — là où S4 n'en porte que six, avec
# d'autres coefficients. La caler sur le niveau perdrait cette distinction.
#
# `TP` reflète la colonne « Tpcolle » du relevé : elle n'est imprimée que pour
# les matières qui en comportent.
#
# (code, intitulé, coef, volume horaire indicatif, TP/colle ?)
MAQUETTE = {
    'S1': [
        ('MATH',  'Mathématiques',            14, 90, True),
        ('PHYS',  'Physique — Chimie',        12, 98, True),
        ('SI',    "Sciences de l'ingénierie",  5, 38, True),
        ('INFO',  'Informatique',              5, 30, True),
        ('ANGL',  'Anglais',                   3, 23, False),
        ('FRAN',  'Français',                 10, 23, False),
    ],
    'S2': [
        ('MATH',  'Mathématiques',            14, 90, False),
        ('PHYS',  'Physique — Chimie',        12, 98, True),
        ('SI',    "Sciences de l'ingénierie",  5, 38, False),
        ('INFO',  'Informatique',              5, 30, True),
        ('ANGL',  'Anglais',                   3, 23, False),
        ('FRAN',  'Français',                 10, 23, False),
    ],
    'S3': [
        ('MATH',  'Mathématiques',             4, 60, True),
        ('PHYS',  'Physique — Chimie',         5, 60, True),
        ('SI',    "Sciences de l'ingénierie",  2, 38, True),
        ('INFO',  'Informatique',              2, 30, True),
        ('ANGL',  'Anglais',                   2, 23, True),
        ('FRAN',  'Français',                  4, 23, True),
        ('PHYS2', 'Physique',                  4, 45, True),
        ('MATH2', 'Mathématiques 2',           5, 60, True),
    ],
    'S4': [
        ('MATH',  'Mathématiques',             4, 60, False),
        ('PHYS',  'Physique — Chimie',         4, 60, True),
        ('SI',    "Sciences de l'ingénierie",  3, 38, False),
        ('INFO',  'Informatique',              3, 30, False),
        ('ANGL',  'Anglais',                   3, 23, False),
        ('FRAN',  'Français',                  4, 23, False),
    ],
}
NIVEAU_DU_SEMESTRE = {'S1': 'MPSI', 'S2': 'MPSI', 'S3': 'MP', 'S4': 'MP'}



class Command(BaseCommand):
    help = "Initialise l'institution IPGEI, ses paramètres et sa maquette."

    def add_arguments(self, parser):
        parser.add_argument('--annee', default='', help='Année universitaire, ex. 2025-2026.')
        parser.add_argument('--sans-maquette', action='store_true',
                            help='N\'insère pas les matières indicatives.')
        parser.add_argument('--sans-classes', action='store_true',
                            help='Ne crée pas les classes MPSI A / MP A de démarrage.')

    def _dire(self, message, style=None):
        """
        Écrit sur la console en survivant à un terminal non-UTF8 (le cp1256 de
        Windows FR ne sait pas encoder « É »). On retombe alors sur une version
        translittérée plutôt que de faire échouer toute l'initialisation.
        """
        texte = style(message) if style else message
        try:
            self.stdout.write(texte)
        except UnicodeEncodeError:
            ascii_ = unicodedata.normalize('NFKD', message).encode('ascii', 'ignore').decode()
            self.stdout.write(style(ascii_) if style else ascii_)

    @transaction.atomic
    def handle(self, *args, **options):
        annee = options['annee'] or self._annee_courante()

        institution = self._institution()
        params = ParametresIPGEI.pour(institution)
        self._dire(
            f'Institution principale : {institution.acronyme} — {institution.nom_display}',
            self.style.SUCCESS,
        )
        self._dire(
            f'Paramètres : seuil {params.seuil_validation}, '
            f'plafond rattrapage {params.plafond_rattrapage or "aucun"}, '
            f'{params.nb_semaines_defaut} semaines par défaut.'
        )

        self._referentiels(annee)
        semestres = self._semestres(institution, annee, params.nb_semaines_defaut)

        if not options['sans_maquette']:
            self._matieres(institution)
        if not options['sans_classes']:
            self._classes(institution, annee)

        self._dire(
            f'IPGEI initialisé pour {annee} — {len(semestres)} semestre(s) prêts.',
            self.style.SUCCESS,
        )

    # ── Étapes ───────────────────────────────────────────────────────────────
    def _annee_courante(self) -> str:
        """L'année universitaire bascule à la rentrée : septembre fait foi."""
        today = date.today()
        debut = today.year if today.month >= 9 else today.year - 1
        return f'{debut}-{debut + 1}'

    def _institution(self) -> Institution:
        """
        IPGEI est l'établissement principal de cette base : c'est lui que
        `InstitutionScopedMixin` retient pour borner tous les querysets.
        """
        institution, cree = Institution.objects.get_or_create(
            acronyme='IPGEI',
            defaults={
                'nom':            "Institut Préparatoire aux Grandes Écoles d'Ingénieurs",
                'nom_fr':         "Institut Préparatoire aux Grandes Écoles d'Ingénieurs",
                'nom_complet_fr': "Institut Préparatoire aux Grandes Écoles d'Ingénieurs",
                'groupe_fr':      'Groupe Polytechnique',
                'type_etablissement': 'institut',
                'ville_fr':       'Nouakchott',
                'pays_fr':        'Mauritanie',
                'est_principale': True,
            },
        )
        if not institution.est_principale:
            # save() du modèle retire le drapeau des autres institutions : IPGEI
            # devient bien l'unique établissement principal.
            institution.est_principale = True
            institution.save(update_fields=['est_principale'])
            self._dire('IPGEI promu établissement principal.', self.style.WARNING)
        if cree:
            self._dire('Institution IPGEI créée.')
        return institution

    def _referentiels(self, annee):
        for libelle in JOURS:
            Jour.objects.get_or_create(jour=libelle)

        # La grille horaire est celle de SIGA : on la reflete plutot que d'en
        # inventer une, sinon les deux produits divergeraient des la premiere
        # rentree. Repli sur une grille par defaut si `siga_pg` est injoignable
        # (poste isole, demo), pour que l'installation reste possible.
        from apps.parametres.models import Seance
        if not (Creneau.objects.filter(is_actif=True).exists() and Seance.objects.exists()):
            try:
                call_command('sync_referentiels_siga', exclusif=True, avec_salles=True, verbosity=0)
                self._dire('Referentiels repris de SIGA : creneaux, types de seance, salles.')
            except CommandError as exc:
                self._dire(f'SIGA injoignable ({exc}) - grille horaire par defaut.',
                           self.style.WARNING)
                for libelle, type_creneau, ordre in CRENEAUX:
                    Creneau.objects.get_or_create(
                        creneau=libelle,
                        defaults={'type_creneau': type_creneau, 'ordre': ordre, 'duree': 1.5},
                    )

        # L'année universitaire du socle (`parametres.Year`) alimente le
        # sélecteur de l'écran de connexion et le contexte de session. Sans
        # elle, la liste déroulante du login reste vide.
        debut = int(annee.split('-')[0])
        Year.objects.get_or_create(
            annee=annee,
            defaults={
                'date_debut': date(debut, 9, 1),
                'date_fin':   date(debut + 1, 8, 31),
                'est_active': True,
            },
        )
        self._dire(
            f'Référentiels : {len(JOURS)} jours, '
            f'{Creneau.objects.filter(is_actif=True).count()} créneaux actifs, année {annee}.'
        )

    def _semestres(self, institution, annee, nb_semaines):
        """
        Quatre semestres bornés sur l'année universitaire. Les dates sont un
        point de départ raisonnable (rentrée septembre) à ajuster dans l'écran
        « Semestres » — ce sont elles qui pilotent la génération des semaines.
        """
        debut_annee = int(annee.split('-')[0])
        bornes = {
            'S1': (date(debut_annee, 9, 15),     date(debut_annee + 1, 1, 31)),
            'S2': (date(debut_annee + 1, 2, 1),  date(debut_annee + 1, 6, 30)),
            'S3': (date(debut_annee, 9, 15),     date(debut_annee + 1, 1, 31)),
            'S4': (date(debut_annee + 1, 2, 1),  date(debut_annee + 1, 6, 30)),
        }
        crees = []
        for code, (d1, d2) in bornes.items():
            semestre, cree = SemestreIPGEI.objects.get_or_create(
                institution=institution, code=code, annee_universitaire=annee,
                defaults={'date_debut': d1, 'date_fin': d2, 'nb_semaines': nb_semaines},
            )
            if cree:
                generer_semaines(semestre)
            crees.append(semestre)
        return crees

    def _matieres(self, institution):
        cree = 0
        for code_semestre, lignes in MAQUETTE.items():
            for ordre, (code, intitule, coef, volume, has_tp) in enumerate(lignes, start=1):
                defauts = PCT_DEFAUT_AVEC_TP if has_tp else PCT_DEFAUT_SANS_TP
                _, nouveau = Matiere.objects.get_or_create(
                    institution=institution, code=code, code_semestre=code_semestre,
                    defaults={
                        'intitule':    intitule,
                        'coefficient': Decimal(coef),
                        # `volume_horaire` est une propriété : le volume se
                        # pose sur la composante cours, à ajuster ensuite.
                        'volume_cm':   volume,
                        'has_tp':      has_tp,
                        'ordre':       ordre,
                        **defauts,
                    },
                )
                cree += int(nouveau)
        self._dire(f'Maquette : {cree} matière(s) ajoutée(s).')

    def _classes(self, institution, annee):
        # Le niveau est une cle etrangere depuis que `Classe` herite de
        # `Departement` ; `groupe` porte le suffixe de classe (A, B...).
        from apps.ipgei.socle import niveau_socle

        cree = 0
        for code_niveau in ('MPSI', 'MP'):
            _, nouveau = Classe.objects.get_or_create(
                institution=institution, niveau=niveau_socle(code_niveau),
                groupe='A', annee_universitaire=annee,
            )
            cree += int(nouveau)
        self._dire(f'Classes de démarrage : {cree} créée(s).')
