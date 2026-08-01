from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator


TYPE_SESSION_CHOICES = [
    ('normale',     'Session normale'),
    ('rattrapage',  'Session de rattrapage'),
]

TYPE_SEMESTRE_CHOICES = [
    ('Impairs', 'Semestres impairs (S1, S3, S5)'),
    ('Pairs',   'Semestres pairs   (S2, S4, S6)'),
]

TYPE_NOTE_CHOICES = [
    ('CC',   'Contrôle continu'),
    ('TP',   'Travaux pratiques'),
    ('EXAM', 'Examen final'),
]

DECISION_CHOICES = [
    ('admis',     'Admis'),
    ('ajourned',  'Ajourné'),
    ('rachat',    'Admis par rachat'),
    ('exclus',    'Exclus'),
]

# Légende validée — docs/deliberation.md §2
CODE_STATUT_CHOICES = [
    ('V',   'Validé directement (Art. 12)'),
    ('VCI', 'Validé par Compensation Interne — module (Art. 13)'),
    ('VCS', 'Validé par Compensation Semestrielle (Art. 14)'),
    ('R',   'Validé par Rachat jury'),
    ('NV',  'Non Validé — rattrapage facultatif (Art. 17 al. 3)'),
    ('NVO', 'Non Validé — rattrapage obligatoire (Art. 17 al. 2)'),
    ('E',   'Éliminatoire — rattrapage obligatoire (Art. 17 al. 1)'),
]

TYPE_PV_CHOICES = [
    ('semestriel', 'Délibération semestrielle'),
    ('annuel',     'Délibération annuelle (progression)'),
]

ROLE_JURY_CHOICES = [
    ('president',           'Président (responsable de filière)'),
    ('chef_etablissement',  'Chef d\'établissement'),
    ('enseignant',          'Enseignant permanent'),
    ('professionnel',       'Enseignant issu du milieu professionnel'),
    ('secretaire',          'Secrétaire (scolarité)'),
]

DECISION_ANNUELLE_CHOICES = [
    ('passage_droit',    'Admis — passage de droit (60 crédits)'),
    ('passage_cond',     'Admis conditionnel (≥ 65 % crédits)'),
    ('redoublement',     'Redoublement autorisé (Art. 21-22)'),
    ('exclusion',        'Exclu (Art. 21)'),
    ('annee_blanche',    'Année blanche — absence médicale (Art. 23)'),
]

OBLIGATION_CHOICES = [
    ('obligatoire', 'Rattrapage obligatoire'),
    ('facultatif',  'Rattrapage facultatif'),
]


class SessionEvaluation(models.Model):
    """
    Session d'examen : fenêtre temporelle de saisie des notes.
    Une session couvre TOUS les semestres d'une même parité (Impairs ou Pairs)
    pour une année universitaire donnée.
    4 sessions max par année : SN-I, SR-I, SN-P, SR-P.
    """
    code                = models.CharField(max_length=30, blank=True, default='')
    intitule            = models.CharField(max_length=200, blank=True, default='')
    annee_univ          = models.ForeignKey(
        'parametres.Year', on_delete=models.PROTECT,
        related_name='sessions_evaluation',
        null=True, blank=True,
    )
    # Section 1bis institution_V1 — CRITIQUE : sans cette FK, les sessions de 2 institutions
    # sur la même année se confondent (unique_together les fusionnerait).
    institution         = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT,
        related_name='sessions_evaluation',
    )
    type_session        = models.CharField(max_length=20, choices=TYPE_SESSION_CHOICES, default='normale')
    type_semestre       = models.CharField(
        max_length=10, choices=TYPE_SEMESTRE_CHOICES, default='Impairs',
        help_text="Parité des semestres couverts : Impairs (S1,S3,S5) ou Pairs (S2,S4,S6).",
    )
    date_debut_saisie   = models.DateField(null=True, blank=True)
    date_cloture_saisie = models.DateField(null=True, blank=True)
    est_ouverte         = models.BooleanField(default=False)
    est_close           = models.BooleanField(default=False)
    # Instantané du plafond rattrapage, FIGÉ à la création depuis le défaut global
    # (ParametresPonderation). Le calcul lit CES valeurs, jamais le global -> un
    # changement ultérieur du défaut n'altère pas les sessions déjà créées.
    # NULL (sessions héritées d'avant la fonctionnalité) = AUCUN plafond.
    rattrapage_plafond_actif = models.BooleanField(null=True, blank=True)
    rattrapage_plafond       = models.DecimalField(
        max_digits=4, decimal_places=2, null=True, blank=True,
    )
    # Exception TEMPORAIRE : autorise le rattrapage FACULTATIF des éléments validés
    # par compensation semestrielle (VCS), pour permettre d'améliorer la note (toujours
    # plafonnée). Figé PAR SESSION (comme le plafond) : pour retirer l'exception (ex.
    # 2025-2026), il suffit de laisser False par défaut — aucun code à toucher, aucune
    # année antérieure recalculée. Défaut False = pas d'exception.
    rattrapage_vcs_actif     = models.BooleanField(default=False)
    # Même exception, pour les éléments validés par compensation INTRA-module (VCI) :
    # rattrapage facultatif pour améliorer la note (plafonnée). Figé par session, défaut False.
    rattrapage_vci_actif     = models.BooleanField(default=False)
    cloturee_par        = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='sessions_cloturees',
    )

    class Meta:
        db_table        = 'evaluations_session'
        # Élargi avec institution pour éviter chevauchement multi-institution
        unique_together = ('institution', 'annee_univ', 'type_session', 'type_semestre')
        ordering        = ['-annee_univ', 'type_semestre', 'type_session']

    def __str__(self):
        return (
            f'{self.annee_univ} — {self.get_type_session_display()} '
            f'({self.get_type_semestre_display()})'
        )


