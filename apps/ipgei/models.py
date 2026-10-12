"""
Moteur académique IPGEI — Institut Préparatoire aux Grandes Écoles d'Ingénieurs.

Cursus à 2 niveaux : MPSI (1re année) → MP (2e année), 4 semestres S1..S4.
Ce module remplace la couche académique LMD du socle SIGA (filieres / modules /
EM / evaluations). Il ne modifie AUCUN modèle SIGA : il réutilise seulement
`absence.Etudiant`, `prof.Prof`, `salle.Salle`, `parametres.Jour/Creneau/
Institution` et `authentication.CustomUser` par clé étrangère.

Toutes les tables sont préfixées `ipgei_` pour rendre l'origine évidente en base.
"""
import re
from datetime import time
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

# `Classe` et `SousGroupeTP` héritent de ce modèle du socle : une classe IPGEI
# EST un département de planification, ce qui rend Suivi, Vacations, Avancement
# et Emplois utilisables sans copie ni synchronisation.
from apps.departement.models import Departement
from core.types_seance import LIBELLES_EPREUVE
from core.validators import validate_document


# ── Vocabulaire du cursus ────────────────────────────────────────────────────
NIVEAU_MPSI = 'MPSI'
NIVEAU_MP   = 'MP'
NIVEAU_CHOICES = [
    (NIVEAU_MPSI, 'MPSI — 1re année'),
    (NIVEAU_MP,   'MP — 2e année'),
]

# Semestres par RANG d'année, et non par niveau : deux niveaux d'une même année
# — MP et MPI, par exemple — partagent le calendrier. Ce qui les distingue est
# leur maquette, portée par `Matiere.niveau`, pas leur découpage du temps.
SEMESTRES_PAR_RANG = {
    1: ['S1', 'S2'],
    2: ['S3', 'S4'],
}

# Rattachement historique semestre → niveau. Conservé pour le cursus d'origine
# (MPSI/MP) et pour les données d'avant le référentiel de niveaux ; le code
# courant passe par `SEMESTRES_PAR_RANG` et le rang du niveau.
SEMESTRES_PAR_NIVEAU = {
    NIVEAU_MPSI: SEMESTRES_PAR_RANG[1],
    NIVEAU_MP:   SEMESTRES_PAR_RANG[2],
}
CODE_SEMESTRE_CHOICES = [
    ('S1', 'S1 — MPSI, semestre 1'),
    ('S2', 'S2 — MPSI, semestre 2'),
    ('S3', 'S3 — MP, semestre 1'),
    ('S4', 'S4 — MP, semestre 2'),
]

TYPE_SEMESTRE_CHOICES = [('I', 'Impair'), ('P', 'Pair')]

# Types de séance IPGEI (prépa : cours magistral, TD, TP, DS surveillé).
TYPE_SEANCE_COURS = 'cours'
TYPE_SEANCE_TD    = 'td'
TYPE_SEANCE_TP    = 'tp'
TYPE_SEANCE_DS    = 'ds'
TYPE_SEANCE_CHOICES = [
    (TYPE_SEANCE_COURS, 'Cours'),
    (TYPE_SEANCE_TD,    'TD'),
    (TYPE_SEANCE_TP,    'TP'),
    (TYPE_SEANCE_DS,    'Devoir surveillé'),
]

# Pondérations par défaut (plan §4.2) — modifiables par matière.
PCT_DEFAUT_SANS_TP = {'pct_ds': Decimal('30'), 'pct_tp': Decimal('0'),  'pct_exam': Decimal('70')}
PCT_DEFAUT_AVEC_TP = {'pct_ds': Decimal('20'), 'pct_tp': Decimal('10'), 'pct_exam': Decimal('70')}


def _note_field(**kwargs):
    """Champ note /20 — même précision partout (2 décimales, bornée 0..20)."""
    kwargs.setdefault('max_digits', 5)
    kwargs.setdefault('decimal_places', 2)
    kwargs.setdefault('validators', [MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('20'))])
    return models.DecimalField(**kwargs)


# ═════════════════════════════════════════════════════════════════════════════
# 1. Paramétrage global
# ═════════════════════════════════════════════════════════════════════════════
class NiveauIPGEI(models.Model):
    """
    Niveau du cursus : MPSI, MP, et tout niveau ajouté ensuite — MPI par exemple.

    Les deux niveaux d'origine étaient des constantes du code. Quatre règles y
    étaient adossées : les semestres suivis, le droit au redoublement, les
    décisions que le jury peut prononcer, et le rang d'année qui commande le
    tarif. Les porter en données est ce qui permet d'en ajouter un sans toucher
    au code — et de les voir, plutôt que de les deviner en lisant `services.py`.

    Le rang est saisi à la création : dire qu'un niveau est de 1re ou de 2e
    année suffit à en déduire ses semestres et son tarif. Deux niveaux peuvent
    partager un rang — c'est le cas de MP et MPI — sans partager leur maquette.
    """
    institution = models.ForeignKey(
        'parametres.Institution', on_delete=models.CASCADE, related_name='niveaux_ipgei',
    )
    code    = models.CharField(
        max_length=10, help_text='Code court affiché partout : MPSI, MP, MPI…',
    )
    libelle = models.CharField(
        max_length=100, blank=True, default='',
        help_text="Intitulé complet. Vide, le code et le rang en tiennent lieu.",
    )
    rang    = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(len(SEMESTRES_PAR_RANG))],
        help_text='1 = première année, 2 = deuxième année. Commande les semestres et le tarif.',
    )

    redoublement_autorise = models.BooleanField(
        default=False,
        help_text="Le jury peut-il prononcer un redoublement à ce niveau ? "
                  "Interdit en première année, sans exception.",
    )
    # Liste de codes de `LigneDeliberation.DECISION_CHOICES`. Vide = les
    # décisions habituelles du rang, pour qu'un niveau créé à la hâte reste
    # utilisable sans paramétrage supplémentaire.
    decisions_possibles = models.JSONField(
        default=list, blank=True,
        help_text='Codes des décisions que le jury peut prononcer. Vide = celles du rang.',
    )

    actif = models.BooleanField(
        default=True,
        help_text='Un niveau désactivé disparaît des écrans sans rien effacer : '
                  'inscriptions et délibérations passées restent lisibles.',
    )
    ordre = models.PositiveSmallIntegerField(default=0, help_text="Ordre d'affichage.")

    class Meta:
        db_table = 'ipgei_niveau'
        ordering = ['rang', 'ordre', 'code']
        verbose_name        = 'niveau IPGEI'
        verbose_name_plural = 'niveaux IPGEI'
        constraints = [
            models.UniqueConstraint(
                fields=['institution', 'code'], name='uniq_ipgei_niveau_code',
            ),
        ]

    def __str__(self):
        return self.libelle or f'{self.code} — {self.libelle_rang}'

    @property
    def libelle_rang(self) -> str:
        return '1re année' if self.rang == 1 else f'{self.rang}e année'

    @property
    def codes_semestres(self) -> list:
        """Semestres suivis à ce niveau — ceux de son rang."""
        return list(SEMESTRES_PAR_RANG.get(self.rang, []))

    def clean(self):
        # La règle du cursus prime sur la saisie : le redoublement en première
        # année n'existe pas, pas même par dérogation.
        if self.rang == 1 and self.redoublement_autorise:
            raise ValidationError(
                {'redoublement_autorise': 'Le redoublement est interdit en première année.'}
            )


class ParametresIPGEI(models.Model):
    """
    Réglages institutionnels du cursus (singleton par institution).

    Ces valeurs servent de DÉFAUTS : chaque délibération fige son propre seuil
    et son propre plafond de rattrapage, de sorte qu'un changement de paramétrage
    ne réécrit jamais une décision déjà prise.
    """
    institution = models.OneToOneField(
        'parametres.Institution', on_delete=models.CASCADE,
        related_name='parametres_ipgei',
    )
    # Un seuil par portée : valider un semestre et prononcer le passage d'année
    # ne se jugent pas sur la même exigence. Une valeur unique obligeait à la
    # corriger à la main sur chaque jury dont la portée n'était pas celle du
    # paramétrage — donc à l'oublier un jour.
    seuil_validation      = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal('10'),
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('20'))],
        help_text="Seuil des délibérations annuelles : admission / autorisation CNIM.",
    )
    seuil_validation_semestre = models.DecimalField(
        max_digits=4, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('20'))],
        help_text='Seuil des délibérations semestrielles. Vide = même seuil que les annuelles.',
    )
    plafond_rattrapage    = _note_field(
        null=True, blank=True,
        help_text="Note maximale qu'un rattrapage peut rapporter. Vide = sans plafond.",
    )
    nb_semaines_defaut    = models.PositiveSmallIntegerField(
        default=16, validators=[MinValueValidator(1), MaxValueValidator(40)],
        help_text="Nombre de semaines de cours proposé à la création d'un semestre.",
    )
    droit_redoublement_max = models.PositiveSmallIntegerField(
        default=1,
        help_text="Nombre de redoublements autorisés en 2e année (MP). Défaut : 1 (droit unique).",
    )

    class Meta:
        db_table            = 'ipgei_parametres'
        verbose_name        = 'Paramètres IPGEI'
        verbose_name_plural = 'Paramètres IPGEI'

    def __str__(self):
        return f'Paramètres IPGEI — {self.institution}'

    @classmethod
    def pour(cls, institution):
        """Retourne (en créant au besoin) les paramètres d'une institution."""
        obj, _ = cls.objects.get_or_create(institution=institution)
        return obj

    def seuil_pour(self, portee: str):
        """
        Seuil proposé à une délibération selon sa portée.

        Le seuil semestriel est facultatif : laissé vide, il vaut celui des
        annuelles. C'est ce qui permet d'ajouter le champ sans changer le
        comportement des établissements qui n'en distinguent qu'un.
        """
        if portee == Deliberation.PORTEE_SEMESTRE and self.seuil_validation_semestre is not None:
            return self.seuil_validation_semestre
        return self.seuil_validation


class SignataireIPGEI(models.Model):
    """
    Personne habilitée à signer les documents officiels.

    Le bloc de signature était écrit en dur dans les gabarits — « Chef service
    de la Scolarité », un titre sans nom. Quand son titulaire est absent, le
    document sortait quand même à son intitulé, et un autre signait à la main
    sous une fonction qui n'était pas la sienne.

    Aucune image ici : la signature reste manuscrite. Ce qui varie, c'est le
    NOM et le TITRE imprimés au-dessus du trait.
    """
    institution = models.ForeignKey(
        'parametres.Institution', on_delete=models.CASCADE,
        related_name='signataires_ipgei',
    )
    nom_fr   = models.CharField(max_length=150, help_text='Nom imprimé sur le document.')
    nom_ar   = models.CharField(max_length=150, blank=True, default='')
    titre_fr = models.CharField(
        max_length=150,
        help_text='Fonction : Chef service de la Scolarité, Directeur des études…',
    )
    titre_ar = models.CharField(max_length=150, blank=True, default='')
    #: Proposé d'emblée à la génération. Un seul par institution — le poser
    #: sur un autre le retire au précédent, sans quoi le choix par défaut
    #: dépendrait de l'ordre de lecture.
    par_defaut = models.BooleanField(default=False)
    actif      = models.BooleanField(default=True)
    ordre      = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = 'ipgei_signataire'
        ordering = ['ordre', 'nom_fr']
        constraints = [
            models.UniqueConstraint(
                fields=['institution', 'nom_fr', 'titre_fr'],
                name='uniq_ipgei_signataire',
            ),
        ]

    def __str__(self):
        return f'{self.nom_fr} — {self.titre_fr}'

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.par_defaut:
            (SignataireIPGEI.objects
             .filter(institution_id=self.institution_id, par_defaut=True)
             .exclude(pk=self.pk)
             .update(par_defaut=False))


# ═════════════════════════════════════════════════════════════════════════════
# 2. Calendrier : semestres + semaines flexibles
# ═════════════════════════════════════════════════════════════════════════════
class SemestreIPGEI(models.Model):
    """
    Semestre IPGEI d'une année universitaire donnée (S1..S4).

    Le socle SIGA porte un `parametres.Semestre` générique orienté LMD (crédits,
    niveau LMD) sans bornes de dates : IPGEI a besoin de dates réelles pour
    générer les semaines, d'où ce modèle dédié. `parametres.Semestre` reste
    intact et utilisable par le reste du socle.
    """
    institution         = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT, related_name='semestres_ipgei',
    )
    code                = models.CharField(max_length=2, choices=CODE_SEMESTRE_CHOICES)
    annee_universitaire = models.CharField(max_length=9, help_text='ex. 2025-2026')
    date_debut          = models.DateField(
        help_text='Date de la première semaine. Les suivantes en découlent.',
    )
    # Facultative : le calendrier fait foi, pas la période. On ajoute des
    # semaines au fil de l'année plutôt que de prédire une date de fin qu'un
    # report d'examens ou une semaine de rattrapage rendrait fausse.
    date_fin            = models.DateField(
        null=True, blank=True,
        help_text='Indicative. La fin réelle est celle de la dernière semaine générée.',
    )
    nb_semaines         = models.PositiveSmallIntegerField(
        default=16, validators=[MinValueValidator(1), MaxValueValidator(40)],
        help_text='Nombre de semaines ajoutées par défaut à chaque génération.',
    )
    # Le DÉCALAGE de rentrée, en semaines. Il n'y a qu'un seul calendrier par
    # parité : S1 et S3 reçoivent les mêmes semaines, aux mêmes numéros. Quand
    # une promotion entre plus tard — les MPSI arrivés le 12/10/2026, quatre
    # semaines après les MP —, ses étudiants lisent « semaine 5 » sur un
    # emploi du temps qui est leur premier.
    #
    # Le numéro en base ne bouge pas : c'est lui que lisent le suivi et la
    # paie, et c'est ce qui les garde justes. Seul l'AFFICHAGE recule, de ce
    # nombre de semaines. Le socle convertit déjà ainsi, par département
    # (`Departement.decalage_impair`) ; ce champ en est la source unique, et
    # `services.appliquer_decalage` le recopie sur les classes du niveau.
    #
    # Posé ici, et non sur chaque classe : ce n'est pas une propriété de
    # « MPSI A » mais de la promotion. Quinze saisies identiques seraient
    # quinze occasions d'en oublier une, et l'oubli ne se verrait pas.
    decalage_semaines   = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(20)],
        help_text="Semaines de retard à la rentrée : l'affichage recule d'autant, "
                  'la numérotation en base ne change pas.',
    )
    est_cloture         = models.BooleanField(
        default=False,
        help_text='Semestre clôturé : notes verrouillées, plus de saisie.',
    )

    class Meta:
        db_table    = 'ipgei_semestre'
        ordering    = ['-annee_universitaire', 'code']
        constraints = [
            models.UniqueConstraint(
                fields=['institution', 'code', 'annee_universitaire'],
                name='uniq_ipgei_semestre',
            ),
        ]

    def __str__(self):
        return f'{self.code} {self.annee_universitaire}'

    @property
    def rang(self) -> int:
        """Année d'étude du semestre : S1/S2 en 1re, S3/S4 en 2e."""
        for rang, codes in SEMESTRES_PAR_RANG.items():
            if self.code in codes:
                return rang
        return 1

    @property
    def libelle_annee(self) -> str:
        """« 1re année », « 2e année » — ce qu'un semestre désigne réellement."""
        return '1re année' if self.rang == 1 else f'{self.rang}e année'

    @property
    def niveaux(self) -> list:
        """
        Codes des niveaux qui suivent ce semestre.

        Un semestre appartient à une année d'étude, pas à un niveau : S1 est
        celui de tous les niveaux de première année. Le déduire du seul code
        renvoyait « MPSI » quoi qu'il arrive, si bien qu'ouvrir un MPI de
        première année le laissait invisible — et, plus grave, hors des
        requêtes qui filtraient là-dessus.
        """
        from .models import NiveauIPGEI

        return list(
            NiveauIPGEI.objects
            .filter(institution=self.institution_id, rang=self.rang, actif=True)
            .order_by('ordre', 'code')
            .values_list('code', flat=True)
        )

    @property
    def type_semestre(self) -> str:
        """S1/S3 impairs, S2/S4 pairs — pilote le choix de la grille type."""
        return 'I' if self.code in ('S1', 'S3') else 'P'

    def clean(self):
        if self.date_debut and self.date_fin and self.date_fin <= self.date_debut:
            raise ValidationError({'date_fin': 'La date de fin doit suivre la date de début.'})


