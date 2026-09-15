import hashlib
import uuid
from django.db import models
from django.db.models import F
from django.utils import timezone


TYPE_DOCUMENT_CHOICES = [
    ('attestation_inscription', "Attestation d'inscription"),
    ('releve_semestre',         'Relevé de notes (semestre)'),
    ('releve_complet',          'Relevé de notes complet'),
    ('attestation_reussite',    'Attestation de réussite'),
    ('attestation_diplome',     'Attestation de diplôme'),
    ('diplome',                 'Diplôme'),
    ('recu_inscription',        "Reçu de paiement (inscription)"),
    # ── Documents officiels IPGEI (plan §5) ─────────────────────────────────
    # Ils reutilisent toute l'infra existante : numerotation, hash SHA-256, QR
    # et page publique /verifier/{token} fonctionnent sans code supplementaire.
    ('ipgei_releve_semestre',       'IPGEI — Relevé de semestre'),
    ('ipgei_releve_annuel',         'IPGEI — Relevé annuel'),
    ('ipgei_decision_deliberation', 'IPGEI — Décision de délibération'),
    ('ipgei_attestation_cnim',      "IPGEI — Attestation d'autorisation CNIM"),
]


class NumeroSerieConfig(models.Model):
    """
    Compteur thread-safe de numérotation par type de document et par institution.
    L'incrément utilise une expression F() pour éviter les race conditions.
    """
    institution    = models.ForeignKey(
        'parametres.Institution', on_delete=models.CASCADE,
        related_name='configs_numerotation',
    )
    type_document  = models.CharField(max_length=40, choices=TYPE_DOCUMENT_CHOICES)
    prefixe        = models.CharField(max_length=10)   # 'AI', 'RS', 'RC', 'AR', 'DI'
    dernier_numero = models.IntegerField(default=0)
    nb_chiffres    = models.IntegerField(default=5)

    class Meta:
        db_table        = 'documents_numero_serie_config'
        unique_together = ('institution', 'type_document')

    def __str__(self):
        return f'{self.prefixe} — {self.get_type_document_display()} ({self.institution})'

    def generer_prochain(self, avec_annee: bool = True) -> str:
        """
        Génère le prochain numéro de série de manière atomique.
        Thread-safe via expression F() : pas de lecture-modification-écriture.

        avec_annee=True  → PREFIXE-AAAA-NNNNN (défaut, documents officiels)
        avec_annee=False → PREFIXE-NNNNNN     (reçus de paiement : REC-000001)
        """
        NumeroSerieConfig.objects.filter(pk=self.pk).update(
            dernier_numero=F('dernier_numero') + 1
        )
        self.refresh_from_db(fields=['dernier_numero'])
        numero_formate = str(self.dernier_numero).zfill(self.nb_chiffres)
        if not avec_annee:
            return f'{self.prefixe}-{numero_formate}'
        annee = timezone.now().year
        return f'{self.prefixe}-{annee}-{numero_formate}'