class Note(models.Model):
    """
    Note d'un étudiant pour un élément de module et une session.
    Immutable après clôture de la session (vérification dans le serializer).
    """
    inscription_element = models.ForeignKey(
        'inscriptions.InscriptionElement', on_delete=models.PROTECT,
        related_name='notes',
    )
    session             = models.ForeignKey(
        SessionEvaluation, on_delete=models.PROTECT,
        related_name='notes',
    )
    type_note           = models.CharField(max_length=10, choices=TYPE_NOTE_CHOICES)
    valeur              = models.DecimalField(
        max_digits=5, decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(20)],
        help_text="Note sur 20.",
    )
    saisie_par          = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='notes_saisies',
    )
    date_saisie         = models.DateTimeField(auto_now_add=True)
    date_modification   = models.DateTimeField(auto_now=True)

    class Meta:
        db_table        = 'evaluations_note'
        unique_together = ('inscription_element', 'session', 'type_note')
        indexes         = [
            models.Index(fields=['session', 'type_note']),
        ]

    def __str__(self):
        return f'{self.inscription_element} — {self.type_note} : {self.valeur}/20'


class ResultatElement(models.Model):
    """
    Note finale calculée pour un élément de module (CC*poids + EXAM*poids).
    Calculé par le service NoteCalculService, jamais saisi manuellement.
    Une ligne par (étudiant × élément × session) — Art. 18 : 2 lignes pour normale + rattrapage.
    """
    inscription_element = models.ForeignKey(
        'inscriptions.InscriptionElement', on_delete=models.CASCADE,
        related_name='resultats',
    )
    session             = models.ForeignKey(
        SessionEvaluation, on_delete=models.PROTECT,
        related_name='resultats_elements',
    )
    note_finale         = models.DecimalField(max_digits=5, decimal_places=2)
    est_valide          = models.BooleanField(default=False)
    est_eliminatoire    = models.BooleanField(
        default=False,
        help_text="True si note < seuil_eliminatoire de l'élément.",
    )
    code_statut         = models.CharField(
        max_length=3, choices=CODE_STATUT_CHOICES, blank=True, default='',
        help_text="Statut d'évaluation (V/VCI/VCS/R/NV/NVO/E) — docs/deliberation.md §2.",
    )
    date_calcul         = models.DateTimeField(auto_now=True)

    class Meta:
        db_table        = 'evaluations_resultat_element'
        unique_together = ('inscription_element', 'session')

    def __str__(self):
        return f'{self.inscription_element} / {self.session} → {self.note_finale}/20'


