from django.db import models

from core.validators import validate_document


TYPE_CHOICES = [
    ('absence', 'Absence'),
    ('note',    'Note'),
    ('autre',   'Autre'),
]

STATUT_CHOICES = [
    ('soumise',  'Soumise'),
    ('en_cours', 'En cours'),
    ('acceptee', 'Acceptée'),
    ('rejetee',  'Rejetée'),
]


class Reclamation(models.Model):
    etudiant             = models.ForeignKey(
        'absence.Etudiant',
        on_delete=models.CASCADE,
        related_name='reclamations',
    )
    type_reclamation     = models.CharField(max_length=20, choices=TYPE_CHOICES, default='autre')
    statut               = models.CharField(max_length=20, choices=STATUT_CHOICES, default='soumise')

    # Références optionnelles selon le type
    presence             = models.ForeignKey(
        'absence.Presence',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='reclamations',
    )
    inscription_element  = models.ForeignKey(
        'inscriptions.InscriptionElement',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='reclamations',
    )
    session_evaluation   = models.ForeignKey(
        'evaluations.SessionEvaluation',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='reclamations',
    )

    motif                = models.TextField()
    justificatif         = models.FileField(upload_to='reclamations/justificatifs/', null=True, blank=True,
                                             validators=[validate_document])

    # Traitement staff
    reponse              = models.TextField(blank=True, default='')
    traitee_par          = models.ForeignKey(
        'authentication.CustomUser',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='reclamations_traitees',
    )
    date_soumission      = models.DateTimeField(auto_now_add=True)
    date_traitement      = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'reclamations_reclamation'
        ordering = ['-date_soumission']

    def __str__(self):
        return f'{self.etudiant} — {self.type_reclamation} ({self.statut})'


TYPE_SESSION_CHOICES = [
    ('normale',    'Normale (SN)'),
    ('rattrapage', 'Rattrapage (SR)'),
]

TYPE_SEMESTRE_CHOICES = [
    ('I', 'Impairs (S1 / S3 / S5)'),
    ('P', 'Pairs (S2 / S4 / S6)'),
]


class PeriodeReclamation(models.Model):
    """
    Fenetre temporelle pendant laquelle les etudiants peuvent reclamer
    sur leurs notes pour un perimetre (annee + parite + session) donne.
    Cree par la SG apres cloture d'une session SR pour donner X jours
    aux etudiants pour formuler des reclamations sur leurs notes finales.
    """
    annee_univ      = models.ForeignKey(
        'parametres.Year', on_delete=models.PROTECT,
        related_name='periodes_reclamation',
    )
    type_session    = models.CharField(max_length=20, choices=TYPE_SESSION_CHOICES, default='rattrapage')
    type_semestre   = models.CharField(max_length=1, choices=TYPE_SEMESTRE_CHOICES)
    institution     = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT,
        related_name='periodes_reclamation',
    )
    filiere         = models.ForeignKey(
        'scolarite.Filiere', on_delete=models.PROTECT,
        null=True, blank=True, related_name='periodes_reclamation',
        help_text="Si vide : toutes les filieres de l'institution.",
    )
    niveau          = models.IntegerField(
        null=True, blank=True,
        help_text="Si vide : tous les niveaux.",
    )

    date_ouverture  = models.DateTimeField()
    date_fermeture  = models.DateTimeField()
    actif           = models.BooleanField(default=True)

    cree_par        = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='periodes_reclamation_creees',
    )
    motif           = models.CharField(
        max_length=200, blank=True, default='',
        help_text="Texte court affiche aux etudiants (ex : 'Reclamation SR-I 2025-2026').",
    )
    date_creation     = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'reclamations_periode'
        ordering = ['-date_ouverture']
        indexes = [
            models.Index(fields=['actif', 'date_ouverture', 'date_fermeture']),
            models.Index(fields=['annee_univ', 'type_semestre']),
        ]

    def __str__(self):
        return (
            f'Periode {self.get_type_session_display()} {self.get_type_semestre_display()} '
            f'{self.annee_univ} — {self.date_ouverture:%d/%m %H:%M} → {self.date_fermeture:%d/%m %H:%M}'
        )

    @property
    def est_en_cours(self):
        from django.utils import timezone
        now = timezone.now()
        return self.actif and self.date_ouverture <= now <= self.date_fermeture

    @property
    def statut_temporel(self):
        """'a_venir' | 'en_cours' | 'fermee' | 'inactive'"""
        if not self.actif:
            return 'inactive'
        from django.utils import timezone
        now = timezone.now()
        if now < self.date_ouverture:
            return 'a_venir'
        if now > self.date_fermeture:
            return 'fermee'
        return 'en_cours'


