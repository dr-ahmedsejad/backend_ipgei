from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator


class EM(models.Model):
    # --- Existant (inchangé) ---
    code_em     = models.CharField(max_length=50)
    intitule    = models.CharField(max_length=200)
    CM          = models.IntegerField(default=0)
    TD          = models.IntegerField(default=0)
    TP          = models.IntegerField(default=0)
    PR          = models.IntegerField(default=0)
    # IDENTITÉ STABLE de l'EM : la filière (indépendante de l'année et du groupe).
    # Un même EM (code_em) existe une seule fois par filière et est réutilisé par
    # tous les groupes et toutes les années. Le lien groupe↔EM est DÉRIVÉ
    # (filière + niveau du semestre), jamais stocké.
    filiere     = models.ForeignKey(
        'scolarite.Filiere', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ems',
    )
    # Section 1bis G4 institution_V1 — découplage EM/Departement annuel.
    # VESTIGIAL : `departement` (groupe annuel) ne sert PLUS d'identité (remplacé
    # par `filiere`). Conservé nullable pour rétro-compat ; SET_NULL garantit que
    # supprimer un Departement annuel ne détruit jamais l'EM.
    departement = models.ForeignKey(
        'departement.Departement', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ems',
    )
    # SET_NULL (comme `departement` ci-dessus) : supprimer un Semestre ne doit
    # pas détruire en cascade tous les EM rattachés (purge année = perte de données).
    semestre    = models.ForeignKey(
        'parametres.Semestre', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ems',
    )
    module_lmd  = models.ForeignKey(
        'modules.Module',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='ems_planification',
        help_text='Module LMD auquel cet EM est rattaché (crédits, filière, semestre).',
    )

    # --- Nouveaux champs scolarite LMD (nullable → zero impact existant) ---
    credits     = models.IntegerField(null=True, blank=True)
    coefficient = models.IntegerField(null=True, blank=True)

    seuil_eliminatoire = models.DecimalField(
        max_digits=4, decimal_places=2, default=6.00,
        help_text="Note en dessous de laquelle l'UE est éliminatoire (ex : 6.00)",
    )

    has_tp = models.BooleanField(
        default=False,
        help_text="L'élément comporte une note de TP (pondération /5).",
    )

    # Scoping multi-institution. Backfill : Institution principale.
    institution = models.ForeignKey(
        'parametres.Institution',
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='ems',
    )

    class Meta:
        db_table = 'em'
        ordering = ['code_em']
        constraints = [
            # Un même code_em existe une seule fois par filière (réutilisé partout).
            # Remplace l'ancienne unicité (code_em, departement) qui forçait un EM
            # par groupe/année.
            models.UniqueConstraint(
                fields=['code_em', 'filiere'],
                name='uniq_em_code_em_filiere',
            ),
        ]

    def save(self, *args, **kwargs):
        # Filière = identité stable. Si non fournie mais module LMD présent, la
        # déduire (le module porte la filière stable).
        if self.filiere_id is None and self.module_lmd_id is not None:
            self.filiere_id = self.module_lmd.filiere_id
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.code_em} - {self.intitule}'

    @property
    def volume_horaire_total(self):
        return self.CM + self.TD + self.TP + self.PR


class VolumeAnnuelEM(models.Model):
    """
    Le volume horaire d'un EM, FIGÉ pour une année.

    POURQUOI. Il n'existe qu'UN EM par matière, partagé par toutes les
    promotions : `em.Matiere` n'est pas datée, et le miroir IPGEI réécrit l'EM à
    chaque modification de la maquette. Or l'écran « Avancement » lit `em.CM` en
    DIRECT comme volume de référence. Ramener MATH-S1 de 147 h à 100 h pour la
    rentrée faisait donc passer l'avancement de 2024-2025 de 1 % à 2 % — une
    année révolue dont le chiffre change sans que personne n'y ait touché, et
    sans la moindre trace.

    C'est l'idiome que `Note` applique déjà à son coefficient : ce qui a servi
    au calcul est recopié dans la ligne, et reste lisible après coup. La
    maquette de l'année est gelée ici, au moment où le suivi de cette année naît
    — pas plus tard, sinon on gèlerait déjà une valeur retouchée.

    Une ligne par (année, EM) : 26 matières, donc 26 lignes par année. C'est
    aussi, incidemment, la seule histoire de maquette dont l'établissement
    dispose.

    L'alternative — dater `Matiere` — a été écartée : chaque matière veut son
    EM, et `code_em` est unique par filière. `MATH-S1` existerait trois fois.
    Il faudrait le suffixer par l'année, donc renommer, donc rompre le lien avec
    toutes les vacations déjà saisies. Beaucoup plus cher, pour le même résultat.
    """
    annee_universitaire = models.CharField(max_length=20, db_index=True)
    em = models.ForeignKey(
        EM, on_delete=models.CASCADE, related_name='volumes_annuels',
    )
    CM = models.IntegerField(default=0)
    TD = models.IntegerField(default=0)
    TP = models.IntegerField(default=0)
    PR = models.IntegerField(default=0)
    fige_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'em_volume_annuel'
        ordering = ['-annee_universitaire', 'em__code_em']
        constraints = [
            models.UniqueConstraint(
                fields=['annee_universitaire', 'em'],
                name='uniq_volume_annuel_annee_em',
            ),
        ]
        verbose_name        = 'volume annuel figé'
        verbose_name_plural = 'volumes annuels figés'

    def __str__(self):
        return f'{self.em.code_em} — {self.annee_universitaire} ({self.CM}/{self.TD}/{self.TP})'
