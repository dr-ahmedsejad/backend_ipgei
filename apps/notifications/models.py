from django.db import models


TYPE_NOTIFICATION_CHOICES = [
    ('info',           'Info'),
    ('succes',         'Succès'),
    ('avertissement',  'Avertissement'),
    ('erreur',         'Erreur'),
    ('preinscription', 'Pré-inscription'),
    ('inscription',    'Inscription'),
    ('evaluation',     'Évaluation'),
    ('document',       'Document'),
    ('stage',          'Stage'),
]


class Notification(models.Model):
    destinataire    = models.ForeignKey(
        'authentication.CustomUser', on_delete=models.CASCADE,
        related_name='notifications',
    )
    titre           = models.CharField(max_length=200)
    message         = models.TextField()
    type            = models.CharField(max_length=20, choices=TYPE_NOTIFICATION_CHOICES, default='info')
    lue             = models.BooleanField(default=False)
    lien            = models.CharField(max_length=500, blank=True, default='')
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'notifications_notification'
        ordering = ['-created_at']

    def __str__(self):
        return f'[{self.type}] {self.titre} → {self.destinataire}'
