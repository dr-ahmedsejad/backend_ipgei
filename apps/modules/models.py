from decimal import Decimal
from django.db import models
from django.core.exceptions import ValidationError


class Module(models.Model):
    """
    Module LMD (conteneur pédagogique).
    Rattaché à une filière et un semestre. Contient des ElementModule évaluables.
    """
    code              = models.CharField(max_length=20, unique=True)
    intitule_fr       = models.CharField(max_length=200)
    intitule_ar       = models.CharField(max_length=200, blank=True, default='')
    semestre          = models.ForeignKey(
        'parametres.Semestre',
        on_delete=models.PROTECT,
        related_name='modules',
    )
    filiere           = models.ForeignKey(
        'scolarite.Filiere',
        on_delete=models.PROTECT,
        related_name='modules',
    )
    # Scoping multi-institution. Backfill : Institution principale.
    institution       = models.ForeignKey(
        'parametres.Institution',
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='modules',
    )
    credits           = models.PositiveIntegerField(default=0)
    coefficient       = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal('1.00'))
    seuil_compensation = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal('10.00'))
    actif             = models.BooleanField(default=True)

    class Meta:
        db_table = 'modules_module'
        ordering = ['filiere', 'semestre', 'code']
        verbose_name = 'Module'
        verbose_name_plural = 'Modules'

    def __str__(self):
        return f'{self.code} — {self.intitule_fr}'

    def clean(self):
        total = sum(e.credits for e in self.elements.all())
        if self.pk and total and total != self.credits:
            raise ValidationError(
                f'La somme des crédits des éléments ({total}) ne correspond pas aux crédits du module ({self.credits}).'
            )


class ElementModule(models.Model):
    """
    Élément d'un module LMD — unité d'évaluation (CC, Examen, TP…).
    """
    module            = models.ForeignKey(
        Module,
        on_delete=models.CASCADE,
        related_name='elements',
    )
    code              = models.CharField(max_length=20, unique=True)
    intitule_fr       = models.CharField(max_length=200)
    intitule_ar       = models.CharField(max_length=200, blank=True, default='')
    credits           = models.PositiveIntegerField(default=0)
    coefficient       = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal('1.00'))
    # Pondération — fractions décimales (0.30 = 30 %) — somme doit = 1
    poids_cc          = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal('0.30'))
    poids_tp          = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal('0.20'))
    poids_exam        = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal('0.50'))
    seuil_eliminatoire = models.DecimalField(
        max_digits=4, decimal_places=2,
        null=True, blank=True,
        help_text='Note /20 en dessous de laquelle l\'élément est éliminatoire.',
    )
    ordre             = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = 'modules_element'
        ordering = ['module', 'ordre', 'code']
        verbose_name = 'Élément de module'
        verbose_name_plural = 'Éléments de module'

    def __str__(self):
        return f'{self.code} — {self.intitule_fr}'

    def clean(self):
        total = self.poids_cc + self.poids_tp + self.poids_exam
        if total != Decimal('1'):
            raise ValidationError(
                f'La somme des poids CC+TP+Exam doit être égale à 1 (actuel : {total}).'
            )