class SemaineIPGEI(models.Model):
    """
    Semaine pédagogique d'un semestre. Seules les semaines de type `cours` sont
    numérotées ; vacances / examens / fériés restent dans la séquence pour
    l'affichage du calendrier mais ne portent pas de numéro.
    """
    TYPE_COURS    = 'cours'
    TYPE_EXAMEN   = 'examen'
    TYPE_VACANCES = 'vacances'
    TYPE_FERIE    = 'ferie'
    TYPE_CHOICES  = [
        (TYPE_COURS,    'Cours'),
        (TYPE_EXAMEN,   'Examens'),
        (TYPE_VACANCES, 'Vacances'),
        (TYPE_FERIE,    'Férié'),
    ]

    semestre     = models.ForeignKey(
        SemestreIPGEI, on_delete=models.CASCADE, related_name='semaines',
    )
    numero       = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text='Numéro dans la séquence de cours. NULL hors semaine de cours.',
    )
    date_debut   = models.DateField(help_text='Lundi de la semaine.')
    date_fin     = models.DateField(help_text='Dimanche de la semaine.')

    # Horodatage de la dernière génération du suivi pour cette semaine.
    #
    # C'est la moitié du verrou de cohérence : comparé à `SeanceReelle.modifiee_le`,
    # il dit si l'emploi du temps a bougé DEPUIS que le suivi en a été tiré. Sans
    # lui, une séance corrigée après génération faisait diverger l'emploi du temps
    # et le pointage en silence, et plus rien ne disait lequel faisait foi.
    suivi_genere_le = models.DateTimeField(
        null=True, blank=True, editable=False,
        help_text='Dernière génération du suivi de cette semaine.',
    )

    # États possibles d'une semaine, du point de vue de la cohérence.
    ETAT_PREVISIONNEL = 'previsionnel'   # aucun suivi : l'EDT n'engage que l'avenir
    ETAT_ALIGNE       = 'aligne'         # suivi généré, et rien n'a bougé depuis
    ETAT_DIVERGENT    = 'divergent'      # l'EDT a changé APRÈS la génération
    type_semaine = models.CharField(max_length=10, choices=TYPE_CHOICES, default=TYPE_COURS)
    description  = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        db_table    = 'ipgei_semaine'
        ordering    = ['semestre', 'date_debut']
        constraints = [
            models.UniqueConstraint(fields=['semestre', 'date_debut'], name='uniq_ipgei_semaine_date'),
        ]
        indexes = [models.Index(fields=['semestre', 'numero'])]

    def __str__(self):
        if self.numero is None:
            return f'[{self.get_type_semaine_display()}] {self.date_debut}'
        return f'S{self.numero} — {self.date_debut}'

    def _empreinte_edt(self, classe=None) -> set:
        """
        Ce que l'emploi du temps de la semaine contient réellement.

        Une empreinte par enseignement : où, quand, quoi, par qui, de quel
        type. La salle en est volontairement absente — la déplacer ne change
        ni les heures dues ni le programme avancé, donc ne rend pas le suivi
        faux.
        """
        # `values_list` plutôt que des instances : l'empreinte ne lit que six
        # colonnes, en construire des objets complets coûtait l'essentiel du
        # temps de calcul sur un semestre entier.
        #
        # Les deux côtés désignent désormais la MÊME ligne de référentiel : le
        # type se compare directement, sans traduction.
        empreinte = {
            (sous_groupe or classe_id, jour, creneau, em, prof, type_seance)
            for sous_groupe, classe_id, jour, creneau, em, prof, type_seance
            in self.seances.filter(annulee=False, type_seance__is_special=False,
                                   **({'classe': classe} if classe else {})).values_list(
                'sous_groupe_id', 'classe_id', 'jour_id', 'creneau_id',
                'matiere__em_miroir_id', 'prof_id', 'type_seance__type_seance',
            )
        }
        empreinte |= self._empreinte_epreuves(classe)
        return empreinte

    def _empreinte_epreuves(self, classe=None) -> set:
        """
        Les ÉPREUVES de la semaine, dans la même forme que les séances.

        Sans elles, une semaine qui en porte une se déclarait éternellement
        « divergente » : l'épreuve produit une ligne de `Suivie` — c'est ce qui
        la rend pointable — mais aucune `SeanceReelle`. Elle figurait donc dans
        l'empreinte du suivi et manquait à celle de l'emploi du temps, et les
        deux ne pouvaient plus jamais s'égaler.

        Le professeur est NUL, comme sur la ligne projetée : une épreuve n'en a
        pas. L'ancrage vient de `ancrage_epreuve`, le même que la projection
        utilise — les deux doivent désigner la même case.
        """
        from apps.parametres.models import Creneau, Jour

        convocations = EpreuveClasse.objects.filter(
            epreuve__date__gte=self.date_debut, epreuve__date__lte=self.date_fin,
            **({'classe': classe} if classe else
               {'classe_id__in': self._unites_du_semestre()}),
        ).select_related('epreuve__matiere')
        if not convocations:
            return set()

        creneaux = list(Creneau.objects.filter(is_actif=True).order_by('ordre', 'id'))
        jours = {j.jour.lower(): j for j in Jour.objects.all()}

        empreinte = set()
        for convocation in convocations:
            epreuve = convocation.epreuve
            jour, creneau = ancrage_epreuve(epreuve, creneaux, jours)
            if jour is None:
                continue
            em_id = epreuve.matiere.em_miroir_id if epreuve.matiere_id else None
            empreinte.add((convocation.classe_id, jour.pk, creneau.pk, em_id,
                           None, epreuve.type_epreuve))
        return empreinte

    def _unites_du_semestre(self, classe=None) -> list:
        """
        Départements (classes et sous-groupes) sur lesquels porte la comparaison.

        Sans `classe`, tout le niveau du semestre : c'est nécessaire car S1 et
        S3 sont tous deux impairs et partagent leurs numéros de semaine —
        comparer sans ce filtre revenait à confronter MPSI au suivi de MP.

        Avec `classe`, la comparaison se restreint à elle et à ses sous-groupes.
        L'état est alors celui de CETTE classe : une classe sans emploi du temps
        ne doit pas hériter du verdict de sa voisine du même niveau.
        """
        if classe is not None:
            return [classe.pk] + list(
                SousGroupeTP.objects.filter(classe=classe).values_list('pk', flat=True))

        # Tous les niveaux de l'année d'étude, et pas le seul niveau déduit du
        # code : un MPI de première année suit S1 comme MPSI, et son suivi ne
        # doit pas manquer à la comparaison.
        classes = Classe.objects.filter(
            annee_universitaire=self.semestre.annee_universitaire,
            niveau__niveau__in=self.semestre.niveaux,
        )
        ids = list(classes.values_list('pk', flat=True))
        ids += list(SousGroupeTP.objects.filter(classe__in=ids).values_list('pk', flat=True))
        return ids

    def _empreinte_suivi(self, classe=None) -> set:
        """La même empreinte, vue depuis les lignes de suivi de la semaine."""
        from apps.suivi.models import Suivie

        type_semestre = 'I' if self.semestre.code in ('S1', 'S3') else 'P'
        return set(
            Suivie.objects
            .filter(annee_universitaire=self.semestre.annee_universitaire,
                    type_semestre=type_semestre, numero_semaine=self.numero,
                    departement_id__in=self._unites_du_semestre(classe),
                    em__isnull=False)
            .values_list('departement_id', 'jour_fk_id', 'creneau_fk_id',
                         'em_id', 'prof_id', 'type_seance_fk__type_seance')
        )

    @property
    def etat_coherence(self) -> str:
        """
        Rapport entre l'emploi du temps de la semaine et son suivi.

        La comparaison porte sur le CONTENU, pas sur des horodatages. Comparer
        `modifiee_le` à `suivi_genere_le` disait seulement que quelque chose
        avait été touché : un enregistrement sans changement, ou une valeur
        modifiée puis remise, suffisait à déclarer une divergence qui n'existait
        pas. Un faux positif sur ce point est coûteux — il pousse à régénérer un
        suivi qui allait bien.

        « Divergent » signifie donc : les deux décrivent réellement des semaines
        différentes. Les heures pointées, la charge et les vacations ne
        correspondent plus à ce qu'on imprime.
        """
        return self.etat_coherence_pour()

    def etat_coherence_pour(self, classe=None) -> str:
        """État vu depuis une classe donnée, ou depuis tout le niveau."""
        empreinte_suivi = self._empreinte_suivi(classe)
        if not empreinte_suivi:
            return self.ETAT_PREVISIONNEL
        if self._empreinte_edt(classe) != empreinte_suivi:
            return self.ETAT_DIVERGENT
        return self.ETAT_ALIGNE

    @classmethod
    def etats_en_lot(cls, semaines, classe=None) -> dict:
        """
        États de plusieurs semaines en **deux requêtes**, pas deux par semaine.

        Le calcul unitaire coûtait 39 allers-retours et près d'une demi-seconde
        pour un semestre de seize semaines — sur un écran de liste, cela se
        sent. Le résultat est identique, seule la façon de le lire change.
        """
        from apps.suivi.models import Suivie

        semaines = list(semaines)
        if not semaines:
            return {}

        # Côté emploi du temps : une passe sur toutes les séances concernées.
        edt = {s.pk: set() for s in semaines}
        for (sid, sous_groupe, classe_id, jour, creneau, em, prof, type_seance) in (
            SeanceReelle.objects
            .filter(semaine_id__in=edt, annulee=False, type_seance__is_special=False,
                    **({'classe': classe} if classe else {}))
            .values_list('semaine_id', 'sous_groupe_id', 'classe_id', 'jour_id',
                         'creneau_id', 'matiere__em_miroir_id', 'prof_id',
                         'type_seance__type_seance')
        ):
            edt[sid].add((sous_groupe or classe_id, jour, creneau, em, prof, type_seance))

        # Les ÉPREUVES, sans quoi ce calcul et le calcul unitaire donneraient
        # des verdicts opposés sur la même semaine — l'un lu par les écrans de
        # liste, l'autre par le PDF. Elles produisent une ligne de `Suivie`
        # mais aucune `SeanceReelle` : les omettre ici déclare divergente toute
        # semaine qui en porte une.
        #
        # UNE passe pour tout le lot, comme le reste : appeler l'empreinte
        # unitaire par semaine aurait rendu fausse la promesse de deux
        # requêtes, sur l'écran de liste où elle a été écrite.
        cls._ajouter_epreuves_en_lot(edt, semaines, classe)

        # Côté suivi : une passe sur les lignes des années et périodes visées.
        annees   = {s.semestre.annee_universitaire for s in semaines}
        numeros  = {s.numero for s in semaines if s.numero is not None}
        # Même portée que le calcul unitaire : on ne compare une semaine qu'aux
        # unités de SON niveau, S1 et S3 partageant leurs numéros de semaine.
        # Une fois par SEMESTRE, pas par semaine : les unités ne dépendent que
        # du semestre et de la classe. Appelé par semaine, cela coûtait deux
        # requêtes chacune — 64 pour un semestre de 32 — et la promesse de
        # « deux requêtes » ci-dessus était fausse.
        unites = set()
        for semaine in {s.semestre_id: s for s in semaines}.values():
            unites.update(semaine._unites_du_semestre(classe))
        suivi = {}
        for (annee, type_semestre, numero, dept, jour, creneau, em, prof, type_seance) in (
            Suivie.objects
            .filter(annee_universitaire__in=annees, numero_semaine__in=numeros,
                    departement_id__in=unites, em__isnull=False)
            .values_list('annee_universitaire', 'type_semestre', 'numero_semaine',
                         'departement_id', 'jour_fk_id', 'creneau_fk_id',
                         'em_id', 'prof_id', 'type_seance_fk__type_seance')
        ):
            suivi.setdefault((annee, type_semestre, numero), set()).add(
                (dept, jour, creneau, em, prof, type_seance))

        etats = {}
        for semaine in semaines:
            type_semestre = 'I' if semaine.semestre.code in ('S1', 'S3') else 'P'
            cle = (semaine.semestre.annee_universitaire, type_semestre, semaine.numero)
            empreinte_suivi = suivi.get(cle, set())
            if not empreinte_suivi:
                etats[semaine.pk] = cls.ETAT_PREVISIONNEL
            elif edt.get(semaine.pk, set()) != empreinte_suivi:
                etats[semaine.pk] = cls.ETAT_DIVERGENT
            else:
                etats[semaine.pk] = cls.ETAT_ALIGNE
        return etats

    @classmethod
    def _ajouter_epreuves_en_lot(cls, edt: dict, semaines, classe=None) -> None:
        """
        Verse les épreuves de toutes les semaines dans leurs empreintes, en une
        passe. Modifie `edt` sur place.

        L'appartenance à une semaine se décide sur la DATE : les épreuves sont
        lues une fois sur l'intervalle qui couvre tout le lot, puis réparties.
        """
        from apps.parametres.models import Creneau, Jour

        bornes = [(s.pk, s.date_debut, s.date_fin) for s in semaines
                  if s.date_debut and s.date_fin]
        if not bornes:
            return

        unites = set()
        for semaine in {s.semestre_id: s for s in semaines}.values():
            unites.update(semaine._unites_du_semestre(classe))

        convocations = list(EpreuveClasse.objects.filter(
            epreuve__date__gte=min(b[1] for b in bornes),
            epreuve__date__lte=max(b[2] for b in bornes),
            classe_id__in=unites,
        ).select_related('epreuve__matiere'))
        if not convocations:
            return

        creneaux = list(Creneau.objects.filter(is_actif=True).order_by('ordre', 'id'))
        jours = {j.jour.lower(): j for j in Jour.objects.all()}

        for convocation in convocations:
            epreuve = convocation.epreuve
            jour, creneau = ancrage_epreuve(epreuve, creneaux, jours)
            if jour is None:
                continue
            em_id = epreuve.matiere.em_miroir_id if epreuve.matiere_id else None
            trace = (convocation.classe_id, jour.pk, creneau.pk, em_id,
                     None, epreuve.type_epreuve)
            for pk, debut, fin in bornes:
                if debut <= epreuve.date <= fin:
                    edt.setdefault(pk, set()).add(trace)

    @property
    def libelle_coherence(self) -> str:
        return {
            self.ETAT_PREVISIONNEL: 'Prévisionnel — suivi non généré',
            self.ETAT_ALIGNE:       'Suivi généré et à jour',
            self.ETAT_DIVERGENT:    'Emploi du temps modifié après la génération du suivi',
        }[self.etat_coherence]


