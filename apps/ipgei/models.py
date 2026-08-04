"""
Moteur académique IPGEI — Institut Préparatoire aux Grandes Écoles d'Ingénieurs.

Cursus à 2 niveaux : MPSI (1re année) → MP (2e année), 4 semestres S1..S4.
Ce module remplace la couche académique LMD du socle SIGA (filieres / modules /
EM / evaluations). Il ne modifie AUCUN modèle SIGA : il réutilise seulement
`absence.Etudiant`, `prof.Prof`, `salle.Salle`, `parametres.Jour/Creneau/
Institution` et `authentication.CustomUser` par clé étrangère.

Toutes les tables sont préfixées `ipgei_` pour rendre l'origine évidente en base.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

# `Classe` et `SousGroupeTP` héritent de ce modèle du socle : une classe IPGEI
# EST un département de planification, ce qui rend Suivi, Vacations, Avancement
# et Emplois utilisables sans copie ni synchronisation.
from apps.departement.models import Departement


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
    def niveau(self) -> str:
        """
        Premier niveau de l'année d'étude — conservé pour l'affichage hérité.

        Ne jamais s'en servir pour filtrer : voir `niveaux` et `rang`.
        """
        codes = self.niveaux
        if codes:
            return codes[0]
        return NIVEAU_MPSI if self.code in SEMESTRES_PAR_NIVEAU[NIVEAU_MPSI] else NIVEAU_MP

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
        return {
            (sous_groupe or classe, jour, creneau, em, prof, type_seance)
            for sous_groupe, classe, jour, creneau, em, prof, type_seance
            in self.seances.filter(annulee=False, type_seance__is_special=False,
                                   **({'classe': classe} if classe else {})).values_list(
                'sous_groupe_id', 'classe_id', 'jour_id', 'creneau_id',
                'matiere__em_miroir_id', 'prof_id', 'type_seance__type_seance',
            )
        }

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
            niveau__niveau__in=self.semestre.niveaux or [self.semestre.niveau],
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

        # Côté suivi : une passe sur les lignes des années et périodes visées.
        annees   = {s.semestre.annee_universitaire for s in semaines}
        numeros  = {s.numero for s in semaines if s.numero is not None}
        # Même portée que le calcul unitaire : on ne compare une semaine qu'aux
        # unités de SON niveau, S1 et S3 partageant leurs numéros de semaine.
        unites = set()
        for semaine in semaines:
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

    def save(self, *args, **kwargs):
        # Import local : `socle` importe ce module, l'importer en tête créerait
        # un cycle.
        from .socle import filiere_socle

        self.nom          = f'{self.niveau_code} {self.groupe}'.strip()
        self.code         = f'{self.niveau_code}-{self.groupe}'.strip('-')
        self.is_container = False       # une classe se planifie, ce n'est pas un conteneur
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


class InscriptionIPGEI(models.Model):
    """
    Rattachement d'un étudiant à une classe pour une année universitaire.

    Un changement de classe (permutation étudiant) **modifie cette ligne** et
    journalise l'ancien rattachement dans `HistoriqueClasse` : notes et absences
    pointent sur l'inscription, donc rien n'est copié ni perdu (plan §4.6).
    """
    STATUT_ACTIF      = 'actif'
    STATUT_ADMIS      = 'admis'
    STATUT_REORIENTE  = 'reoriente'
    STATUT_REDOUBLANT = 'redoublant'
    STATUT_CNIM       = 'autorise_cnim'
    STATUT_ABANDON    = 'abandon'
    STATUT_CHOICES    = [
        (STATUT_ACTIF,      'En cours'),
        (STATUT_ADMIS,      'Admis en 2e année'),
        (STATUT_REORIENTE,  'Réorienté'),
        (STATUT_REDOUBLANT, 'Redoublant'),
        (STATUT_CNIM,       'Autorisé à concourir (CNIM)'),
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
    statut              = models.CharField(max_length=15, choices=STATUT_CHOICES, default=STATUT_ACTIF)
    nb_redoublements    = models.PositiveSmallIntegerField(
        default=0,
        help_text='Compteur du droit de redoublement (MP). Au-delà du max, le redoublement est refusé.',
    )
    date_inscription    = models.DateField(auto_now_add=True)
    actif               = models.BooleanField(default=True)

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
    pct_ds          = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('30'))
    pct_tp          = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    pct_exam        = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('70'))
    ordre           = models.PositiveSmallIntegerField(default=0, help_text="Ordre d'affichage sur les relevés.")
    actif           = models.BooleanField(default=True)

    # Contrepartie SIGA de la matiere : les vacations et le suivi raisonnent en
    # `EM`. Dernier miroir subsistant — les classes, elles, heritent desormais
    # de `Departement` et n'ont plus de copie a maintenir.
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


# ═════════════════════════════════════════════════════════════════════════════
# 6. Délibération IPGEI
# ═════════════════════════════════════════════════════════════════════════════
class Deliberation(models.Model):
    """
    Jury de fin de semestre ou de fin d'année, pour un niveau et une année.

    Le seuil et le plafond de rattrapage sont **copiés depuis les paramètres à
    la création** puis figés dans la délibération : rejouer un calcul donne
    toujours le même résultat, même si le paramétrage a bougé entre-temps.
    """
    PORTEE_SEMESTRE = 'semestre'
    PORTEE_ANNUELLE = 'annuelle'
    PORTEE_CHOICES  = [(PORTEE_SEMESTRE, 'Semestre'), (PORTEE_ANNUELLE, 'Annuelle')]

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
    annee_universitaire = models.CharField(max_length=9)
    portee              = models.CharField(max_length=10, choices=PORTEE_CHOICES, default=PORTEE_ANNUELLE)
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
        return f'Délibération {self.niveau} {cible}'

    def clean(self):
        if self.portee == self.PORTEE_SEMESTRE and not self.semestre_id:
            raise ValidationError({'semestre': 'Une délibération de semestre doit cibler un semestre.'})

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
    # 1re année (MPSI) : admis ou réorienté — pas de redoublement.
    DECISION_ADMIS      = 'admis'
    DECISION_REORIENTE  = 'reoriente'
    # 2e année (MP) : autorisé CNIM ou redoublant (droit unique).
    DECISION_CNIM       = 'autorise_cnim'
    DECISION_REDOUBLANT = 'redoublant'
    DECISION_EXCLU      = 'exclu'
    DECISION_CHOICES    = [
        (DECISION_ADMIS,      'Admis en 2e année'),
        (DECISION_REORIENTE,  'Réorienté'),
        (DECISION_CNIM,       'Autorisé à concourir (CNIM)'),
        (DECISION_REDOUBLANT, 'Redoublant'),
        (DECISION_EXCLU,      'Exclu — droit de redoublement épuisé'),
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
    decision_auto     = models.CharField(max_length=15, choices=DECISION_CHOICES, blank=True, default='')
    decision          = models.CharField(max_length=15, choices=DECISION_CHOICES, blank=True, default='')
    motif_ajustement  = models.TextField(blank=True, default='')
    observations      = models.TextField(blank=True, default='')

    # ── État de l'inscription avant validation ───────────────────────────────
    # Écrits au moment de la validation, quand la décision est reportée sur
    # l'inscription. Sans eux, dévalider un jury ne saurait pas quoi restaurer :
    # remettre « actif » d'office effacerait un redoublement antérieur, et
    # décrémenter à l'aveugle fausserait le droit au redoublement.
    statut_avant           = models.CharField(max_length=15, blank=True, default='')
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
    ORIGINE_CHOICES     = [
        (ORIGINE_GRILLE,      'Dupliquée de la grille type'),
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

    origine      = models.CharField(max_length=12, choices=ORIGINE_CHOICES, default=ORIGINE_GRILLE)
    seance_type  = models.ForeignKey(
        SeanceType, on_delete=models.SET_NULL, null=True, blank=True, related_name='occurrences',
    )
    annulee      = models.BooleanField(default=False)
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
    Changement de classe d'un étudiant. Le rattachement est déplacé, jamais
    dupliqué : notes et absences pointent sur l'inscription et suivent
    automatiquement (plan §4.6).
    """
    inscription    = models.ForeignKey(
        InscriptionIPGEI, on_delete=models.CASCADE, related_name='permutations',
    )
    classe_source  = models.ForeignKey(Classe, on_delete=models.PROTECT, related_name='permutations_sortantes')
    classe_cible   = models.ForeignKey(Classe, on_delete=models.PROTECT, related_name='permutations_entrantes')
    sous_groupe_cible = models.ForeignKey(
        SousGroupeTP, on_delete=models.SET_NULL, null=True, blank=True, related_name='permutations_entrantes',
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

    def __str__(self):
        return f'{self.inscription.etudiant} : {self.classe_source} → {self.classe_cible}'

    def clean(self):
        if self.classe_source_id and self.classe_cible_id:
            if self.classe_source_id == self.classe_cible_id:
                raise ValidationError('La classe cible doit différer de la classe actuelle.')
            if self.classe_source.niveau != self.classe_cible.niveau:
                raise ValidationError('Un changement de classe reste au même niveau (MPSI ou MP).')


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
