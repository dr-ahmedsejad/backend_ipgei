from django.db import models


class Annonce(models.Model):
    """Un message envoyé à des étudiants choisis par cases à cocher.

    Chaque destinataire reçoit une `Notification` de type « annonce » qui porte
    le texte ENTIER : la cloche du portail et l'app Groupe Polytechnique
    l'ouvrent telle quelle. Cette table garde la trace de l'envoi — qui, quand,
    à qui — pour l'historique de l'écran « Annonces ».

    `cibles` : les cases cochées, telles que les décrit `services.grille()`
    (« c12 » = la classe 12). `resume` : leur libellé lisible
    (« Toutes les classes MPSI »), figé à l'envoi.
    """
    # LE PUBLIC (décision du 11/10/2026, comme à l'ESP) : les élèves des
    # classes, et/ou les enseignants — tous, ou ceux qui ont cours dans les
    # classes cochées.
    ENSEIGNANTS_CHOICES = [('', 'Aucun'), ('tous', 'Tous'), ('cases', 'Ceux des cases')]
    etudiants = models.BooleanField(default=True)
    enseignants = models.CharField(max_length=10, blank=True, default='',
                                   choices=ENSEIGNANTS_CHOICES)
    titre = models.CharField(max_length=200)
    texte = models.TextField()
    cibles = models.JSONField(default=list)
    resume = models.CharField(max_length=500, blank=True, default='')
    nb_destinataires = models.PositiveIntegerField(default=0)
    auteur = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+')
    auteur_nom = models.CharField(max_length=150, blank=True, default='')
    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'annonces_annonce'
        ordering = ['-cree_le']

    def __str__(self):
        return f'{self.titre} → {self.resume}'