# ═════════════════════════════════════════════════════════════════════════════
# 3. Structure : classes, sous-groupes de TP, inscriptions
# ═════════════════════════════════════════════════════════════════════════════
class Classe(Departement):
    """
    Classe préparatoire d'une année : MPSI A, MPSI B, MP A… Aucune limite du
    nombre de classes par niveau et par année (plan §4.1).

    **Une classe EST un département du socle** (héritage multi-table) : une
    seule ligne logique, un seul identifiant. Suivi, Vacations, Avancement et
    Emplois indexent leurs données sur `departement.Departement` ; en héritant,
    la classe leur devient directement utilisable — il n'y a rien à
    synchroniser, donc rien qui puisse diverger.

    Champs repris du parent : `institution`, `annee_universitaire`, `niveau`
    (FK `parametres.Niveau`, valant MPSI ou MP), `nom` (« MPSI A »), `code`,
    `groupe` (le suffixe de classe : A, B, C…), `filiere` et `is_container`.
    Les quatre derniers sont dérivés automatiquement dans `save()`.
    """
    # Lien de parenté nommé explicitement : par défaut Django exposerait la
    # classe sur le département sous le nom `classe`, qui entrerait en conflit
    # avec la clé étrangère `SousGroupeTP.classe` — un sous-groupe étant lui
    # aussi un département, il hériterait de cet accesseur.
    departement_ptr = models.OneToOneField(
        Departement, on_delete=models.CASCADE, parent_link=True,
        primary_key=True, related_name='classe_ipgei',
    )

    capacite             = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text='Effectif maximal indicatif (aucun blocage).',
    )
    professeur_principal = models.ForeignKey(
        'prof.Prof', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='classes_ipgei_principales',
    )
    actif                = models.BooleanField(default=True)
    date_creation        = models.DateTimeField(auto_now_add=True)

    # `institution`, `annee_universitaire` et `niveau` vivent sur la table
    # parente : Django refuse une UniqueConstraint qui mêle champs hérités et
    # champs locaux. Cette clé dérivée, recalculée à chaque `save()`, fait
    # garantir l'unicité (institution, année, niveau, groupe) par la base et
    # non par la seule couche applicative.
    cle_unicite = models.CharField(max_length=120, unique=True, editable=False, default='')

    class Meta:
        db_table            = 'ipgei_classe'
        ordering            = ['-annee_universitaire', 'nom']
        verbose_name        = 'classe IPGEI'
        verbose_name_plural = 'classes IPGEI'

    def __str__(self):
        return f'{self.niveau_code} {self.groupe} ({self.annee_universitaire})'

    @property
    def niveau_code(self) -> str:
        """
        « MPSI » / « MP ». Le niveau du socle est une clé étrangère et non une
        chaîne : ce raccourci évite d'écrire `classe.niveau.niveau` partout.
        """
        return self.niveau.niveau if self.niveau_id else ''

    @property
    def libelle(self) -> str:
        """Suffixe de classe (A, B…) — alias de lecture sur le champ hérité."""
        return self.groupe

    @property
    def effectif(self) -> int:
        return self.inscriptions.filter(actif=True).count()

    @property
    def est_conteneur(self) -> bool:
        """
        Classe d'attente d'un niveau, sans suffixe : « MPSI » plutôt que
        « MPSI A ».

        Elle reçoit les inscrits à leur arrivée, avant qu'on sache combien de
        classes ouvrir et comment les répartir. Elle ne se planifie pas : ni
        emploi du temps, ni suivi, ni appel — le socle exclut partout les
        départements marqués `is_container`.
        """
        return not (self.groupe or '').strip()

    def save(self, *args, **kwargs):
        # Import local : `socle` importe ce module, l'importer en tête créerait
        # un cycle.
        from .socle import filiere_socle

        self.nom          = f'{self.niveau_code} {self.groupe}'.strip()
        self.code         = f'{self.niveau_code}-{self.groupe}'.strip('-')
        # Une classe se planifie ; une classe d'attente, non. Le socle s'appuie
        # sur ce drapeau pour l'écarter des emplois, du suivi et de l'appel.
        self.is_container = self.est_conteneur
        if not self.filiere_id:
            self.filiere = filiere_socle(self.institution)
        self.cle_unicite = (
            f'{self.institution_id}|{self.annee_universitaire}|'
            f'{self.niveau_code}|{self.groupe}'
        )
        super().save(*args, **kwargs)


class SousGroupeTP(Departement):
    """
    Sous-groupe de TP d'une classe (G1, G2…). **Fixe sur le semestre** : la
    composition ne change pas en cours de semestre (plan, cadrage). Les matières
    concernées (Informatique, Physique…) sont explicitement rattachées.

    **Un sous-groupe est lui aussi un département**, et c'est ce qui rend le
    dédoublement gérable : ni `emplois.Emplois` ni `suivi.Suivie` ne portent de
    champ « groupe », le département est le seul niveau de planification que le
    socle sache distinguer. Sans cela les deux TP d'une même classe retombent
    sur la même ligne, impossibles à départager au pointage comme sur
    l'attestation de vacation.

    Le libellé (G1, G2…) est porté par le champ hérité `groupe`.
    """
    departement_ptr = models.OneToOneField(
        Departement, on_delete=models.CASCADE, parent_link=True,
        primary_key=True, related_name='sous_groupe_ipgei',
    )

    classe   = models.ForeignKey(Classe, on_delete=models.CASCADE, related_name='sous_groupes')
    matieres = models.ManyToManyField(
        'ipgei.Matiere', blank=True, related_name='sous_groupes',
        help_text='Matières dont les TP sont dédoublés sur ce sous-groupe.',
    )

    # Même raison que sur `Classe` : `classe` est locale, `groupe` est héritée.
    cle_unicite = models.CharField(max_length=120, unique=True, editable=False, default='')

    class Meta:
        db_table            = 'ipgei_sous_groupe_tp'
        ordering            = ['classe', 'groupe']
        verbose_name        = 'sous-groupe de TP'
        verbose_name_plural = 'sous-groupes de TP'

    def __str__(self):
        return f'{self.classe.nom} — {self.groupe}'

    @property
    def libelle(self) -> str:
        """Nom court du sous-groupe — alias de lecture sur le champ hérité."""
        return self.groupe

    def save(self, *args, **kwargs):
        # Le sous-groupe reprend le cadre de sa classe. Sans ces reports, un
        # écran du socle filtrant sur l'année ou l'institution laisserait
        # silencieusement tomber les séances de TP.
        self.institution         = self.classe.institution
        self.annee_universitaire = self.classe.annee_universitaire
        self.niveau              = self.classe.niveau
        self.filiere             = self.classe.filiere
        self.is_container        = False
        self.nom                 = f'{self.classe.nom} — {self.groupe}'
        self.code                = f'{self.classe.code}-{self.groupe}'
        self.cle_unicite         = f'{self.classe_id}|{self.groupe}'
        super().save(*args, **kwargs)


class InscriptionQuerySet(models.QuerySet):
    """Les inscriptions IPGEI, et le seul endroit qui sait écarter un auditeur."""

    def cursus(self):
        """
        Les inscriptions QUI COMPTENT — celles de la promotion, auditeurs
        libres exclus.

        À appeler partout où l'on constitue une POPULATION : jury, classement,
        PV, CNIM, effectifs, moyennes de classe, rangs. Pas ailleurs : la
        saisie des notes, l'émargement et le relevé doivent voir l'auditeur.

        Un filtre unique, parce que la faute n'est pas rattrapable à l'œil :
        un auditeur oublié dans un classement ne s'y voit pas, il y décale
        seulement le rang de tous les autres. Cf. `auditeur_libre`.
        """
        return self.filter(auditeur_libre=False)


class InscriptionIPGEI(models.Model):
    """
    Rattachement d'un étudiant à une classe pour une année universitaire.

    Un changement de classe (permutation étudiant) **modifie cette ligne** et
    journalise l'ancien rattachement dans `HistoriqueClasse` : notes et absences
    pointent sur l'inscription, donc rien n'est copié ni perdu (plan §4.6).
    """

    objects = InscriptionQuerySet.as_manager()

    STATUT_ACTIF      = 'actif'
    STATUT_ADMIS      = 'admis'
    STATUT_REORIENTE  = 'reoriente'
    STATUT_REDOUBLANT = 'redoublant'
    STATUT_CNIM       = 'autorise_cnim'
    #: Entre le jury annuel et le jury de rattrapage : l'étudiant n'est ni
    #: admis à concourir ni écarté, il compose une seconde fois.
    STATUT_RATTRAPAGE = 'autorise_rattrapage'
    #: Fin du cursus par la réussite au concours — l'étudiant sort de l'IPGEI.
    STATUT_SORTANT    = 'sortant'
    STATUT_ABANDON    = 'abandon'
    STATUT_CHOICES    = [
        (STATUT_ACTIF,      'En cours'),
        (STATUT_ADMIS,      'Admis en 2e année'),
        (STATUT_REORIENTE,  'Réorienté'),
        (STATUT_REDOUBLANT, 'Redoublant'),
        (STATUT_CNIM,       'Autorisé à concourir (CNIM)'),
        (STATUT_RATTRAPAGE, 'Autorisé au rattrapage'),
        (STATUT_SORTANT,    'Sortant — admis au concours'),
        (STATUT_ABANDON,    'Abandon'),
    ]

    etudiant            = models.ForeignKey(
        'absence.Etudiant', on_delete=models.PROTECT, related_name='inscriptions_ipgei',
    )
    classe              = models.ForeignKey(
        Classe, on_delete=models.PROTECT, related_name='inscriptions',
    )
    sous_groupe         = models.ForeignKey(
        SousGroupeTP, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='inscriptions',
    )
    annee_universitaire = models.CharField(max_length=9)
    numero_ordre        = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Rang alphabétique dans la classe (affichage listes).",
    )
    #: Ligne de jury dont cette inscription procède, quand elle vient d'un
    #: passage en année supérieure. Elle répond à trois questions que rien ne
    #: permettait de poser : d'où vient ce dossier, cette décision a-t-elle été
    #: suivie d'effet, et laquelle ne l'a pas été.
    #:
    #: `PROTECT` : effacer la délibération effacerait l'origine de dossiers bien
    #: réels. Nulle sur une inscription saisie à la main — la plupart.
    issue_de            = models.ForeignKey(
        'ipgei.LigneDeliberation', on_delete=models.PROTECT,
        null=True, blank=True, related_name='inscriptions_issues',
    )
    statut              = models.CharField(max_length=24, choices=STATUT_CHOICES, default=STATUT_ACTIF)

    # ── Concours CNIM ────────────────────────────────────────────────────────
    # Le résultat vient de l'EXTÉRIEUR : il ne se calcule pas, il se constate.
    # Il est conservé — plutôt que déduit du statut — pour répondre plus tard à
    # « pourquoi cet étudiant est-il sortant ? », et pour qu'appliquer deux fois
    # les situations ne compte pas deux redoublements.
    CNIM_EN_ATTENTE = ''
    CNIM_ADMIS      = 'admis'
    CNIM_NON_ADMIS  = 'non_admis'
    CNIM_CHOICES    = [
        (CNIM_EN_ATTENTE, 'En attente du résultat'),
        (CNIM_ADMIS,      'Admis au concours'),
        (CNIM_NON_ADMIS,  'Non admis'),
    ]
    resultat_cnim      = models.CharField(
        max_length=10, choices=CNIM_CHOICES, blank=True, default=CNIM_EN_ATTENTE,
        help_text='Résultat du concours CNIM, saisi ou importé.',
    )
    date_resultat_cnim = models.DateTimeField(null=True, blank=True)
    nb_redoublements    = models.PositiveSmallIntegerField(
        default=0,
        help_text='Compteur du droit de redoublement (MP). Au-delà du max, le redoublement est refusé.',
    )
    date_inscription    = models.DateField(auto_now_add=True)
    actif               = models.BooleanField(default=True)

    #: SUIT LA SCOLARITÉ SANS ÊTRE DE LA PROMOTION.
    #:
    #: Deux cas, et un seul besoin. Un ancien de l'IPGEI autorisé à refaire sa
    #: deuxième année — il garde son matricule, la contrainte d'unicité portant
    #: sur (étudiant, année) laisse passer la nouvelle inscription. Ou quelqu'un
    #: venu d'une prépa étrangère pour sa seule deuxième année, à qui l'on
    #: attribue un matricule. Ni l'un ni l'autre n'est étudiant de l'IPGEI ; les
    #: deux suivent les cours, composent, et doivent obtenir un relevé.
    #:
    #: Ce n'est PAS une valeur de `statut` : le statut est un axe de cycle de
    #: vie — actif, admis, sortant, abandon — et un auditeur en a un aussi, il
    #: peut abandonner en cours d'année. Les fondre rendrait « auditeur qui a
    #: abandonné » inexprimable.
    #:
    #: Ce que le drapeau commande : l'auditeur est écarté de TOUTE population
    #: collective — jury, classement, PV, CNIM, effectifs, moyennes de classe
    #: et rangs des autres. Il reste un étudiant ordinaire partout ailleurs :
    #: classe, sous-groupe, emploi du temps, émargement, absences, saisie des
    #: notes, relevé. Le point d'entrée unique est `objects.cursus()`.
    #:
    #: Le piège n'est pas qu'il apparaisse où il ne devrait pas — cela se
    #: verrait. C'est qu'il DÉCALE SILENCIEUSEMENT le rang des autres : un
    #: auditeur troisième fait reculer d'un cran les cent quarante derrière
    #: lui, sur un PV signé comme sur les relevés déjà remis.
    auditeur_libre      = models.BooleanField(
        default=False,
        help_text="Suit la scolarité sans être de la promotion : ni jury, ni "
                  'classement, ni effectifs. Conserve notes et relevé.',
    )

    # ── Frais ────────────────────────────────────────────────────────────────
    # Mêmes champs que l'inscription administrative du socle, et même grille
    # tarifaire (`inscriptions.GrilleFrais`) : le montant n'est pas ressaisi à
    # la main, il se lit dans le tarif en vigueur. Le figer sur l'inscription
    # plutôt que de le recalculer à l'affichage est délibéré — un tarif révisé
    # en cours d'année ne doit pas changer ce qui a été facturé.
    montant_frais       = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    est_payee           = models.BooleanField(default=False)
    date_paiement       = models.DateField(null=True, blank=True)
    recu_paiement       = models.CharField(max_length=100, blank=True, default='')

    class Meta:
        db_table    = 'ipgei_inscription'
        ordering    = ['classe', 'numero_ordre', 'etudiant__nom']
        constraints = [
            # Un étudiant n'a qu'une inscription IPGEI par année universitaire.
            models.UniqueConstraint(
                fields=['etudiant', 'annee_universitaire'], name='uniq_ipgei_inscription_annee',
            ),
        ]
        indexes = [
            models.Index(fields=['annee_universitaire', 'statut']),
            models.Index(fields=['classe', 'actif']),
        ]

    def __str__(self):
        return f'{self.etudiant} — {self.classe}'

    @property
    def niveau(self) -> str:
        return self.classe.niveau_code

    def clean(self):
        if self.sous_groupe_id and self.classe_id and self.sous_groupe.classe_id != self.classe_id:
            raise ValidationError({'sous_groupe': "Le sous-groupe n'appartient pas à cette classe."})

    def save(self, *args, **kwargs):
        """
        Enregistre l'inscription et fait suivre le département de l'étudiant.

        Le module Absences du socle liste les étudiants d'une séance par
        `Etudiant.departement` — que l'inscription IPGEI ne renseignait pas.
        L'appel n'affichait donc PERSONNE, quelle que soit la classe, sans le
        moindre message : la classe était pleine côté IPGEI et vide côté appel.

        Le rattachement suit ici plutôt que de se saisir à part : deux endroits
        où dire dans quelle classe est un étudiant finissent par se contredire,
        et c'est l'inscription qui fait foi.
        """
        super().save(*args, **kwargs)
        if self.actif and self.classe_id and self.etudiant.departement_id != self.classe_id:
            self.etudiant.departement_id = self.classe_id
            self.etudiant.save(update_fields=['departement'])


class HistoriqueClasse(models.Model):
    """
    Journal des changements de classe. Append-only : c'est la preuve que le
    parcours de l'étudiant a été re-rattaché et non recréé.
    """
    inscription  = models.ForeignKey(
        InscriptionIPGEI, on_delete=models.CASCADE, related_name='historique_classes',
    )
    classe_avant = models.ForeignKey(
        Classe, on_delete=models.PROTECT, related_name='departs_historique',
    )
    classe_apres = models.ForeignKey(
        Classe, on_delete=models.PROTECT, related_name='arrivees_historique',
    )
    motif        = models.TextField(blank=True, default='')
    decide_par   = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='changements_classe_ipgei',
    )
    date_effet   = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ipgei_historique_classe'
        ordering = ['-date_effet']

    def __str__(self):
        return f'{self.inscription.etudiant} : {self.classe_avant} → {self.classe_apres}'

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise PermissionError("HistoriqueClasse est append-only — modification interdite.")
        super().save(*args, **kwargs)