class ResultatSemestre(models.Model):
    """
    Résultat agrégé pour un semestre (moyenne pondérée des éléments).
    Calculé par le service NoteCalculService après calcul de tous les éléments.
    """
    inscription_ped = models.ForeignKey(
        'inscriptions.InscriptionPedagogique', on_delete=models.CASCADE,
        related_name='resultats_semestre',
    )
    session         = models.ForeignKey(
        SessionEvaluation, on_delete=models.PROTECT,
        related_name='resultats_semestre',
    )
    moyenne         = models.DecimalField(max_digits=5, decimal_places=2)
    credits_valides = models.IntegerField(default=0)
    est_admis       = models.BooleanField(default=False)
    code_statut     = models.CharField(
        max_length=3, choices=CODE_STATUT_CHOICES, blank=True, default='',
        help_text="Statut du semestre (V/VCS/R/NV).",
    )
    date_calcul     = models.DateTimeField(auto_now=True)

    class Meta:
        db_table        = 'evaluations_resultat_semestre'
        unique_together = ('inscription_ped', 'session')

    def __str__(self):
        return f'{self.inscription_ped} — Moy {self.moyenne} (Crédits : {self.credits_valides})'


class PVDeliberation(models.Model):
    """
    Procès-verbal de délibération du jury.
    type_pv='semestriel' → validation semestre (Art. 15-17).
    type_pv='annuel'     → progression annuelle (Art. 20-21).
    """
    type_pv         = models.CharField(
        max_length=20, choices=TYPE_PV_CHOICES, default='semestriel',
        help_text="Nature du PV : semestriel (Art. 15) ou annuel (Art. 20).",
    )
    session         = models.ForeignKey(
        SessionEvaluation, on_delete=models.PROTECT,
        related_name='pvs',
        null=True, blank=True,
        help_text="Session d'évaluation (obligatoire pour PV semestriel).",
    )
    annee_univ      = models.ForeignKey(
        'parametres.Year', on_delete=models.PROTECT,
        null=True, blank=True, related_name='pvs_annuels',
        help_text="Année universitaire (obligatoire pour PV annuel).",
    )
    filiere         = models.ForeignKey(
        'scolarite.Filiere', on_delete=models.PROTECT,
        related_name='pvs',
    )
    # Section 1bis institution_V1 — redondant avec filiere.institution mais direct
    institution     = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT,
        related_name='pvs',
    )
    niveau          = models.IntegerField(help_text="Année d'étude : 1, 2, 3")
    semestre_code   = models.CharField(
        max_length=10, blank=True, default='',
        help_text="Code semestre pour PV semestriel (ex : S1, S2).",
    )
    date_deliberation = models.DateField(null=True, blank=True)
    president_jury  = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='pvs_presides',
    )
    est_clos        = models.BooleanField(default=False)
    observations    = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'evaluations_pv_deliberation'

    def clean(self):
        # Cohérence type_pv ↔ portée : un PV semestriel exige une session,
        # un PV annuel exige une année universitaire (Art. 15 vs Art. 20).
        from django.core.exceptions import ValidationError
        if self.type_pv == 'semestriel' and self.session_id is None:
            raise ValidationError({'session': 'Session obligatoire pour un PV semestriel.'})
        if self.type_pv == 'annuel' and self.annee_univ_id is None:
            raise ValidationError({'annee_univ': 'Année universitaire obligatoire pour un PV annuel.'})

    def __str__(self):
        label = f'S{self.semestre_code}' if self.semestre_code else f'N{self.niveau}'
        return f'PV {self.get_type_pv_display()} — {self.filiere} {label}'


