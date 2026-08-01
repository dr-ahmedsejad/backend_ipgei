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