# ═════════════════════════════════════════════════════════════════════════════
# 4. Matières : coefficient, volume horaire, pondération paramétrable
# ═════════════════════════════════════════════════════════════════════════════
class Matiere(models.Model):
    """
    Matière d'un semestre du cursus (Maths S1, Physique S3…).

    La pondération DS / TP / examen est réglée **par matière** et vaut pour
    toutes les classes du niveau (plan §4.2). Elle est recopiée dans chaque
    `Note` au moment du calcul : la modifier n'altère jamais l'existant.
    """
    institution     = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT, related_name='matieres_ipgei',
    )
    code            = models.CharField(max_length=20)
    intitule        = models.CharField(max_length=200)
    intitule_ar     = models.CharField(max_length=200, blank=True, default='')
    code_semestre   = models.CharField(
        max_length=2, choices=CODE_SEMESTRE_CHOICES,
        help_text='Semestre du cursus auquel la matière appartient.',
    )
    # Le semestre ne suffit plus à désigner le niveau : deux niveaux d'un même
    # rang — MP et MPI — partagent S3 et S4 tout en ayant des maquettes
    # distinctes. Nul le temps de la reprise des données ; la propriété
    # `niveau` retombe alors sur la déduction d'origine.
    niveau_ref      = models.ForeignKey(
        'ipgei.NiveauIPGEI', on_delete=models.PROTECT, null=True, blank=True,
        related_name='matieres',
        help_text='Niveau dont cette matière fait partie de la maquette.',
    )
    coefficient     = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal('1'),
        validators=[MinValueValidator(Decimal('0.25'))],
        help_text='Poids de la matière dans la moyenne semestrielle.',
    )
    volume_cm       = models.PositiveSmallIntegerField(
        default=0, help_text='Heures de cours magistral sur le semestre.',
    )
    volume_td       = models.PositiveSmallIntegerField(
        default=0, help_text='Heures de travaux dirigés sur le semestre.',
    )
    volume_tp       = models.PositiveSmallIntegerField(
        default=0, help_text='Heures de travaux pratiques sur le semestre.',
    )
    has_tp          = models.BooleanField(default=False, help_text='La matière comporte des TP notés.')
    #: MÊME ENSEIGNEMENT qu'une autre matière, présenté en deux lignes.
    #:
    #: En S3, « Mathématiques » et « Mathématiques 2 » ne sont pas deux
    #: matières : c'est un seul cours, un seul volume horaire, un seul
    #: professeur — la scolarité le scinde parce que chaque ligne porte SON
    #: examen et SON coefficient. Les devoirs surveillés et le TP, eux, sont
    #: communs : les DS sont composés une fois (il peut y en avoir plusieurs)
    #: et valent pour les deux, comme le TP.
    #:
    #: Cette clé désigne la matière PORTEUSE, celle où DS et TP se saisissent.
    #: La rattachée les lit chez elle ; rien n'est saisi deux fois, donc rien
    #: ne peut diverger. Seul l'examen reste propre à chacune.
    partage_avec    = models.ForeignKey(
        'self', on_delete=models.PROTECT, null=True, blank=True,
        related_name='matieres_rattachees',
        help_text="Même enseignement qu'une autre matière : les DS et le TP y "
                  'sont saisis une seule fois. Seuls les examens diffèrent.',
    )
    pct_ds          = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('30'))
    pct_tp          = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    pct_exam        = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('70'))
    ordre           = models.PositiveSmallIntegerField(default=0, help_text="Ordre d'affichage sur les relevés.")
    actif           = models.BooleanField(default=True)

    # Contrepartie SIGA de la matiere : les vacations et le suivi raisonnent en
    # `EM`. Dernier miroir subsistant — les classes, elles, heritent desormais
    # de `Departement` et n'ont plus de copie a maintenir.
    #: Le departement qui repond de la matiere. Deux a l'IPGEI, et c'est
    #: l'axe du perimetre des chefs de departement : le chef de ST ne voit
    #: que les matieres ST. Vide = aucun departement ne la revendique.
    DEPARTEMENT_ST      = 'ST'
    DEPARTEMENT_LANGUES = 'LANGUES'
    DEPARTEMENT_CHOICES = [
        (DEPARTEMENT_ST,      'Sciences et techniques'),
        (DEPARTEMENT_LANGUES, 'Langues'),
    ]
    departement     = models.CharField(
        max_length=10, choices=DEPARTEMENT_CHOICES, blank=True, default='',
        help_text='Departement disciplinaire : ST ou Langues.',
    )
    em_miroir       = models.OneToOneField(
        'em.EM', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='matiere_ipgei',
        help_text='EM SIGA equivalent, tenu a jour automatiquement.',
    )

    class Meta:
        db_table    = 'ipgei_matiere'
        ordering    = ['code_semestre', 'ordre', 'code']
        constraints = [
            # Le niveau entre dans la clé : « MATH S3 » existe légitimement deux
            # fois, une pour MP et une pour MPI.
            models.UniqueConstraint(
                fields=['institution', 'code', 'code_semestre', 'niveau_ref'],
                name='uniq_ipgei_matiere_niveau',
            ),
        ]
        indexes = [models.Index(fields=['code_semestre', 'actif'])]

    def __str__(self):
        return f'{self.code} — {self.intitule}'

    @property
    def niveau(self) -> str:
        """Code du niveau, du référentiel s'il est renseigné, déduit sinon."""
        if self.niveau_ref_id:
            return self.niveau_ref.code
        return NIVEAU_MPSI if self.code_semestre in SEMESTRES_PAR_NIVEAU[NIVEAU_MPSI] else NIVEAU_MP

    @property
    def volume_horaire(self) -> int:
        """
        Volume semestriel total.

        Calcule et non stocke : garder un total en base a cote du detail
        CM/TD/TP finirait toujours par diverger de ses composantes.
        """
        return (self.volume_cm or 0) + (self.volume_td or 0) + (self.volume_tp or 0)

    def ponderation(self) -> dict:
        return {'pct_ds': self.pct_ds, 'pct_tp': self.pct_tp, 'pct_exam': self.pct_exam}

    def clean(self):
        if not self.has_tp and self.pct_tp and Decimal(self.pct_tp) != Decimal('0'):
            raise ValidationError({'pct_tp': "Sans TP, la part TP doit valoir 0."})
        total = Decimal(self.pct_ds or 0) + Decimal(self.pct_tp or 0) + Decimal(self.pct_exam or 0)
        if total != Decimal('100'):
            raise ValidationError(
                f'La pondération doit totaliser 100 % (actuellement {total} %).'
            )


class PonderationClasse(models.Model):
    """
    CE QU'UNE SEULE CLASSE FAIT AUTREMENT — pondération, et coefficient.

    `Matiere` règle la pondération pour tout un niveau. C'est le cas général, et
    il tient presque toujours. Mais une classe peut composer autrement : en S4
    2025-2026, seule MP A a tenu une colle de maths — la colonne existe dans son
    fichier de notes, elle est absente en MP B et vide en MP C et MP D. La
    scolarité a donc compté 20/10/70 pour MP A et 30/0/70 pour les trois autres,
    et nos moyennes s'écartaient des siennes pour 29 étudiants.

    Cette table est celle des EXCEPTIONS. Vide, rien ne change. Une ligne, et
    cette classe-là déroge.

    Pourquoi une table plutôt qu'un champ de plus sur `Matiere` : la dérogation
    est un fait rare et daté, qui mérite son motif. Une colonne par cas d'espèce
    aurait grossi la matière sans jamais dire POURQUOI telle classe s'écarte.

    Le nom dit « pondération » parce que c'est le besoin qui l'a fait naître ;
    la table porte depuis un COEFFICIENT dérogatoire optionnel. Les deux tiennent
    dans la même ligne à dessein : ce sont deux façons de dire « cette classe-là
    s'écarte », et elles méritent un seul motif, un seul écran, une seule
    décision à relire. Renommer le modèle coûterait une migration et la réécriture
    de tous ses appelants pour ne gagner qu'un mot.

    Les résolveurs sont `services.ponderation_applicable(matiere, classe)` et
    `services.coefficient_applicable(matiere, classe)`, sur le modèle de
    `seuil_applicable` : la dérogation si elle existe, sinon la règle.

    ATTENTION — créer une ligne ne recalcule RIEN. `Note` fige sa pondération au
    moment du calcul (cf. `Note.pct_ds`), ce qui protège l'historique mais
    signifie qu'il faut relancer le calcul sur les notes concernées, verrous de
    jury levés puis rétablis.
    """
    matiere = models.ForeignKey(
        Matiere, on_delete=models.CASCADE, related_name='ponderations_classe',
    )
    classe  = models.ForeignKey(
        'ipgei.Classe', on_delete=models.CASCADE, related_name='ponderations',
    )
    pct_ds   = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('30'))
    pct_tp   = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    pct_exam = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('70'))
    #: La classe a-t-elle des TP notés dans cette matière ?
    #:
    #: Il ne suffit pas de changer les pourcentages : sans ce drapeau, l'écran
    #: de saisie refuse la fiche de TP et l'import écarte la colonne. La classe
    #: aurait une part TP de 10 % qu'elle ne pourrait jamais remplir.
    has_tp   = models.BooleanField(default=True)
    #: LE COEFFICIENT, quand la classe n'accorde pas le même poids à la matière.
    #:
    #: Vide — le cas normal — signifie « celui de la matière ». La dérogation
    #: porte alors la seule pondération, qui est sa raison d'être première.
    #:
    #: Ce n'est PAS la même nature de décision que la pondération. Celle-ci
    #: corrige la FIDÉLITÉ : MP A a tenu une colle, les autres non, et sa
    #: moyenne de maths reste comparable à la leur. Le coefficient est un choix
    #: de POLITIQUE : il change combien la matière pèse dans la moyenne
    #: générale, donc ce que le classement récompense — et `classement.py` met
    #: toutes les classes dans la MÊME liste pour le CNIM. D'où l'avertissement
    #: porté sur la feuille de classement dès qu'une classe classée en use.
    coefficient = models.DecimalField(
        max_digits=4, decimal_places=2, null=True, blank=True,
        help_text='Laisser vide pour garder le coefficient de la matière.',
    )
    #: POURQUOI cette classe déroge. Exigé : une dérogation sans motif devient,
    #: deux ans plus tard, une anomalie que personne n'ose corriger.
    motif    = models.CharField(
        max_length=300,
        help_text="Ce qui justifie l'écart — « colle de maths tenue en MP A "
                  'seulement », par exemple.',
    )
    cree_le  = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table    = 'ipgei_ponderation_classe'
        ordering    = ['matiere__code', 'classe__nom']
        verbose_name        = 'Pondération dérogatoire'
        verbose_name_plural = 'Pondérations dérogatoires'
        constraints = [
            models.UniqueConstraint(
                fields=['matiere', 'classe'], name='uniq_ipgei_ponderation_classe',
            ),
        ]

    def __str__(self):
        coef = f' · coef {self.coefficient:g}' if self.coefficient is not None else ''
        return (f'{self.matiere.code} · {self.classe.nom} — '
                f'{self.pct_ds:g}/{self.pct_tp:g}/{self.pct_exam:g}{coef}')

    def coefficient_effectif(self):
        """Le coefficient qui compte : celui de la dérogation, sinon la matière."""
        return self.coefficient if self.coefficient is not None else self.matiere.coefficient

    def ponderation(self) -> dict:
        """Même forme que `Matiere.ponderation()` : les deux sont interchangeables."""
        return {'pct_ds': self.pct_ds, 'pct_tp': self.pct_tp, 'pct_exam': self.pct_exam}

    def clean(self):
        # Mêmes règles que la matière — une dérogation reste une pondération.
        if not self.has_tp and self.pct_tp and Decimal(self.pct_tp) != Decimal('0'):
            raise ValidationError({'pct_tp': "Sans TP, la part TP doit valoir 0."})
        total = Decimal(self.pct_ds or 0) + Decimal(self.pct_tp or 0) + Decimal(self.pct_exam or 0)
        if total != Decimal('100'):
            raise ValidationError(
                f'La pondération doit totaliser 100 % (actuellement {total} %).'
            )
        # Une dérogation qui ne déroge pas est un piège : elle survivrait à un
        # changement de la règle générale et figerait l'ancienne valeur en
        # silence.
        if self.coefficient is not None and Decimal(self.coefficient) <= 0:
            raise ValidationError(
                {'coefficient': 'Un coefficient dérogatoire doit être strictement '
                                'positif. Laissez vide pour garder celui de la matière.'})
        # Une dérogation qui ne déroge pas est un piège : elle survivrait à un
        # changement de la règle générale et figerait l'ancienne valeur en
        # silence. Le coefficient entre dans ce jugement — une ligne dont SEUL
        # le coefficient s'écarte déroge bel et bien.
        coef_deroge = (self.matiere_id and self.coefficient is not None
                       and Decimal(self.coefficient) != Decimal(self.matiere.coefficient))
        if (self.matiere_id and not coef_deroge
                and self.has_tp == self.matiere.has_tp
                and self.ponderation() == self.matiere.ponderation()):
            raise ValidationError(
                f'Cette dérogation est identique à la règle de {self.matiere.code} : '
                'une dérogation qui ne déroge à rien figerait la règle générale '
                'sans le dire. Supprimez-la plutôt.'
            )
        if (self.classe_id and self.matiere_id
                and self.matiere.niveau_ref_id
                and self.classe.niveau_code != self.matiere.niveau_ref.code):
            raise ValidationError(
                f'{self.classe.nom} est en {self.classe.niveau_code}, or '
                f'{self.matiere.code} appartient à la maquette de '
                f'{self.matiere.niveau_ref.code}.'
            )


# ═════════════════════════════════════════════════════════════════════════════
# 5. Notes : DS 1..N + examens 1..N, pondération historisée
# ═════════════════════════════════════════════════════════════════════════════
class Note(models.Model):
    """
    Ligne de notation d'un étudiant dans une matière pour un semestre.

    Les DS et les examens sont des lignes filles (`Evaluation`) en nombre libre.
    Les colonnes `pct_*` et `coefficient` sont un **snapshot** figé au calcul :
    modifier plus tard la pondération d'une matière ne recalcule rien
    rétroactivement (plan §4.3, garantie « l'ancien reste figé »).
    """
    inscription      = models.ForeignKey(
        InscriptionIPGEI, on_delete=models.CASCADE, related_name='notes',
    )
    matiere          = models.ForeignKey(Matiere, on_delete=models.PROTECT, related_name='notes')
    semestre         = models.ForeignKey(
        SemestreIPGEI, on_delete=models.PROTECT, related_name='notes',
    )

    note_tp          = _note_field(null=True, blank=True, help_text='Note de TP /20.')
    note_rattrapage  = _note_field(
        null=True, blank=True,
        help_text='Note de rattrapage /20. La note retenue est max(moyenne, rattrapage), plafond appliqué.',
    )

    # ── Résultats calculés ───────────────────────────────────────────────────
    moy_ds           = _note_field(null=True, blank=True)
    moy_exam         = _note_field(null=True, blank=True)
    moyenne          = _note_field(null=True, blank=True, help_text='Moyenne pondérée avant rattrapage.')
    note_retenue     = _note_field(null=True, blank=True, help_text='Note finale après rattrapage.')

    # ── Snapshot de pondération (figé au calcul) ─────────────────────────────
    pct_ds           = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    pct_tp           = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    pct_exam         = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    coefficient      = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    plafond_rattrapage_applique = _note_field(null=True, blank=True)

    verrouillee      = models.BooleanField(
        default=False, help_text='Verrouillée par la délibération : plus aucune saisie.',
    )
    date_calcul      = models.DateTimeField(null=True, blank=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        db_table    = 'ipgei_note'
        ordering    = ['inscription', 'matiere__ordre', 'matiere__code']
        constraints = [
            models.UniqueConstraint(
                fields=['inscription', 'matiere', 'semestre'], name='uniq_ipgei_note',
            ),
        ]
        indexes = [
            models.Index(fields=['semestre', 'matiere']),
            models.Index(fields=['inscription', 'semestre']),
        ]

    def __str__(self):
        return f'{self.inscription.etudiant} — {self.matiere.code} ({self.semestre.code})'

    @property
    def ds(self):
        return self.evaluations.filter(type_evaluation=Evaluation.TYPE_DS).order_by('numero')

    @property
    def examens(self):
        return self.evaluations.filter(type_evaluation=Evaluation.TYPE_EXAM).order_by('numero')


class SessionEvaluationIPGEI(models.Model):
    """
    Fenêtre de saisie des notes d'un semestre. Deux par semestre : la session
    **normale** (DS, TP, examens) et la session de **rattrapage**.

    Le semestre porte déjà `est_cloture`, mais c'est un verrou d'un seul tenant :
    il ferme la saisie du rattrapage en même temps que celle des DS. Or le
    rattrapage se saisit précisément après l'arrêt des notes normales — il
    fallait donc rouvrir tout le semestre, et les DS redevenaient modifiables
    pendant ce temps. Les deux campagnes s'ouvrent et se ferment désormais
    séparément ; `Semestre.est_cloture` reste le verrou global au-dessus.

    Le plafond de rattrapage est **figé à la création** depuis les paramètres de
    l'institution, comme le fait déjà la délibération. Sans ce gel, un
    `recalculer-lot` postérieur à un changement de paramétrage réécrivait les
    notes retenues d'une campagne close.
    """
    TYPE_NORMALE    = 'normale'
    TYPE_RATTRAPAGE = 'rattrapage'
    TYPE_CHOICES    = [
        (TYPE_NORMALE,    'Session normale'),
        (TYPE_RATTRAPAGE, 'Session de rattrapage'),
    ]

    semestre     = models.ForeignKey(
        SemestreIPGEI, on_delete=models.CASCADE, related_name='sessions',
    )
    type_session = models.CharField(max_length=12, choices=TYPE_CHOICES)

    # Trois états réels : pas encore ouverte / ouverte / close. Close est
    # définitif tant qu'un administrateur ne réouvre pas explicitement.
    est_ouverte  = models.BooleanField(default=False)
    est_close    = models.BooleanField(default=False)

    date_debut_saisie = models.DateField(
        null=True, blank=True, help_text='Indicative — affichée aux enseignants.',
    )
    date_fin_saisie   = models.DateField(
        null=True, blank=True, help_text='Indicative — la clôture seule fait foi.',
    )

    # Snapshot, posé à la création depuis `ParametresIPGEI`. Vide = sans plafond.
    # Ne concerne que la session de rattrapage ; reste nul sur la normale.
    plafond_rattrapage = _note_field(null=True, blank=True)

    ouverte_le    = models.DateTimeField(null=True, blank=True)
    cloturee_le   = models.DateTimeField(null=True, blank=True)
    cloturee_par  = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sessions_ipgei_cloturees',
    )

    class Meta:
        db_table    = 'ipgei_session_evaluation'
        ordering    = ['semestre', 'type_session']
        verbose_name        = 'session de saisie IPGEI'
        verbose_name_plural = 'sessions de saisie IPGEI'
        constraints = [
            models.UniqueConstraint(
                fields=['semestre', 'type_session'], name='uniq_ipgei_session_evaluation',
            ),
        ]

    def __str__(self):
        return f'{self.get_type_session_display()} — {self.semestre}'

    @property
    def est_saisissable(self) -> bool:
        """Vrai si la campagne accepte des notes maintenant."""
        return self.est_ouverte and not self.est_close and not self.semestre.est_cloture

    @property
    def etat(self) -> str:
        """`fermee` (pas encore ouverte), `ouverte` ou `close`."""
        if self.est_close:
            return 'close'
        return 'ouverte' if self.est_ouverte else 'fermee'