class LigneDeliberation(models.Model):
    """
    Décision finale par étudiant dans le PV de délibération.
    """
    pv                  = models.ForeignKey(
        PVDeliberation, on_delete=models.CASCADE,
        related_name='lignes',
    )
    inscription_admin   = models.ForeignKey(
        'inscriptions.InscriptionAdministrative', on_delete=models.PROTECT,
        related_name='lignes_deliberation',
    )
    decision            = models.CharField(max_length=20, choices=DECISION_CHOICES)
    decision_annuelle   = models.CharField(
        max_length=20, choices=DECISION_ANNUELLE_CHOICES, blank=True, default='',
        help_text="Décision de progression annuelle (PV annuel uniquement).",
    )
    moyenne_annuelle    = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    credits_annuels     = models.IntegerField(default=0)
    taux_capitalisation = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="% crédits capitalisés sur crédits de l'année (Art. 20).",
    )
    verrou_passage      = models.BooleanField(
        default=False,
        help_text="True si passage bloqué : Licence (L3 nécessite L1 complète Art. 20) "
                  "ou Ingénieur (S5 nécessite S1+S2 complets Art. 25).",
    )
    code_statut         = models.CharField(
        max_length=3, choices=CODE_STATUT_CHOICES, blank=True, default='',
    )
    rang                = models.IntegerField(null=True, blank=True)
    observations        = models.TextField(blank=True, default='')

    class Meta:
        db_table        = 'evaluations_ligne_deliberation'
        unique_together = ('pv', 'inscription_admin')

    def __str__(self):
        return f'{self.pv} — {self.inscription_admin} : {self.get_decision_display()}'


class ParametreJury(models.Model):
    """
    Ajustements exceptionnels de seuils pour un jury (par PV).
    Permet au jury de déroger aux seuils par défaut.
    """
    pv                        = models.OneToOneField(
        PVDeliberation, on_delete=models.CASCADE,
        related_name='parametre_jury',
    )
    seuil_validation_module   = models.DecimalField(max_digits=4, decimal_places=2, default=10)
    seuil_validation_semestre = models.DecimalField(max_digits=4, decimal_places=2, default=10)
    seuil_compensation        = models.DecimalField(max_digits=4, decimal_places=2, default=8)
    seuil_eliminatoire        = models.DecimalField(max_digits=4, decimal_places=2, default=6)
    seuil_progression         = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        help_text="Override seuil de progression annuelle (% crédits). "
                  "NULL = défaut réglementaire (65 LP / 75 ING).",
    )
    justification             = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'evaluations_parametre_jury'

    def __str__(self):
        return f'Paramètres jury — {self.pv}'


class RachatNote(models.Model):
    """
    Rachat d'une note par le jury — immuable après création (append-only).
    Trace obligatoirement l'ancienne et la nouvelle valeur + le motif.
    Conforme à l'Art. 562 : tout rachat doit être auditée.
    """
    pv              = models.ForeignKey(
        PVDeliberation, on_delete=models.PROTECT,
        related_name='rachats',
    )
    ligne           = models.ForeignKey(
        LigneDeliberation, on_delete=models.PROTECT,
        related_name='rachats',
    )
    ancienne_valeur = models.DecimalField(max_digits=5, decimal_places=2)
    nouvelle_valeur = models.DecimalField(max_digits=5, decimal_places=2)
    motif           = models.TextField()
    decidee_par     = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.PROTECT,
        related_name='rachats_decides',
    )
    date_decision   = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'evaluations_rachat_note'
        indexes  = [
            models.Index(fields=['pv']),
            models.Index(fields=['ligne']),
        ]

    def __str__(self):
        return (
            f'Rachat {self.ligne.inscription_admin} : '
            f'{self.ancienne_valeur} → {self.nouvelle_valeur}'
        )

    def save(self, *args, **kwargs):
        """RachatNote est immuable — seul l'INSERT est autorisé."""
        if self.pk is not None:
            raise PermissionError('RachatNote est immuable — les mises à jour sont interdites.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError('RachatNote est immuable — la suppression est interdite.')


class ResultatModule(models.Model):
    """
    Résultat agrégé pour un module (moyenne pondérée des éléments du module).
    Art. 13 : compensation toujours entre éléments du même module.
    Calculé par ResultatModuleService après calcul de tous les éléments.
    """
    inscription_ped = models.ForeignKey(
        'inscriptions.InscriptionPedagogique', on_delete=models.CASCADE,
        related_name='resultats_module',
    )
    module          = models.ForeignKey(
        'modules.Module', on_delete=models.PROTECT,
        related_name='resultats',
    )
    session         = models.ForeignKey(
        SessionEvaluation, on_delete=models.PROTECT,
        related_name='resultats_modules',
    )
    moyenne         = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    credits_valides = models.IntegerField(default=0)
    est_valide      = models.BooleanField(default=False)
    a_eliminatoire  = models.BooleanField(
        default=False,
        help_text="True si au moins un élément du module a une moyenne éliminatoire.",
    )
    code_statut     = models.CharField(
        max_length=3, choices=CODE_STATUT_CHOICES, blank=True, default='',
        help_text="Statut du module (V/VCI/VCS/R/NV/NVO/E).",
    )
    date_calcul     = models.DateTimeField(auto_now=True)

    class Meta:
        db_table        = 'evaluations_resultat_module'
        unique_together = ('inscription_ped', 'module', 'session')

    def __str__(self):
        return f'{self.inscription_ped} / {self.module} — {self.moyenne}/20'


class MembreJury(models.Model):
    """
    Membre signataire d'un PV de délibération.
    Art. 24 : 5 membres pour jury de passage/diplôme.
    """
    pv           = models.ForeignKey(
        PVDeliberation, on_delete=models.CASCADE,
        related_name='membres_jury',
    )
    user         = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.PROTECT,
        related_name='jury_participations',
    )
    role         = models.CharField(max_length=30, choices=ROLE_JURY_CHOICES)
    signature_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table        = 'evaluations_membre_jury'
        unique_together = ('pv', 'user')

    def __str__(self):
        return f'{self.get_role_display()} — {self.user} ({self.pv})'