class DocumentOfficiel(models.Model):
    # Section 1bis institution_V1 — isolation multi-institution
    institution         = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT,
        related_name='documents_officiels',
    )
    etudiant            = models.ForeignKey(
        'absence.Etudiant', on_delete=models.PROTECT, related_name='documents_officiels',
    )
    type_document       = models.CharField(max_length=40, choices=TYPE_DOCUMENT_CHOICES)
    numero_serie        = models.CharField(max_length=50, unique=True)
    token_verification  = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    annee_universitaire = models.CharField(max_length=10, blank=True, default='')
    semestre            = models.ForeignKey(
        'parametres.Semestre', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='documents',
    )
    #: QUEL SEMESTRE la pièce couvre — « S1 » à « S4 ».
    #:
    #: `semestre` ci-dessus pointe vers le `Semestre` du SOCLE, que le cursus
    #: IPGEI n'utilise pas : il a ses propres semestres (`ipgei.SemestreIPGEI`).
    #: Un relevé IPGEI sortait donc du registre sans rien qui dise s'il
    #: couvrait le S3 ou le S4 — deux pièces indistinguables, alors même que la
    #: numérotation les sépare.
    #:
    #: Un CODE et non une clé étrangère : la dépendance ne va aujourd'hui que
    #: d'IPGEI vers ce module, et l'inverser pour un champ d'identification
    #: coûterait plus qu'elle ne rapporte. L'année étant déjà sur le document,
    #: le couple (année, code) désigne le semestre sans ambiguïté.
    #:
    #: Vide pour les pièces qui ne visent pas un semestre — attestation, relevé
    #: annuel — et pour les deux relevés émis avant ce champ, dont rien au
    #: registre ne permet de retrouver le semestre.
    semestre_code       = models.CharField(
        max_length=2, blank=True, default='',
        help_text='Semestre couvert par la pièce : S1 à S4. Vide si sans objet.',
    )
    hash_sha256         = models.CharField(max_length=64, blank=True, default='')
    est_valide          = models.BooleanField(default=True)
    date_generation     = models.DateTimeField(auto_now_add=True)
    genere_par          = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='documents_generes',
    )
    fichier_pdf         = models.FileField(upload_to='documents/officiels/', null=True, blank=True)
    # Horodatage de la PREMIERE production du PDF (= l'original délivré). Toute
    # génération ultérieure est un DUPLICATA (filigrane), conformément au standard
    # universitaire. NULL = jamais généré → la prochaine sera l'original.
    premiere_generation = models.DateTimeField(null=True, blank=True)

    # Signataire, recopié À L'ÉMISSION et non pointé par clé étrangère : le
    # document doit garder le nom qu'il PORTE, même si l'intéressé quitte
    # l'établissement ou change de fonction. Une référence vivante réécrirait
    # après coup ce qu'une pièce déjà remise affirme.
    signataire_nom   = models.CharField(max_length=150, blank=True, default='')
    signataire_titre = models.CharField(max_length=150, blank=True, default='')

    class Meta:
        db_table = 'documents_officiel'
        ordering = ['-date_generation']

    def __str__(self):
        return f'{self.numero_serie} — {self.etudiant}'


class RegistreDiplome(models.Model):
    # Section 1bis institution_V1
    institution         = models.ForeignKey(
        'parametres.Institution', on_delete=models.PROTECT,
        related_name='registres_diplomes',
    )
    etudiant            = models.ForeignKey(
        'absence.Etudiant', on_delete=models.PROTECT, related_name='diplomes',
    )
    filiere             = models.ForeignKey(
        'scolarite.Filiere', on_delete=models.PROTECT, related_name='diplomes',
    )
    numero_diplome      = models.CharField(max_length=50, unique=True)
    mention             = models.CharField(max_length=50, blank=True, default='')
    moyenne_generale    = models.DecimalField(max_digits=5, decimal_places=2)
    date_delivrance     = models.DateField()
    annee_universitaire = models.CharField(max_length=10)
    document            = models.OneToOneField(
        DocumentOfficiel, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='registre_diplome',
    )

    class Meta:
        db_table = 'documents_registre_diplome'
        ordering = ['-date_delivrance']

    def __str__(self):
        return f'Diplôme {self.numero_diplome} — {self.etudiant}'

    def save(self, *args, **kwargs):
        """Le registre des diplômes est append-only."""
        if self.pk is not None:
            raise PermissionError('RegistreDiplome est immuable — les mises à jour sont interdites.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError('RegistreDiplome est immuable — la suppression est interdite.')


class AttestationTravail(models.Model):
    """Attestation enseignant (service fait / enseignement) vérifiable en ligne
    via /verifier/{token}, sur le même dispositif que les documents étudiants."""
    institution         = models.ForeignKey('parametres.Institution', on_delete=models.PROTECT,
                             related_name='attestations_travail', null=True, blank=True)
    prof                = models.ForeignKey('prof.Prof', on_delete=models.PROTECT,
                             related_name='attestations_travail')
    titre_document      = models.CharField(max_length=60, blank=True, default='')
    numero              = models.CharField(max_length=50, unique=True)
    token_verification  = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    annee_universitaire = models.CharField(max_length=10, blank=True, default='')
    date_debut          = models.DateField(null=True, blank=True)
    date_fin            = models.DateField(null=True, blank=True)
    heures_eq_cm        = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    hash_sha256         = models.CharField(max_length=64, blank=True, default='')
    est_valide          = models.BooleanField(default=True)
    date_generation     = models.DateTimeField(auto_now_add=True)
    genere_par          = models.ForeignKey('authentication.CustomUser', on_delete=models.SET_NULL,
                             null=True, blank=True, related_name='attestations_travail_generees')

    class Meta:
        db_table = 'documents_attestation_travail'
        ordering = ['-date_generation']

    def __str__(self):
        return f'{self.numero} — {self.prof}'