class AnonymatIPGEI(models.Model):
    """
    Numéro d'anonymat d'un étudiant pour une campagne de saisie.

    Le correcteur saisit sous un numéro, jamais sous un nom : c'est ce qui rend
    la correction indépendante de l'identité du candidat. Le numéro vaut pour
    toute la session — un numéro par épreuve obligerait à redistribuer des
    étiquettes à chaque devoir surveillé, ce qu'aucune scolarité ne tient.

    Le couple (session, numéro) est unique, comme (session, étudiant) : deux
    étudiants sous le même numéro rendraient la correspondance ambiguë au
    moment de reporter les notes.
    """
    session     = models.ForeignKey(
        'ipgei.SessionEvaluationIPGEI', on_delete=models.CASCADE, related_name='anonymats',
    )
    inscription = models.ForeignKey(
        InscriptionIPGEI, on_delete=models.CASCADE, related_name='anonymats',
    )
    numero      = models.PositiveIntegerField()
    genere_le   = models.DateTimeField(auto_now_add=True)
    genere_par  = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='anonymats_ipgei_generes',
    )

    class Meta:
        db_table    = 'ipgei_anonymat'
        ordering    = ['session', 'numero']
        verbose_name        = "numéro d'anonymat IPGEI"
        verbose_name_plural = "numéros d'anonymat IPGEI"
        constraints = [
            models.UniqueConstraint(
                fields=['session', 'inscription'], name='uniq_ipgei_anonymat_inscrit',
            ),
            models.UniqueConstraint(
                fields=['session', 'numero'], name='uniq_ipgei_anonymat_numero',
            ),
        ]

    def __str__(self):
        return f'Anonymat {self.numero} — {self.session}'


class _IntervalleEpreuve:
    """
    Ce qu'une épreuve sait de son propre horaire : sa fin, sa durée lisible, et
    les créneaux qu'elle recouvre.

    Partagé entre l'épreuve vivante et son ARCHIVE. Recopier ce calcul aurait
    laissé les deux dériver — et un document réimprimé ne recouvrirait plus les
    mêmes créneaux que l'original, ce qui vide l'archive de son sens.

    Le porteur doit offrir `heure_debut` et `duree_minutes`.
    """

    @property
    def debut_minutes(self) -> int:
        """Début en minutes depuis minuit — la forme dans laquelle on compare."""
        return self.heure_debut.hour * 60 + self.heure_debut.minute

    @property
    def fin_minutes(self) -> int:
        return self.debut_minutes + self.duree_minutes

    @property
    def heure_fin(self) -> time:
        """
        Fin de l'épreuve. Bornée à 23h59 : une épreuve qui déborderait sur le
        lendemain n'existe pas, et laisser l'heure reboucler sur 00h30
        afficherait une fin AVANT le début.
        """
        fin = min(self.fin_minutes, 23 * 60 + 59)
        return time(fin // 60, fin % 60)

    @property
    def duree_libelle(self) -> str:
        """« 3h » ou « 2h30 » — ce qui se lit sur le bandeau."""
        h, m = divmod(self.duree_minutes, 60)
        return f'{h}h{m:02d}' if m else f'{h}h'

    def recouvre(self, creneau) -> bool:
        """
        Vrai si l'épreuve empiète, ne serait-ce que d'une minute, sur ce créneau.

        Le créneau est un libellé — « 09h45-11h15 » —, pas deux champs d'heure :
        c'est de là qu'on lit ses bornes. Un libellé qui ne se laisse pas lire
        ne bloque rien, plutôt que de bloquer tout par excès de prudence.

        Le recouvrement PARTIEL suffit. Un DS de 8h00 à 10h00 ne mord que 15
        minutes sur le créneau de 09h45-11h15, mais personne ne peut y
        commencer un cours : la classe compose encore.
        """
        bornes = _bornes_creneau(getattr(creneau, 'creneau', '') or '')
        if bornes is None:
            return False
        debut_c, fin_c = bornes
        return self.debut_minutes < fin_c and debut_c < self.fin_minutes


class EpreuveIPGEI(_IntervalleEpreuve, models.Model):
    """
    Une épreuve : le DS de maths du 18 novembre, 8h00, 3h, pour MP A à MP E.

    Le chaînon qui manquait. `SessionEvaluationIPGEI` n'est qu'une fenêtre de
    saisie, et `Evaluation` est la note d'UN étudiant : la composition
    elle-même — sa date, son heure, sa durée, les classes qui y sont
    convoquées — n'était représentée nulle part. Faute de mieux on la posait
    dans la grille d'emploi du temps, où elle n'entre pas :

    - **Sa durée ne tombe pas sur la grille.** Les créneaux font 1h30 ; une
      épreuve fait 2h, 3h ou 4h. Aucune de ces durées n'est un multiple de
      1h30, donc « la séance occupe N cases » ne peut pas marcher. D'où
      `heure_debut` + `duree_minutes`, qui disent l'heure vraie ; les créneaux
      recouverts se CALCULENT, ils ne se saisissent pas.
    - **Elle concerne plusieurs classes à la fois.** `SeanceReelle.classe` est
      une clé simple : il aurait fallu cinq cases dans cinq grilles, cinq fois
      modifiables séparément — avec la certitude qu'une soit oubliée le jour
      où l'heure change.
    - **Elle n'a ni salle ni enseignant.** Chaque classe compose dans sa propre
      salle, répartie plus tard sur les fiches, quand l'effectif et l'anonymat
      sont connus. Et le surveillant relève de la vacation : `avancement`
      compte déjà DS, EF et ER en `Surveillance_total`, jamais en heures de
      cours, et `avancement/par_groupe.py` les exclut du programme.

    L'emploi du temps ne la contient donc pas : il l'affiche en surimpression
    et neutralise les créneaux que son intervalle recouvre.
    """
    TYPE_DS = 'DS'
    TYPE_EF = 'EF'
    TYPE_ER = 'ER'
    # Dérivés de `core.types_seance`, seul endroit où ces codes et leurs
    # libellés sont écrits. Les redire ici les aurait laissés se séparer —
    # c'est ce qui était arrivé avec `secretariat.LIBELLES_EPREUVE`, qui
    # nommait le rattrapage « Épreuve » quand ce modèle disait « Examen ».
    TYPE_CHOICES = list(LIBELLES_EPREUVE.items())

    # La session porte le semestre ET la campagne : un ER appartient à la
    # session de rattrapage, un DS à la normale. Rattacher l'épreuve au
    # semestre aurait laissé cette distinction implicite.
    session = models.ForeignKey(
        SessionEvaluationIPGEI, on_delete=models.CASCADE, related_name='epreuves',
    )
    matiere = models.ForeignKey(
        Matiere, on_delete=models.PROTECT, related_name='epreuves',
    )
    type_epreuve = models.CharField(max_length=2, choices=TYPE_CHOICES)
    numero = models.PositiveSmallIntegerField(
        default=1, help_text='DS 1, DS 2… ; 1 pour un examen unique.',
    )

    date        = models.DateField()
    heure_debut = models.TimeField()
    duree_minutes = models.PositiveSmallIntegerField(
        default=120,
        help_text='Durée réelle en minutes : 120 = 2h, 180 = 3h, 240 = 4h.',
    )

    classes = models.ManyToManyField(
        Classe, through='EpreuveClasse', related_name='epreuves',
    )
    observations = models.CharField(max_length=300, blank=True, default='')

    cree_le  = models.DateTimeField(auto_now_add=True)
    cree_par = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='epreuves_ipgei_creees',
    )

    class Meta:
        db_table = 'ipgei_epreuve'
        ordering = ['date', 'heure_debut', 'matiere']
        verbose_name        = 'épreuve IPGEI'
        verbose_name_plural = 'épreuves IPGEI'
        constraints = [
            # Deux fois « DS 1 de maths » dans la même campagne serait une
            # saisie en double, pas deux épreuves.
            models.UniqueConstraint(
                fields=['session', 'matiere', 'type_epreuve', 'numero'],
                name='uniq_ipgei_epreuve',
            ),
            models.CheckConstraint(
                check=models.Q(duree_minutes__gte=15) & models.Q(duree_minutes__lte=480),
                name='ck_ipgei_epreuve_duree',
            ),
        ]
        indexes = [models.Index(fields=['date'])]

    def __str__(self):
        return (f'{self.type_epreuve} {self.numero} {self.matiere.code} — '
                f'{self.date} {self.heure_debut:%H:%M}')

    @property
    def intitule_affiche(self) -> str:
        """Ce que la case porte. L'archive expose la même chose, sans FK."""
        return self.matiere.intitule if self.matiere_id else self.type_epreuve


#: Les jours dans l'ordre de `date.weekday()`. Le référentiel `Jour` est
#: libellé en toutes lettres et son ordre n'est pas garanti : le nom est la
#: seule correspondance stable entre une date et une ligne de grille.
JOURS_SEMAINE = ('lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi',
                 'dimanche')


def ancrage_epreuve(epreuve, creneaux, jours):
    """
    Le couple (jour, créneau) où la ligne de suivi d'une épreuve se pose.

    UN SEUL ENDROIT où ce choix est fait. Il sert à deux calculs qui doivent
    tomber d'accord, faute de quoi une semaine qui porte une épreuve se déclare
    éternellement divergente :

      * `miroir._lignes_epreuves`, qui PROJETTE l'épreuve dans `Emplois`, d'où
        naîtra sa ligne de `Suivie` ;
      * `SemaineIPGEI._empreinte_edt`, qui doit RETROUVER cette même ligne pour
        la reconnaître dans l'emploi du temps.

    L'ancrage se fait sur le PREMIER créneau recouvert : un examen de 4h en
    couvre trois, et une ligne par créneau demanderait trois pointages pour une
    seule épreuve.

    Rend `(None, None)` quand l'épreuve ne recouvre aucun créneau — logée dans
    une pause — ou que le jour est introuvable : elle n'a alors aucune case où
    se poser, ni dans l'une ni dans l'autre empreinte.

    `creneaux` et `jours` sont passés par l'appelant, qui boucle : les relire à
    chaque épreuve ferait deux requêtes par ligne.
    """
    creneau = next((c for c in creneaux if epreuve.recouvre(c)), None)
    jour = jours.get(JOURS_SEMAINE[epreuve.date.weekday()])
    if creneau is None or jour is None:
        return None, None
    return jour, creneau


def _bornes_creneau(libelle: str):
    """
    Les bornes d'un créneau, en minutes depuis minuit, lues dans son libellé.

    Accepte « 08h00-09h30 », « 08:00 - 09:30 », « 8h-9h30 ». Rend `None` si le
    libellé ne se laisse pas lire — au vu de la table `creneau`, qui est un
    référentiel libre partagé avec le socle, c'est un cas à prévoir.
    """
    trouves = re.findall(r'(\d{1,2})\s*[h:]\s*(\d{2})?', libelle or '')
    if len(trouves) < 2:
        return None
    try:
        debut = int(trouves[0][0]) * 60 + int(trouves[0][1] or 0)
        fin   = int(trouves[1][0]) * 60 + int(trouves[1][1] or 0)
    except (TypeError, ValueError):
        return None
    return (debut, fin) if fin > debut else None


class EpreuveClasse(models.Model):
    """
    Une classe convoquée à une épreuve.

    Table de liaison explicite, et non un `ManyToManyField` nu, pour une raison
    précise : chaque classe compose dans SA salle, à la même heure que les
    autres. Cette salle se décide sur les fiches de répartition, pas ici — le
    champ reste donc vide et hors des écrans. Mais il a sa place dès
    maintenant : convertir plus tard une liaison nue en table explicite est une
    migration pénible, alors que la laisser inutilisée ne coûte rien.
    """
    epreuve = models.ForeignKey(
        EpreuveIPGEI, on_delete=models.CASCADE, related_name='convocations',
    )
    classe = models.ForeignKey(
        Classe, on_delete=models.PROTECT, related_name='convocations_epreuve',
    )
    salle = models.ForeignKey(
        'salle.Salle', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='epreuves_ipgei',
        help_text='Renseignée lors de la répartition sur fiches, le cas échéant.',
    )

    class Meta:
        db_table = 'ipgei_epreuve_classe'
        ordering = ['epreuve', 'classe']
        constraints = [
            models.UniqueConstraint(
                fields=['epreuve', 'classe'], name='uniq_ipgei_epreuve_classe',
            ),
        ]

    def __str__(self):
        return f'{self.classe.nom} — {self.epreuve}'


class Evaluation(models.Model):
    """Un DS ou un examen d'une ligne de note. Nombre libre (1..N) par type."""
    TYPE_DS   = 'ds'
    TYPE_EXAM = 'exam'
    TYPE_CHOICES = [(TYPE_DS, 'Devoir surveillé'), (TYPE_EXAM, 'Examen')]

    note            = models.ForeignKey(Note, on_delete=models.CASCADE, related_name='evaluations')
    type_evaluation = models.CharField(max_length=4, choices=TYPE_CHOICES)
    numero          = models.PositiveSmallIntegerField(help_text='DS 1, DS 2… / Examen 1, Examen 2…')
    valeur          = _note_field()
    date_evaluation = models.DateField(null=True, blank=True)
    saisie_par      = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='evaluations_ipgei_saisies',
    )
    date_saisie     = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table    = 'ipgei_evaluation'
        ordering    = ['note', 'type_evaluation', 'numero']
        constraints = [
            models.UniqueConstraint(
                fields=['note', 'type_evaluation', 'numero'], name='uniq_ipgei_evaluation',
            ),
        ]

    def __str__(self):
        return f'{self.get_type_evaluation_display()} {self.numero} : {self.valeur}'

    def libelle_audit(self) -> str:
        """
        Le libellé lu dans le journal d'audit.

        `__str__` sert l'affichage courant, où l'étudiant et la matière sont
        déjà à l'écran. Au journal ils n'y sont pas : « Devoir surveillé 1 :
        12.50 » ne dirait ni de qui ni dans quoi. Une seule requête, celle qui
        remonte la note et ses deux libellés.
        """
        try:
            note = (Note.objects
                    .select_related('inscription__etudiant', 'matiere')
                    .get(pk=self.note_id))
            etudiant = note.inscription.etudiant
            qui = f'{etudiant.matricule} {etudiant.nom}'.strip()
            return (f'{self.get_type_evaluation_display()} {self.numero} : '
                    f'{self.valeur} — {qui} / {note.matiere.code}')
        except Exception:                                          # noqa: BLE001
            return str(self)