class ObligationRattrapage(models.Model):
    """
    Obligation ou faculté de se présenter à la session de rattrapage.
    Art. 17 : obligatoire (E, NVO) ou facultatif (NV).
    Générée automatiquement par DeliberationSemestreService.
    """
    ligne               = models.ForeignKey(
        LigneDeliberation, on_delete=models.CASCADE,
        related_name='obligations_rattrapage',
    )
    inscription_element = models.ForeignKey(
        'inscriptions.InscriptionElement', on_delete=models.PROTECT,
        related_name='obligations_rattrapage',
    )
    type_obligation     = models.CharField(max_length=20, choices=OBLIGATION_CHOICES)
    code_statut_initial = models.CharField(
        max_length=3, choices=CODE_STATUT_CHOICES, blank=True, default='',
        help_text="Code de l'élément avant rattrapage (E/NVO/NV).",
    )
    motif               = models.TextField(blank=True, default='')

    class Meta:
        db_table        = 'evaluations_obligation_rattrapage'
        unique_together = ('ligne', 'inscription_element')

    def __str__(self):
        return f'{self.get_type_obligation_display()} — {self.inscription_element} ({self.ligne.pv})'


class AnonymatSession(models.Model):
    """
    Numéro d'anonymat par (étudiant × session) — Art. 18 / intégrité des examens.
    Généré aléatoirement par AnonymatService.generer(), immuable ensuite.
    """
    session           = models.ForeignKey(
        SessionEvaluation, on_delete=models.CASCADE,
        related_name='anonymats',
    )
    inscription_admin = models.ForeignKey(
        'inscriptions.InscriptionAdministrative', on_delete=models.CASCADE,
        related_name='anonymats',
    )
    numero_anonymat   = models.PositiveIntegerField()
    genere_le         = models.DateTimeField(auto_now_add=True)
    genere_par        = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='anonymats_generes',
    )

    class Meta:
        db_table        = 'evaluations_anonymat_session'
        unique_together = [
            ('session', 'inscription_admin'),
            ('session', 'numero_anonymat'),
        ]

    def __str__(self):
        return f'Anonymat {self.numero_anonymat} — {self.inscription_admin} / {self.session}'


class JustificatifAnneeBlanche(models.Model):
    """
    Justificatif médical obligatoire pour une année blanche.
    Art. 23 Arrêté 562 / Art. 29 Décret 2018-070 : l'annulation de l'année
    sur avis médical ne doit pas être comptée comme un redoublement.
    """
    ligne_deliberation = models.OneToOneField(
        LigneDeliberation, on_delete=models.PROTECT,
        related_name='justificatif_annee_blanche',
    )
    document      = models.FileField(upload_to='justificatifs_annee_blanche/%Y/')
    motif         = models.TextField()
    decide_par    = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.PROTECT,
        related_name='annees_blanches_decidees',
    )
    date_decision = models.DateField()
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'evaluations_justificatif_annee_blanche'

    def __str__(self):
        return f'Année blanche — {self.ligne_deliberation} ({self.date_decision})'