# ═════════════════════════════════════════════════════════════════════════════
# BOÎTE DE RÉCEPTION — ce qui s'écrit EN LIGNE, sur le miroir
# ═════════════════════════════════════════════════════════════════════════════
# Ces deux tables sont exclues EN ENTIER du dump de synchronisation
# (settings.SYNC_EXCLUDE_TABLE) : la restauration ne les touche pas, donc ce
# qu'un enseignant y dépose survit au push suivant.
#
# D'où la règle qui gouverne leur dessin : AUCUNE ForeignKey vers une table
# miroir. Le dump restaure avec DROP ... CASCADE ; une clé étrangère entrante
# ferait échouer le DROP de la table visée, et la restauration entière avec elle.
# On stocke donc des identifiants nus (*_id) et un INSTANTANÉ des libellés — qui
# a en outre le mérite de survivre à la disparition de ce qu'il désigne.

STATUT_SEANCE_CHOICES = [
    ('en_attente', 'En attente'),
    ('acceptee',   'Acceptée'),
    ('rejetee',    'Rejetée'),
]


class ReclamationSeance(models.Model):
    """
    Un enseignant conteste EN LIGNE une séance portée « Non fait ».

    La séance n'est pas référencée par sa clé primaire — elle vivrait dans une
    table miroir — mais par sa CLÉ NATURELLE : année, période, semaine, matière,
    jour et créneau. C'est ce qui permet au local de la retrouver au moment de
    traiter, même si les identifiants ont bougé entre-temps.
    """
    # ── Auteur ───────────────────────────────────────────────────────────────
    prof_id  = models.IntegerField(db_index=True)
    prof_nom = models.CharField(max_length=200, blank=True, default='')
    user_id  = models.IntegerField(null=True, blank=True)

    # ── Clé naturelle de la séance ───────────────────────────────────────────
    annee_universitaire = models.CharField(max_length=20, blank=True, db_index=True)
    type_semestre       = models.CharField(max_length=10, blank=True, default='')
    numero_semaine      = models.IntegerField(default=0)
    em_id               = models.IntegerField(null=True, blank=True)
    jour                = models.CharField(max_length=20, blank=True, default='')
    creneau             = models.CharField(max_length=50, blank=True, default='')
    #: Indicatif seulement : l'identifiant du pointage au moment de la
    #: réclamation. Le rattachement se fait sur la clé naturelle, pas sur lui.
    pointage_id         = models.IntegerField(null=True, blank=True)
    institution_id      = models.IntegerField(null=True, blank=True, db_index=True)

    # ── Instantané d'affichage ───────────────────────────────────────────────
    em_code             = models.CharField(max_length=50,  blank=True, default='')
    em_intitule         = models.CharField(max_length=255, blank=True, default='')
    date_seance         = models.DateField(null=True, blank=True)
    commentaire_origine = models.CharField(max_length=100, blank=True,
                                           default='Non fait')

    # ── La réclamation elle-même ─────────────────────────────────────────────
    motif           = models.TextField()
    statut          = models.CharField(max_length=20, choices=STATUT_SEANCE_CHOICES,
                                       default='en_attente')
    reponse         = models.TextField(blank=True, default='')
    traite_par_id   = models.IntegerField(null=True, blank=True)
    traite_par_nom  = models.CharField(max_length=200, blank=True, default='')
    date_soumission = models.DateTimeField(auto_now_add=True)
    date_traitement = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'reclamations_reclamation_seance'
        ordering = ['-date_soumission']
        indexes = [
            models.Index(fields=['prof_id', 'annee_universitaire', 'statut']),
        ]

    def __str__(self):
        return (f'{self.prof_nom} — {self.em_code} '
                f'semaine {self.numero_semaine} ({self.statut})')


STATUT_SAISIE_CHOICES = [
    ('brouillon', 'Brouillon'),
    ('exporte',   'Exporté'),
]