# ═════════════════════════════════════════════════════════════════════════════
# 6. Délibération IPGEI
# ═════════════════════════════════════════════════════════════════════════════
class Deliberation(models.Model):
    """
    Jury de fin de semestre ou de fin d'année, pour une CLASSE et une année.

    Le jury siège classe par classe : c'est la classe qui a un professeur
    principal, un effectif qu'on parcourt en séance, et un procès-verbal que
    l'on signe. Délibérer sur tout un niveau mêlait deux classes dans un même
    classement, où le rang d'un élève dépendait d'élèves qu'il ne côtoie pas.

    Le seuil et le plafond de rattrapage sont **copiés depuis les paramètres à
    la création** puis figés dans la délibération : rejouer un calcul donne
    toujours le même résultat, même si le paramétrage a bougé entre-temps.
    """
    PORTEE_SEMESTRE  = 'semestre'
    PORTEE_ANNUELLE  = 'annuelle'
    #: Le second jury de deuxième année, tenu après la session de rattrapage.
    #: C'est une délibération À PART, et non une reprise de l'annuelle : deux
    #: jurys siègent, à deux dates, et chacun signe son procès-verbal. Le PV
    #: annuel dit « X est autorisé au rattrapage » ; celui-ci dit ce qu'il en
    #: est advenu. Rouvrir le premier effacerait la première décision.
    PORTEE_RATTRAPAGE = 'rattrapage'
    PORTEE_CHOICES  = [
        (PORTEE_SEMESTRE,  'Semestre'),
        (PORTEE_ANNUELLE,  'Annuelle'),
        (PORTEE_RATTRAPAGE, 'Rattrapage'),
    ]

    STATUT_BROUILLON = 'brouillon'
    STATUT_CALCULEE  = 'calculee'
    STATUT_VALIDEE   = 'validee'
    STATUT_CHOICES   = [
        (STATUT_BROUILLON, 'Brouillon'),
        (STATUT_CALCULEE,  'Calculée'),
        (STATUT_VALIDEE,   'Validée'),
    ]

    institution         = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT, related_name='deliberations_ipgei',
    )
    libelle             = models.CharField(max_length=200, blank=True, default='')
    # Code du niveau délibéré. Sans `choices` : la liste vit dans
    # `NiveauIPGEI`, et un niveau ajouté après coup doit pouvoir délibérer. Le
    # serializer valide contre le référentiel.
    niveau              = models.CharField(max_length=10)
    #: Classe délibérée. Nullable pour les jurys tenus avant que la règle ne
    #: change — eux portent tout le niveau, et leurs PV déjà signés ne doivent
    #: pas se mettre à dire autre chose. Toute nouvelle délibération en exige
    #: une.
    classe              = models.ForeignKey(
        'ipgei.Classe', on_delete=models.PROTECT, null=True, blank=True,
        related_name='deliberations',
    )
    annee_universitaire = models.CharField(max_length=9)
    portee              = models.CharField(max_length=12, choices=PORTEE_CHOICES, default=PORTEE_ANNUELLE)
    #: D'où vient la cohorte d'un jury de RATTRAPAGE : le jury annuel qui l'a
    #: renvoyée au rattrapage. Le lien n'est pas décoratif — il désigne le
    #: périmètre exact (les lignes « autorisé au rattrapage ») et interdit
    #: d'ouvrir deux fois le même second jury.
    jury_source         = models.ForeignKey(
        'self', on_delete=models.PROTECT, null=True, blank=True,
        related_name='jurys_rattrapage',
        help_text='Jury annuel dont ce rattrapage reprend les étudiants.',
    )
    semestre            = models.ForeignKey(
        SemestreIPGEI, on_delete=models.PROTECT, null=True, blank=True,
        related_name='deliberations',
        help_text='Requis si portée = semestre ; ignoré en portée annuelle.',
    )

    # ── Paramètres figés ─────────────────────────────────────────────────────
    seuil_validation    = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal('10'),
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('20'))],
    )
    plafond_rattrapage  = _note_field(null=True, blank=True)

    statut              = models.CharField(max_length=10, choices=STATUT_CHOICES, default=STATUT_BROUILLON)
    date_creation       = models.DateTimeField(auto_now_add=True)
    date_calcul         = models.DateTimeField(null=True, blank=True)
    date_validation     = models.DateTimeField(null=True, blank=True)
    validee_par         = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='deliberations_ipgei_validees',
    )
    observations        = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'ipgei_deliberation'
        ordering = ['-annee_universitaire', 'niveau', 'portee']
        indexes  = [models.Index(fields=['annee_universitaire', 'niveau', 'statut'])]

    def __str__(self):
        cible = self.semestre.code if self.semestre_id else self.annee_universitaire
        quoi = ('Jury de rattrapage' if self.portee == self.PORTEE_RATTRAPAGE
                else 'Délibération')
        return f'{quoi} {self.classe.nom if self.classe_id else self.niveau} {cible}'

    def clean(self):
        if self.portee == self.PORTEE_SEMESTRE and not self.semestre_id:
            raise ValidationError({'semestre': 'Une délibération de semestre doit cibler un semestre.'})
        if self.portee == self.PORTEE_RATTRAPAGE and not self.jury_source_id:
            raise ValidationError(
                {'jury_source': "Un jury de rattrapage reprend les étudiants d'un "
                                "jury annuel : il ne s'ouvre pas seul."})
        if (self.jury_source_id
                and self.jury_source.portee != self.PORTEE_ANNUELLE):
            raise ValidationError(
                {'jury_source': 'Le rattrapage suit un jury ANNUEL — un jury de '
                                "semestre ne prononce rien qui l'appelle."})
        if self.classe_id and self.classe.niveau_code != self.niveau:
            raise ValidationError(
                {'classe': f'{self.classe.nom} relève du niveau '
                           f'{self.classe.niveau_code}, pas de {self.niveau}.'})

    @property
    def est_verrouillee(self) -> bool:
        return self.statut == self.STATUT_VALIDEE


class LigneDeliberation(models.Model):
    """
    Résultat d'un étudiant dans une délibération.

    `decision_auto` conserve la proposition du calcul ; `decision` est ce que
    le jury retient. Quand les deux diffèrent, `motif_ajustement` est exigé —
    l'écart reste ainsi toujours justifié et auditable.
    """
    # 1re année (MPSI) : admis ou réorienté — ni rattrapage, ni redoublement.
    DECISION_ADMIS      = 'admis'
    DECISION_REORIENTE  = 'reoriente'
    # 2e année (MP). Le jury ANNUEL n'a que deux issues : le seuil est atteint
    # et l'étudiant concourt, ou il ne l'est pas et il passe par le rattrapage.
    # On ne va JAMAIS au concours sans passer par là, et le redoublement ne se
    # prononce pas d'emblée — il ne vient qu'après le rattrapage, ou après un
    # échec au concours.
    DECISION_CNIM       = 'autorise_cnim'
    DECISION_RATTRAPAGE = 'autorise_rattrapage'
    # Prononcés par le jury de RATTRAPAGE, ou après le résultat du concours,
    # selon que le droit de redoublement est encore ouvert.
    DECISION_REDOUBLANT = 'redoublant'
    DECISION_CHOICES    = [
        (DECISION_ADMIS,      'Admis en 2e année'),
        (DECISION_REORIENTE,  'Réorienté'),
        (DECISION_CNIM,       'Autorisé à concourir (CNIM)'),
        (DECISION_RATTRAPAGE, 'Autorisé au rattrapage'),
        (DECISION_REDOUBLANT, 'Redoublant'),
    ]

    deliberation      = models.ForeignKey(
        Deliberation, on_delete=models.CASCADE, related_name='lignes',
    )
    inscription       = models.ForeignKey(
        InscriptionIPGEI, on_delete=models.PROTECT, related_name='lignes_deliberation',
    )
    moyenne_generale  = _note_field(null=True, blank=True)
    total_coefficients = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    rang              = models.PositiveSmallIntegerField(null=True, blank=True)
    decision_auto     = models.CharField(max_length=24, choices=DECISION_CHOICES, blank=True, default='')
    decision          = models.CharField(max_length=24, choices=DECISION_CHOICES, blank=True, default='')
    motif_ajustement  = models.TextField(blank=True, default='')
    observations      = models.TextField(blank=True, default='')

    # ── État de l'inscription avant validation ───────────────────────────────
    # Écrits au moment de la validation, quand la décision est reportée sur
    # l'inscription. Sans eux, dévalider un jury ne saurait pas quoi restaurer :
    # remettre « actif » d'office effacerait un redoublement antérieur, et
    # décrémenter à l'aveugle fausserait le droit au redoublement.
    # Même largeur que `InscriptionIPGEI.statut` : c'est une COPIE de ce champ,
    # et la laisser plus courte faisait échouer la validation dès qu'un statut
    # dépassait quinze caractères — « autorise_rattrapage » en fait dix-neuf.
    statut_avant           = models.CharField(max_length=24, blank=True, default='')
    nb_redoublements_avant = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        db_table    = 'ipgei_ligne_deliberation'
        ordering    = ['deliberation', 'rang', '-moyenne_generale']
        constraints = [
            models.UniqueConstraint(
                fields=['deliberation', 'inscription'], name='uniq_ipgei_ligne_deliberation',
            ),
        ]

    def __str__(self):
        return f'{self.inscription.etudiant} — {self.get_decision_display() or "sans décision"}'

    @property
    def est_ajustee(self) -> bool:
        return bool(self.decision) and self.decision != self.decision_auto


class MembreJuryIPGEI(models.Model):
    """
    Membre signataire du procès-verbal d'une délibération.

    Le PV n'engage que par les personnes qui l'ont signé : sans cette table, le
    document sortait avec une seule signature d'établissement, alors qu'une
    délibération est un acte collégial. La signature est datée et rattachée à un
    compte — on ne signe que pour soi.
    """
    ROLE_PRESIDENT  = 'president'
    ROLE_DIRECTEUR  = 'directeur'
    ROLE_ENSEIGNANT = 'enseignant'
    ROLE_SECRETAIRE = 'secretaire'
    ROLE_CHOICES    = [
        (ROLE_PRESIDENT,  'Président du jury'),
        (ROLE_DIRECTEUR,  "Directeur des études"),
        (ROLE_ENSEIGNANT, 'Enseignant'),
        (ROLE_SECRETAIRE, 'Secrétaire de séance'),
    ]
    # Ordre de présentation sur le PV : le rang protocolaire, pas l'ordre de
    # saisie ni l'alphabet.
    ORDRE_ROLES = {ROLE_PRESIDENT: 0, ROLE_DIRECTEUR: 1, ROLE_ENSEIGNANT: 2,
                   ROLE_SECRETAIRE: 3}

    deliberation = models.ForeignKey(
        Deliberation, on_delete=models.CASCADE, related_name='membres_jury',
    )
    utilisateur  = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.PROTECT,
        related_name='jurys_ipgei',
    )
    role         = models.CharField(max_length=15, choices=ROLE_CHOICES)
    signature_le = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table    = 'ipgei_membre_jury'
        ordering    = ['deliberation', 'id']
        verbose_name        = 'membre du jury IPGEI'
        verbose_name_plural = 'membres du jury IPGEI'
        constraints = [
            models.UniqueConstraint(
                fields=['deliberation', 'utilisateur'], name='uniq_ipgei_membre_jury',
            ),
        ]

    def __str__(self):
        return f'{self.get_role_display()} — {self.utilisateur}'

    @property
    def a_signe(self) -> bool:
        return self.signature_le is not None


# ═════════════════════════════════════════════════════════════════════════════
# Fraudes — sanction des notes, justifiée par un PV
# ═════════════════════════════════════════════════════════════════════════════
class SanctionFraude(models.Model):
    """
    UNE FRAUDE CONSTATÉE, ET CE QU'ON EN A FAIT AUX NOTES.

    Un étudiant pris avec un téléphone, une antisèche, un voisin qui souffle :
    la fiche nomme la nature des faits, en écrit le motif, joint le PV de
    délibération qui la fonde, et désigne les notes à mettre à zéro — une
    matière entière ou une seule épreuve (cf. `ElementSanctionne`).

    ELLE NE PRONONCE AUCUNE DÉCISION DE PASSAGE. Elle pénalise des notes ; c'est
    le jury qui décide ensuite, en voyant la fiche et son PV. Une note arrêtée
    par un jury validé peut être sanctionnée malgré son verrou — une fraude se
    découvre souvent après la délibération —, et ce jury est alors signalé
    « à redélibérer ».

    ELLE SE LÈVE, ET NE S'EFFACE PAS. Une erreur, un recours accepté : les
    valeurs d'origine, gardées dans chaque élément, sont remises. La fiche reste,
    marquée levée, avec son motif de levée — une sanction qui disparaîtrait sans
    trace laisserait des notes changer deux fois sans explication.
    """
    NATURE_TELEPHONE     = 'telephone'
    NATURE_DOCUMENT      = 'document'
    NATURE_COMMUNICATION = 'communication'
    NATURE_SUBSTITUTION  = 'substitution'
    NATURE_AUTRE         = 'autre'
    NATURE_CHOICES = [
        (NATURE_TELEPHONE,     'Téléphone ou appareil électronique'),
        (NATURE_DOCUMENT,      'Document non autorisé (antisèche)'),
        (NATURE_COMMUNICATION, 'Communication avec un autre candidat'),
        (NATURE_SUBSTITUTION,  'Substitution de copie ou de personne'),
        (NATURE_AUTRE,         'Autre'),
    ]
    STATUT_APPLIQUEE = 'appliquee'
    STATUT_LEVEE     = 'levee'
    STATUT_CHOICES = [
        (STATUT_APPLIQUEE, 'Appliquée'),
        (STATUT_LEVEE,     'Levée'),
    ]

    inscription = models.ForeignKey(
        InscriptionIPGEI, on_delete=models.PROTECT, related_name='sanctions_fraude',
    )
    nature      = models.CharField(max_length=20, choices=NATURE_CHOICES)
    date_faits  = models.DateField()
    #: Obligatoire : une note mise à zéro sans motif écrit devient, un an plus
    #: tard, une anomalie que personne ne sait expliquer.
    motif       = models.TextField()
    #: Le PV de délibération qui fonde la sanction. Obligatoire, pour la même
    #: raison, et contrôlé comme toute pièce jointe (PDF, JPEG, PNG, 5 Mo).
    pv_deliberation = models.FileField(
        upload_to='ipgei/fraudes/', validators=[validate_document],
    )
    statut      = models.CharField(max_length=10, choices=STATUT_CHOICES,
                                   default=STATUT_APPLIQUEE)
    cree_par    = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sanctions_fraude_prononcees',
    )
    cree_le     = models.DateTimeField(auto_now_add=True)
    motif_levee = models.TextField(blank=True, default='')
    levee_par   = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sanctions_fraude_levees',
    )
    levee_le    = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'ipgei_sanction_fraude'
        ordering = ['-cree_le']
        verbose_name        = 'sanction pour fraude'
        verbose_name_plural = 'sanctions pour fraude'

    def __str__(self):
        return (f'Fraude — {self.inscription.etudiant.matricule} '
                f'({self.get_nature_display()}, {self.date_faits})')

    @property
    def est_active(self) -> bool:
        return self.statut == self.STATUT_APPLIQUEE

    @property
    def date_derniere_action(self):
        """Ce que le jury n'a pas vu : la pose, puis la levée s'il y en a une."""
        return self.levee_le or self.cree_le


