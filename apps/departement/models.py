from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models


class Departement(models.Model):
    """
    Classe de planification annuelle (filière + niveau + groupe + année).
    Ne jamais supprimer une ligne existante (CASCADE sur Etudiant/EM).
    Enrichir in-place uniquement.
    """
    # --- Existant (inchangé) ---
    nom                 = models.CharField(max_length=200)
    description         = models.TextField(blank=True, default='')
    niveau              = models.ForeignKey(
        'parametres.Niveau', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='departements',
    )
    # Decalage en semaines par type de semestre.
    # Cas typique : L1 avec 3 semaines de formation militaire en Impair.
    # Cas plus rare : decalage Pair (stage de demarrage, partiels reportes...).
    # Validators :
    #   - MinValueValidator(0) : pas de decalage negatif (n'a pas de sens)
    #   - MaxValueValidator(15) : sanity cap (un semestre fait ~15-18 semaines,
    #     un decalage plus grand est presque toujours une erreur de frappe)
    decalage_impair     = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(15)],
        help_text="Semaines sautées au début du semestre Impair (rentrée). "
                  "Ex : L1 avec formation militaire de 3 semaines = 3.",
    )
    decalage_pair       = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(15)],
        help_text="Semaines sautées au début du semestre Pair (rare : stage, "
                  "examens reportés…). Mettre 0 si non applicable.",
    )
    annee_universitaire = models.CharField(max_length=20, blank=True, default='')
    code                = models.CharField(max_length=20, blank=True, default='')

    # Section 1bis institution_V1 — passé en NOT NULL après backfill
    institution = models.ForeignKey(
        'parametres.Institution',
        on_delete=models.PROTECT,
        related_name='departements',
    )
    filiere     = models.ForeignKey(
        'scolarite.Filiere',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='departements',
        help_text="Filière académique stable. None pour les modules transversaux (HE, ST).",
    )
    groupe      = models.CharField(
        max_length=20, blank=True, default='',
        help_text="Groupe de TD : 'G1', 'G2', '' si pas de subdivision.",
    )

    # Conteneur d'inscription (ex : STATL1) : recoit les etudiants au moment de
    # l'inscription puis les dispatche vers les groupes reels. Doit etre exclu
    # des UIs de planification (emplois, vacations, suivi) mais reste utilise
    # pour les inscriptions/promotions.
    is_container = models.BooleanField(
        default=False,
        help_text="True = conteneur d'inscription (pas de planning), False = groupe de planification.",
    )

    class Meta:
        db_table = 'departement'
        ordering = ['nom']
        indexes  = [
            models.Index(fields=['filiere', 'annee_universitaire']),
        ]

    def __str__(self):
        return self.nom