class SaisieNoteEnLigne(models.Model):
    """
    Une note saisie EN LIGNE par un enseignant — un brouillon, rien de plus.

    Ce n'est PAS une note officielle : la direction exporte le classeur, puis
    saisit en local, où seule la note fait foi. On n'écrit jamais dans
    `ipgei_note` depuis ici — le miroir n'a pas autorité sur les résultats.

    ELLE VISE UNE NOTE IPGEI. La cible était `InscriptionElement` et
    `SessionEvaluation`, de la couche LMD que le fork a remplacée et dont les
    tables portent **zéro ligne** : l'écran rendait une grille vide à tout
    enseignant, sans erreur. Les champs pointent désormais `ipgei.Note`.

    Des identifiants NUS, sans clé étrangère, comme partout dans cette boîte de
    réception : ces lignes survivent au dump qui restaure le miroir (cf.
    `SYNC_EXCLUDE_TABLE`), donc elles ne peuvent pas dépendre d'objets que la
    restauration remplace. L'instantané qui les accompagne — matricule, code de
    matière, classe — permet d'afficher et d'exporter sans rien interroger.

    `numero` porte le rang du devoir : l'IPGEI compte DS 1..N et examens 1..N,
    là où le LMD n'avait qu'une case par type. Sans lui, saisir le DS 2
    écraserait le DS 1.
    """
    prof_id  = models.IntegerField(db_index=True)
    prof_nom = models.CharField(max_length=200, blank=True, default='')
    user_id  = models.IntegerField(null=True, blank=True)

    #: La ligne de notes visée — `ipgei.Note.id`. Identifiant nu : le miroir est
    #: une copie du local, les identifiants y sont les mêmes.
    note_ipgei_id  = models.IntegerField(db_index=True)
    #: 'ds' | 'tp' | 'exam' | 'ratt' — le vocabulaire IPGEI, pas le 'CC' du socle.
    type_note      = models.CharField(max_length=10)
    #: Rang du devoir dans son type. 1 pour le TP et le rattrapage, qui sont uniques.
    numero         = models.PositiveSmallIntegerField(default=1)
    matiere_id     = models.IntegerField(null=True, blank=True, db_index=True)
    classe_id      = models.IntegerField(null=True, blank=True, db_index=True)
    semestre_id    = models.IntegerField(null=True, blank=True, db_index=True)
    institution_id = models.IntegerField(null=True, blank=True, db_index=True)
    #: L'année, que la table ne portait pas : rien n'aurait distingué les
    #: brouillons d'une promotion de ceux de la suivante, et la table n'est
    #: jamais réinitialisée par une publication.
    annee_universitaire = models.CharField(max_length=20, blank=True, default='',
                                           db_index=True)

    etudiant_matricule = models.CharField(max_length=50,  blank=True, default='')
    etudiant_nom       = models.CharField(max_length=255, blank=True, default='')
    matiere_code       = models.CharField(max_length=50,  blank=True, default='')
    matiere_intitule   = models.CharField(max_length=255, blank=True, default='')
    classe_nom         = models.CharField(max_length=100, blank=True, default='')
    semestre_code      = models.CharField(max_length=10,  blank=True, default='')

    valeur = models.DecimalField(max_digits=5, decimal_places=2,
                                 null=True, blank=True)

    statut            = models.CharField(max_length=20, choices=STATUT_SAISIE_CHOICES,
                                         default='brouillon')
    date_saisie       = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)
    exporte_le        = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'saisie_note_en_ligne'
        ordering = ['-date_modification']
        #: Un seul brouillon par case : la saisie se rejoue autant qu'on veut,
        #: elle écrase la précédente au lieu d'empiler des doublons. `numero`
        #: en fait partie, sans quoi le DS 2 écraserait le DS 1.
        unique_together = ('prof_id', 'note_ipgei_id', 'type_note', 'numero')
        indexes = [
            models.Index(fields=['prof_id', 'semestre_id', 'statut'],
                         name='saisie_note_prof_sem_idx'),
            models.Index(fields=['classe_id', 'matiere_id', 'semestre_id'],
                         name='saisie_note_feuille_idx'),
        ]

    def __str__(self):
        rang = f' {self.numero}' if self.type_note in ('ds', 'exam') else ''
        return (f'{self.prof_nom} — {self.etudiant_matricule} '
                f'{self.type_note}{rang}={self.valeur} ({self.statut})')