class ElementSanctionne(models.Model):
    """
    Une note visée par la fiche : la matière entière, ou une seule épreuve.

    `valeurs_origine` garde ce que la sanction a remplacé — c'est ce qui permet
    de la lever en rendant EXACTEMENT les notes d'avant, y compris une épreuve
    que l'étudiant n'avait pas rendue et que la sanction a dû créer.

    Seules des épreuves que la CLASSE a passées sont sanctionnables : le
    diviseur d'une moyenne est le nombre d'épreuves tenues par la classe, et en
    inventer une pour un seul étudiant changerait la moyenne de tous.
    """
    PORTEE_MATIERE = 'matiere'
    PORTEE_EPREUVE = 'epreuve'
    PORTEE_CHOICES = [
        (PORTEE_MATIERE, 'Matière entière'),
        (PORTEE_EPREUVE, 'Une épreuve'),
    ]
    COMPOSANTE_DS         = 'ds'
    COMPOSANTE_EXAM       = 'exam'
    COMPOSANTE_TP         = 'tp'
    COMPOSANTE_RATTRAPAGE = 'rattrapage'
    COMPOSANTE_CHOICES = [
        (COMPOSANTE_DS,         'Devoir surveillé'),
        (COMPOSANTE_EXAM,       'Examen'),
        (COMPOSANTE_TP,         'TP'),
        (COMPOSANTE_RATTRAPAGE, 'Rattrapage'),
    ]

    sanction   = models.ForeignKey(SanctionFraude, on_delete=models.CASCADE,
                                   related_name='elements')
    note       = models.ForeignKey(Note, on_delete=models.PROTECT,
                                   related_name='elements_sanctionnes')
    portee     = models.CharField(max_length=10, choices=PORTEE_CHOICES)
    #: Vide pour la matière entière.
    composante = models.CharField(max_length=12, choices=COMPOSANTE_CHOICES,
                                  blank=True, default='')
    #: Rang du DS ou de l'examen ; vide pour le TP et le rattrapage.
    numero     = models.PositiveSmallIntegerField(null=True, blank=True)
    valeurs_origine = models.JSONField(default=dict, blank=True)
    note_retenue_avant = models.DecimalField(max_digits=5, decimal_places=2,
                                             null=True, blank=True)

    class Meta:
        db_table = 'ipgei_element_sanctionne'
        ordering = ['note__semestre__code', 'note__matiere__code']

    def __str__(self):
        return f'{self.note.matiere.code} — {self.libelle}'

    @property
    def libelle(self) -> str:
        if self.portee == self.PORTEE_MATIERE:
            return 'matière entière'
        rang = f' {self.numero}' if self.numero else ''
        return f'{self.get_composante_display()}{rang}'


class DetailLigneDeliberation(models.Model):
    """
    Photo par matière de la moyenne semestrielle utilisée par la délibération.
    Sert à imprimer le relevé exactement tel que le jury l'a vu, même si une
    note est corrigée après coup.
    """
    ligne        = models.ForeignKey(
        LigneDeliberation, on_delete=models.CASCADE, related_name='details',
    )
    matiere      = models.ForeignKey(Matiere, on_delete=models.PROTECT, related_name='+')
    semestre     = models.ForeignKey(SemestreIPGEI, on_delete=models.PROTECT, related_name='+')
    note_retenue = _note_field(null=True, blank=True)
    coefficient  = models.DecimalField(max_digits=4, decimal_places=2)

    class Meta:
        db_table    = 'ipgei_detail_ligne_deliberation'
        ordering    = ['ligne', 'semestre__code', 'matiere__ordre']
        constraints = [
            models.UniqueConstraint(
                fields=['ligne', 'matiere', 'semestre'], name='uniq_ipgei_detail_ligne',
            ),
        ]

    def __str__(self):
        return f'{self.ligne_id} — {self.matiere.code} : {self.note_retenue}'


# ═════════════════════════════════════════════════════════════════════════════
# 7. EDT hebdomadaire : grille type → séances réelles (Phase 2)
# ═════════════════════════════════════════════════════════════════════════════
class GrilleType(models.Model):
    """
    Modèle d'emploi du temps d'une classe pour un type de semestre (impair /
    pair). C'est le patron dupliqué sur chaque semaine du semestre.
    """
    classe        = models.ForeignKey(Classe, on_delete=models.CASCADE, related_name='grilles')
    type_semestre = models.CharField(max_length=1, choices=TYPE_SEMESTRE_CHOICES, default='I')
    libelle       = models.CharField(max_length=100, blank=True, default='')
    actif         = models.BooleanField(default=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table    = 'ipgei_grille_type'
        ordering    = ['classe', 'type_semestre']
        constraints = [
            models.UniqueConstraint(fields=['classe', 'type_semestre'], name='uniq_ipgei_grille'),
        ]

    def __str__(self):
        return f'Grille {self.classe.nom} — semestre {self.get_type_semestre_display()}'


class SeanceType(models.Model):
    """Case de la grille type : jour × créneau → matière, prof, salle."""
    grille       = models.ForeignKey(GrilleType, on_delete=models.CASCADE, related_name='seances')
    jour         = models.ForeignKey('parametres.Jour', on_delete=models.PROTECT, related_name='+')
    creneau      = models.ForeignKey('parametres.Creneau', on_delete=models.PROTECT, related_name='+')
    # Facultative : une séance spéciale — sport, instruction militaire — bloque
    # un créneau sans relever d'une matière de la maquette.
    matiere      = models.ForeignKey(
        Matiere, on_delete=models.PROTECT, related_name='seances_type',
        null=True, blank=True,
    )
    prof         = models.ForeignKey(
        'prof.Prof', on_delete=models.PROTECT, null=True, blank=True, related_name='seances_type_ipgei',
    )
    salle        = models.ForeignKey(
        'salle.Salle', on_delete=models.SET_NULL, null=True, blank=True, related_name='seances_type_ipgei',
    )
    sous_groupe  = models.ForeignKey(
        SousGroupeTP, on_delete=models.CASCADE, null=True, blank=True, related_name='seances_type',
        help_text='Renseigné pour une séance de TP dédoublée ; vide = classe entière.',
    )
    # Référentiel commun avec le socle (« Paramètres → Séances ») : CM, TD, TP,
    # DS, EF, ER… Auparavant une liste figée dans le code, qu'il fallait
    # traduire à chaque échange avec le Suivi. Un seul vocabulaire supprime la
    # traduction, et un type ajouté dans Paramètres devient disponible ici sans
    # toucher au code.
    type_seance  = models.ForeignKey(
        'parametres.Seance', on_delete=models.PROTECT, related_name='+',
    )

    class Meta:
        db_table    = 'ipgei_seance_type'
        ordering    = ['grille', 'jour_id', 'creneau__ordre']
        constraints = [
            # Une classe entière ne peut avoir qu'une séance par case ; les TP
            # dédoublés cohabitent car ils diffèrent par le sous-groupe.
            models.UniqueConstraint(
                fields=['grille', 'jour', 'creneau', 'sous_groupe'],
                name='uniq_ipgei_seance_type_case',
            ),
        ]

    def __str__(self):
        return (f'{self.grille.classe.nom} {self.jour} {self.creneau} — '
                f'{self.matiere.code if self.matiere else self.type_seance}')

    def save(self, *args, **kwargs):
        _vider_si_seance_speciale(self)
        super().save(*args, **kwargs)


def _vider_si_seance_speciale(seance):
    """
    Une séance spéciale n'a ni enseignant, ni salle, ni matière.

    Sport, instruction militaire : le créneau est bloqué pour toute la classe,
    mais aucun enseignant du référentiel ne l'assure, aucune salle n'est
    réservée, et cela ne relève d'aucune matière de la maquette. Y laisser un
    professeur lui compterait des heures — donc une vacation — pour un cours
    qu'il ne donne pas ; y laisser une matière fausserait son avancement.
    """
    if seance.type_seance_id and getattr(seance.type_seance, 'is_special', False):
        seance.prof = None
        seance.salle = None
        seance.matiere = None


class SeanceReelle(models.Model):
    """
    Séance effectivement programmée une semaine donnée. Générée par duplication
    de la grille type, puis librement éditable sur une semaine ou un lot de
    semaines (plan §4.5).
    """
    ORIGINE_GRILLE      = 'grille'
    ORIGINE_MANUELLE    = 'manuelle'
    ORIGINE_PERMUTATION = 'permutation'
    #: Recopiée d'une AUTRE SEMAINE, et non d'un patron.
    #:
    #: Une valeur à part, et non l'origine héritée de la source : la copie doit
    #: rester écrasable par une nouvelle duplication, là où l'original saisi à
    #: la main ne doit jamais l'être. Hériter aurait rendu toute re-duplication
    #: sans effet — on corrige la semaine 1, et les suivantes ne bougent plus.
    ORIGINE_DUPLIQUEE   = 'dupliquee'
    ORIGINE_CHOICES     = [
        (ORIGINE_GRILLE,      'Dupliquée de la grille type'),
        (ORIGINE_DUPLIQUEE,   "Recopiée d'une autre semaine"),
        (ORIGINE_MANUELLE,    'Ajoutée manuellement'),
        (ORIGINE_PERMUTATION, 'Issue d\'une permutation'),
    ]

    classe       = models.ForeignKey(Classe, on_delete=models.CASCADE, related_name='seances')
    semaine      = models.ForeignKey(SemaineIPGEI, on_delete=models.CASCADE, related_name='seances')
    jour         = models.ForeignKey('parametres.Jour', on_delete=models.PROTECT, related_name='+')
    creneau      = models.ForeignKey('parametres.Creneau', on_delete=models.PROTECT, related_name='+')
    matiere      = models.ForeignKey(
        Matiere, on_delete=models.PROTECT, related_name='seances',
        null=True, blank=True,
    )
    prof         = models.ForeignKey(
        'prof.Prof', on_delete=models.PROTECT, null=True, blank=True, related_name='seances_ipgei',
    )
    salle        = models.ForeignKey(
        'salle.Salle', on_delete=models.SET_NULL, null=True, blank=True, related_name='seances_ipgei',
    )
    sous_groupe  = models.ForeignKey(
        SousGroupeTP, on_delete=models.CASCADE, null=True, blank=True, related_name='seances',
    )
    # Référentiel commun avec le socle (« Paramètres → Séances ») : CM, TD, TP,
    # DS, EF, ER… Auparavant une liste figée dans le code, qu'il fallait
    # traduire à chaque échange avec le Suivi. Un seul vocabulaire supprime la
    # traduction, et un type ajouté dans Paramètres devient disponible ici sans
    # toucher au code.
    type_seance  = models.ForeignKey(
        'parametres.Seance', on_delete=models.PROTECT, related_name='+',
    )
    date         = models.DateField(null=True, blank=True, help_text='Date réelle, déduite de la semaine et du jour.')

    #: D'OÙ VIENT CETTE SÉANCE — et le défaut est `manuelle`, non `grille`.
    #:
    #: Il valait `grille`, si bien que TOUTE séance saisie à la main en héritait.
    #: Or c'est sur cette étiquette que repose la promesse de la duplication —
    #: « les séances ajoutées à la main ne sont jamais écrasées » —, qui était
    #: donc fausse : onze séances manuelles étaient marquées « grille » et
    #: auraient été détruites au premier écrasement.
    #:
    #: `seance_type` tranche sans ambiguïté : nul, la séance n'a jamais été
    #: dupliquée d'un patron. C'est ce critère qui a servi à réétiqueter
    #: l'existant (migration `0041`).
    origine      = models.CharField(max_length=12, choices=ORIGINE_CHOICES,
                                    default=ORIGINE_MANUELLE)
    seance_type  = models.ForeignKey(
        SeanceType, on_delete=models.SET_NULL, null=True, blank=True, related_name='occurrences',
    )
    annulee      = models.BooleanField(default=False)
    #: POURQUOI la séance est annulée, quand ce n'est pas une décision humaine.
    #:
    #: `'ferie'` : annulée parce que son jour a été marqué férié. C'est ce qui
    #: permet, au retrait du férié, de rétablir CES séances-là et jamais une
    #: annulation faite à la main — un enseignant malade reste absent même si
    #: le jour redevient ouvré. Vide pour une annulation manuelle, et toute
    #: annulation ou tout rétablissement faits à la main l'effacent.
    #:
    #: On ANNULE et l'on ne supprime pas : la trace reste, et c'est `annulee`
    #: qui retire la séance du pointage et de la paie.
    MOTIF_FERIE = 'ferie'
    motif_annulation = models.CharField(max_length=20, blank=True, default='')
    # Seconde moitié du verrou de cohérence (cf. `SemaineIPGEI.suivi_genere_le`).
    # `auto_now` est posé par `pre_save`, donc honoré aussi par `bulk_create` —
    # mais PAS par un `QuerySet.update()`, qui doit être évité sur ce modèle.
    modifiee_le  = models.DateTimeField(auto_now=True, editable=False)
    # Prof d'origine avant permutation — la charge suit le prof effectif (`prof`),
    # mais on garde qui aurait dû assurer la séance pour la traçabilité.
    prof_initial = models.ForeignKey(
        'prof.Prof', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='seances_ipgei_cedees',
    )
    observations = models.CharField(max_length=300, blank=True, default='')

    class Meta:
        db_table    = 'ipgei_seance_reelle'
        ordering    = ['semaine', 'jour_id', 'creneau__ordre']
        constraints = [
            models.UniqueConstraint(
                fields=['classe', 'semaine', 'jour', 'creneau', 'sous_groupe'],
                name='uniq_ipgei_seance_reelle_case',
            ),
            # La contrainte ci-dessus ne protège QUE les séances de sous-groupe.
            # PostgreSQL tient deux NULL pour distincts : une case de classe
            # entière — la majorité — pouvait donc être insérée autant de fois
            # qu'on cliquait sur « Enregistrer ». D'où cette seconde contrainte,
            # partielle, qui couvre le cas `sous_groupe IS NULL`.
            models.UniqueConstraint(
                fields=['classe', 'semaine', 'jour', 'creneau'],
                condition=models.Q(sous_groupe__isnull=True),
                name='uniq_ipgei_seance_reelle_case_classe',
            ),
        ]
        indexes = [
            models.Index(fields=['semaine', 'classe']),
            models.Index(fields=['prof', 'semaine']),
        ]

    def __str__(self):
        return (f'{self.classe.nom} {self.semaine} {self.jour} {self.creneau} — '
                f'{self.matiere.code if self.matiere else self.type_seance}')

    def save(self, *args, **kwargs):
        _vider_si_seance_speciale(self)
        super().save(*args, **kwargs)


# ═════════════════════════════════════════════════════════════════════════════
# 8. Permutations (Phase 2)
# ═════════════════════════════════════════════════════════════════════════════
class StatutWorkflow(models.TextChoices):
    DEMANDEE = 'demandee',  'Demandée'
    ACCORDEE = 'accordee',  'Accord de la contrepartie'
    VALIDEE  = 'validee',   'Validée par le directeur'
    APPLIQUEE = 'appliquee', 'Appliquée'
    REFUSEE  = 'refusee',   'Refusée'


class PermutationProf(models.Model):
    """
    Échange d'enseignants entre deux séances de la MÊME classe, sur le même
    créneau conservé : ce qui bascule, c'est le trio (prof, salle, matière).

    Circuit : demande de A → accord de B → validation directeur ; le directeur
    peut aussi trancher directement (`action_directe`). Une fois appliquée, la
    charge et le pointage suivent le prof effectif de chaque semaine touchée.
    """
    seance_a       = models.ForeignKey(
        SeanceReelle, on_delete=models.CASCADE, related_name='permutations_en_tant_que_a',
    )
    seance_b       = models.ForeignKey(
        SeanceReelle, on_delete=models.CASCADE, related_name='permutations_en_tant_que_b',
    )
    nb_semaines    = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1)],
        help_text='Portée : 1 = la semaine des séances, N = N semaines consécutives.',
    )
    statut         = models.CharField(
        max_length=10, choices=StatutWorkflow.choices, default=StatutWorkflow.DEMANDEE,
    )
    action_directe = models.BooleanField(
        default=False, help_text='Décidée directement par le directeur, sans circuit A→B.',
    )
    motif          = models.TextField(blank=True, default='')

    demande_par    = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='permutations_prof_demandees',
    )
    accord_par     = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='permutations_prof_accordees',
    )
    valide_par     = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='permutations_prof_validees',
    )
    motif_refus    = models.TextField(blank=True, default='')
    date_demande   = models.DateTimeField(auto_now_add=True)
    date_accord    = models.DateTimeField(null=True, blank=True)
    date_validation = models.DateTimeField(null=True, blank=True)
    date_application = models.DateTimeField(null=True, blank=True)
    seances_impactees = models.PositiveSmallIntegerField(
        default=0, help_text='Nombre de séances effectivement permutées à l\'application.',
    )

    class Meta:
        db_table = 'ipgei_permutation_prof'
        ordering = ['-date_demande']
        indexes  = [models.Index(fields=['statut'])]

    def __str__(self):
        return f'Permutation prof #{self.pk} — {self.get_statut_display()}'

    def clean(self):
        if self.seance_a_id and self.seance_b_id:
            if self.seance_a_id == self.seance_b_id:
                raise ValidationError('Les deux séances doivent être distinctes.')
            if self.seance_a.classe_id != self.seance_b.classe_id:
                raise ValidationError('La permutation ne concerne que deux séances de la même classe.')
            if self.seance_a.creneau_id != self.seance_b.creneau_id:
                raise ValidationError('Le créneau est conservé : les deux séances doivent le partager.')


class PermutationEtudiant(models.Model):
    """
    Mouvement d'étudiant entre classes, sous deux formes.

    **Échange** — `inscription_b` renseignée : les deux élèves croisent, chacun
    prenant la classe de l'autre. C'est la forme courante, parce qu'une classe
    pleine ne peut accueillir qu'en libérant une place ; la traiter comme deux
    transferts séparés laissait un intervalle où l'une était en surnombre et
    l'autre incomplète, et rien ne rattachait les deux décisions.

    **Transfert** — `inscription_b` vide : un élève rejoint une classe qui a de
    la place, sans contrepartie.

    Dans les deux cas le rattachement est DÉPLACÉ, jamais dupliqué : notes,
    absences et lignes de délibération pointent sur l'inscription, dont
    l'identité ne change pas. L'historique reste donc intact et suit l'étudiant
    (plan §4.6).
    """
    inscription    = models.ForeignKey(
        InscriptionIPGEI, on_delete=models.CASCADE, related_name='permutations',
    )
    #: Contrepartie de l'échange. Vide pour un transfert simple. Sa classe est
    #: `classe_cible`, et elle rejoint `classe_source` — l'échange est
    #: symétrique, aucune colonne de destination ne lui est nécessaire.
    inscription_b  = models.ForeignKey(
        InscriptionIPGEI, on_delete=models.CASCADE, null=True, blank=True,
        related_name='permutations_en_contrepartie',
        help_text="Étudiant qui prend la classe de l'autre. Vide = transfert simple.",
    )
    classe_source  = models.ForeignKey(Classe, on_delete=models.PROTECT, related_name='permutations_sortantes')
    classe_cible   = models.ForeignKey(Classe, on_delete=models.PROTECT, related_name='permutations_entrantes')
    sous_groupe_cible = models.ForeignKey(
        SousGroupeTP, on_delete=models.SET_NULL, null=True, blank=True, related_name='permutations_entrantes',
    )
    #: Sous-groupe d'arrivée de la contrepartie, dans `classe_source`.
    sous_groupe_cible_b = models.ForeignKey(
        SousGroupeTP, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='permutations_entrantes_b',
    )
    statut         = models.CharField(
        max_length=10, choices=StatutWorkflow.choices, default=StatutWorkflow.DEMANDEE,
    )
    action_directe = models.BooleanField(default=False)
    motif          = models.TextField(blank=True, default='')
    motif_refus    = models.TextField(blank=True, default='')

    demande_par    = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='permutations_etudiant_demandees',
    )
    accord_par     = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='permutations_etudiant_accordees',
    )
    valide_par     = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='permutations_etudiant_validees',
    )
    date_demande   = models.DateTimeField(auto_now_add=True)
    date_accord    = models.DateTimeField(null=True, blank=True)
    date_validation = models.DateTimeField(null=True, blank=True)
    date_application = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'ipgei_permutation_etudiant'
        ordering = ['-date_demande']
        indexes  = [models.Index(fields=['statut'])]

    @property
    def est_echange(self) -> bool:
        return self.inscription_b_id is not None

    def __str__(self):
        if self.est_echange:
            return (f'{self.inscription.etudiant} ⇄ {self.inscription_b.etudiant} '
                    f'({self.classe_source} / {self.classe_cible})')
        return f'{self.inscription.etudiant} : {self.classe_source} → {self.classe_cible}'

    def clean(self):
        if self.classe_source_id and self.classe_cible_id:
            if self.classe_source_id == self.classe_cible_id:
                raise ValidationError('La classe cible doit différer de la classe actuelle.')
            if self.classe_source.niveau != self.classe_cible.niveau:
                raise ValidationError('Un changement de classe reste au même niveau (MPSI ou MP).')

        if self.inscription_b_id:
            if self.inscription_b_id == self.inscription_id:
                raise ValidationError({'inscription_b': 'Un étudiant ne permute pas avec lui-même.'})
            # La contrepartie doit venir de la classe visée : sans cela
            # l'« échange » déplacerait deux élèves sans qu'aucune place ne se
            # libère là où il faut.
            if self.inscription_b.classe_id != self.classe_cible_id:
                raise ValidationError({'inscription_b': (
                    'La contrepartie doit être inscrite dans la classe visée '
                    f'({self.classe_cible}).')})
            if not self.inscription_b.actif:
                raise ValidationError({'inscription_b': "L'inscription de la contrepartie n'est pas active."})


# ═════════════════════════════════════════════════════════════════════════════
# 9. Absences par exception
# ═════════════════════════════════════════════════════════════════════════════

class _Fige:
    """
    Valeur figée, présentée comme l'objet dont elle vient.

    L'archive ne pointe plus vers le référentiel : elle porte ses libellés en
    dur. Mais le constructeur de grille du PDF, lui, lit `seance.prof.nom` ou
    `seance.type_seance.is_special`. Cette coquille lui rend cette forme, sans
    quoi il aurait fallu deux constructeurs — et deux grilles imprimées qui
    finissent par diverger, alors que tout l'intérêt de l'archive est de se
    superposer à l'emploi du temps vivant.
    """

    def __init__(self, **valeurs):
        self.__dict__.update(valeurs)


class EmploiArchive(models.Model):
    """
    Photographie de l'emploi du temps d'une semaine, prise à la génération du
    suivi.

    Les séances vivent dans `SeanceReelle`, qui est modifiée sur place : la
    version d'avant est perdue. Or c'est sur une version précise que les heures
    ont été pointées, et parfois payées. Sans cette table, vérifier après coup
    ce qui avait été planifié demandait de relire le journal d'audit ligne à
    ligne.

    Rien n'y pointe vers le référentiel. Un archive dont le contenu change
    quand on renomme une salle ou qu'on supprime un enseignant n'archive rien :
    les libellés sont donc figés au moment de la prise. Les identifiants sont
    conservés en simples entiers, pour regrouper et ordonner la grille sans
    créer de dépendance.

    Une régénération ne remplace pas la précédente : elle ajoute une version.
    """

    semaine   = models.ForeignKey(SemaineIPGEI, on_delete=models.CASCADE, related_name='archives')
    classe    = models.ForeignKey(Classe, on_delete=models.CASCADE, related_name='archives_edt')
    version   = models.PositiveSmallIntegerField(default=1)
    genere_le = models.DateTimeField()

    # Repères de grille — entiers nus, sans contrainte : ils servent à ordonner
    # et à regrouper, pas à retrouver une ligne du référentiel.
    jour_ref        = models.IntegerField(null=True, blank=True)
    creneau_ref     = models.IntegerField(null=True, blank=True)
    creneau_ordre   = models.IntegerField(default=0)
    matiere_ref     = models.IntegerField(null=True, blank=True)
    type_seance_ref = models.IntegerField(null=True, blank=True)
    sous_groupe_ref = models.IntegerField(null=True, blank=True)
    # L'emploi du temps publié se consulte aussi par enseignant et par salle.
    # Le nom figé ne suffit pas à filtrer : deux homonymes, une orthographe
    # corrigée, et la grille d'un enseignant se mélange à celle d'un autre.
    prof_ref        = models.IntegerField(null=True, blank=True)
    salle_ref       = models.IntegerField(null=True, blank=True)

    # Libellés figés
    jour_libelle        = models.CharField(max_length=30,  blank=True, default='')
    creneau_libelle     = models.CharField(max_length=30,  blank=True, default='')
    classe_nom          = models.CharField(max_length=100, blank=True, default='')
    sous_groupe_libelle = models.CharField(max_length=30,  blank=True, default='')
    matiere_code        = models.CharField(max_length=30,  blank=True, default='')
    matiere_intitule    = models.CharField(max_length=150, blank=True, default='')
    type_seance_libelle = models.CharField(max_length=50,  blank=True, default='')
    type_seance_special = models.BooleanField(default=False)
    prof_nom            = models.CharField(max_length=150, blank=True, default='')
    prof_initial_nom    = models.CharField(max_length=150, blank=True, default='')
    salle_nom           = models.CharField(max_length=100, blank=True, default='')

    date_seance = models.DateField(null=True, blank=True)
    origine     = models.CharField(max_length=12, blank=True, default='')
    annulee     = models.BooleanField(default=False)

    class Meta:
        verbose_name        = 'Emploi du temps archivé'
        verbose_name_plural = 'Emplois du temps archivés'
        ordering            = ['-genere_le', 'jour_ref', 'creneau_ordre']
        indexes = [
            models.Index(fields=['semaine', 'classe', 'version']),
            models.Index(fields=['semaine', 'prof_ref']),
            models.Index(fields=['semaine', 'salle_ref']),
        ]

    def __str__(self):
        return (f'{self.classe_nom} {self.semaine} v{self.version} — '
                f'{self.jour_libelle} {self.creneau_libelle}')

    # ── Surface attendue par le constructeur de grille du PDF ────────────────
    @property
    def jour_id(self):        return self.jour_ref
    @property
    def creneau_id(self):     return self.creneau_ref
    @property
    def matiere_id(self):     return self.matiere_ref
    @property
    def sous_groupe_id(self): return self.sous_groupe_ref

    @property
    def jour(self):
        return _Fige(jour=self.jour_libelle)

    @property
    def creneau(self):
        return _Fige(creneau=self.creneau_libelle, ordre=self.creneau_ordre)

    @property
    def type_seance(self):
        return _Fige(type_seance=self.type_seance_libelle,
                     is_special=self.type_seance_special)

    @property
    def matiere(self):
        if not (self.matiere_code or self.matiere_intitule):
            return None
        return _Fige(code=self.matiere_code, intitule=self.matiere_intitule)

    @property
    def prof(self):
        return _Fige(nom=self.prof_nom) if self.prof_nom else None

    @property
    def salle(self):
        return _Fige(nom=self.salle_nom) if self.salle_nom else None

    @property
    def sous_groupe(self):
        return _Fige(groupe=self.sous_groupe_libelle) if self.sous_groupe_libelle else None


class EpreuveArchive(_IntervalleEpreuve, models.Model):
    """
    Photographie d'une épreuve, prise avec celle de l'emploi du temps.

    Une table à part, et non des lignes d'`EmploiArchive` : une épreuve n'a ni
    créneau, ni enseignant, ni salle, et convoque plusieurs classes. L'y loger
    aurait demandé d'inventer des valeurs pour ces champs, et toute requête
    lisant des séances aurait dû apprendre à les écarter.

    La ligne est posée PAR CLASSE convoquée, comme `EmploiArchive` : c'est
    classe par classe qu'on génère un suivi et qu'on relit un emploi du temps.
    Une épreuve de niveau y laisse donc autant de lignes que de classes — le
    prix à payer pour que chacune retrouve exactement ce qu'elle a reçu.

    Autoportante, elle aussi : aucun lien vers le référentiel, uniquement des
    valeurs figées. Déplacer une épreuve après coup ne doit pas récrire le
    document déjà distribué — c'est la raison d'être de cette table.

    Le calcul des créneaux recouverts vient du mixin partagé avec l'épreuve
    vivante : le recopier aurait laissé les deux dériver, et un document
    réimprimé n'aurait plus fusionné les mêmes colonnes.
    """
    semaine   = models.ForeignKey(SemaineIPGEI, on_delete=models.CASCADE,
                                  related_name='archives_epreuves')
    classe    = models.ForeignKey(Classe, on_delete=models.CASCADE,
                                  related_name='archives_epreuves')
    version   = models.PositiveSmallIntegerField(default=1)
    genere_le = models.DateTimeField()

    # Repère nu vers l'épreuve d'origine : utile pour rapprocher deux versions,
    # jamais pour lire la valeur. L'épreuve peut avoir été supprimée depuis.
    epreuve_ref = models.IntegerField(null=True, blank=True)
    matiere_ref = models.IntegerField(null=True, blank=True)

    type_epreuve  = models.CharField(max_length=2, blank=True, default='')
    numero        = models.PositiveSmallIntegerField(default=1)
    date          = models.DateField()
    heure_debut   = models.TimeField()
    duree_minutes = models.PositiveSmallIntegerField(default=120)

    matiere_code     = models.CharField(max_length=30,  blank=True, default='')
    matiere_intitule = models.CharField(max_length=150, blank=True, default='')
    classe_nom       = models.CharField(max_length=100, blank=True, default='')
    observations     = models.CharField(max_length=300, blank=True, default='')

    class Meta:
        db_table            = 'ipgei_epreuve_archive'
        verbose_name        = 'Épreuve archivée'
        verbose_name_plural = 'Épreuves archivées'
        ordering            = ['-genere_le', 'date', 'heure_debut']
        indexes = [models.Index(fields=['semaine', 'classe', 'version'])]

    def __str__(self):
        return (f'{self.classe_nom} {self.semaine} v{self.version} — '
                f'{self.type_epreuve} {self.matiere_code} {self.date}')

    # ── Surface attendue par le constructeur de grille du PDF ────────────────
    @property
    def matiere_id(self):
        return self.matiere_ref

    @property
    def intitule_affiche(self) -> str:
        return self.matiere_intitule or self.type_epreuve

class AbsenceSeance(models.Model):
    """
    Saisie **par exception** : présent par défaut, seules les lignes créées ici
    signalent une absence (plan §4.7). L'absence est rattachée à la séance
    réelle, donc datée par la semaine et le créneau.
    """
    STATUT_ABSENT    = 'absent'
    STATUT_RETARD    = 'retard'
    STATUT_JUSTIFIEE = 'justifiee'
    STATUT_CHOICES   = [
        (STATUT_ABSENT,    'Absent'),
        (STATUT_RETARD,    'Retard'),
        (STATUT_JUSTIFIEE, 'Absence justifiée'),
    ]

    seance       = models.ForeignKey(SeanceReelle, on_delete=models.CASCADE, related_name='absences')
    inscription  = models.ForeignKey(
        InscriptionIPGEI, on_delete=models.CASCADE, related_name='absences',
    )
    statut       = models.CharField(max_length=10, choices=STATUT_CHOICES, default=STATUT_ABSENT)
    justificatif = models.CharField(max_length=300, blank=True, default='')
    saisie_par   = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='absences_ipgei_saisies',
    )
    date_saisie  = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table    = 'ipgei_absence'
        ordering    = ['-seance__date', 'inscription']
        constraints = [
            models.UniqueConstraint(fields=['seance', 'inscription'], name='uniq_ipgei_absence'),
        ]
        indexes = [models.Index(fields=['inscription', 'statut'])]

    def __str__(self):
        return f'{self.inscription.etudiant} — {self.get_statut_display()} ({self.seance})'


class PerimetreResponsable(models.Model):
    """
    Ce dont un chef de departement ou un coordinateur repond.

    Un chef porte un `departement` : toutes ses matieres sont a lui. Un
    coordinateur porte des `matieres` nommees. Les deux peuvent se cumuler. Le
    role RBAC est le meme pour tous (`chef_departement`) ; c'est ce perimetre,
    et lui seul, qui dit lesquelles des matieres il voit et modifie.

    Absent, le responsable ne voit rien : mieux vaut un ecran vide qu'un acces
    a tout en attendant qu'on le configure.
    """
    utilisateur = models.OneToOneField(
        'authentication.CustomUser', on_delete=models.CASCADE,
        related_name='perimetre_ipgei',
    )
    departement = models.CharField(
        max_length=10, choices=Matiere.DEPARTEMENT_CHOICES, blank=True, default='',
    )
    matieres    = models.ManyToManyField(
        Matiere, blank=True, related_name='responsables',
    )

    class Meta:
        db_table = 'ipgei_perimetre_responsable'
        verbose_name = 'perimetre de responsable'
        verbose_name_plural = 'perimetres de responsables'

    def __str__(self):
        return f'{self.utilisateur} — {self.departement or "matieres nommees"}'
