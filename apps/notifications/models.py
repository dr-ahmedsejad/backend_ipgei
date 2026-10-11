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
    ('annonce',        'Annonce'),
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
        indexes = [
            # Cloche : notifications non lues d'un utilisateur.
            models.Index(fields=['destinataire', 'lue'], name='notif_dest_lue_idx'),
            # envoyer_push / dernière notification : les plus récentes.
            models.Index(fields=['created_at'], name='notif_created_idx'),
        ]

    def __str__(self):
        return f'[{self.type}] {self.titre} → {self.destinataire}'


class AppareilPush(models.Model):
    """Téléphone inscrit aux notifications push (jeton FCM) — app Groupe
    Polytechnique. Repris de l'ISS. Un jeton = un appareil ; s'il change de
    compte, il le suit."""
    user_id    = models.BigIntegerField(db_index=True)
    jeton      = models.CharField(max_length=512, unique=True)
    plateforme = models.CharField(max_length=20, default='android')
    langue     = models.CharField(max_length=5, default='fr')
    # App d'où vient le jeton : 'gp' = Groupe Polytechnique (la seule à l'IPGEI).
    projet     = models.CharField(max_length=20, blank=True, default='gp')
    cree_le    = models.DateTimeField(auto_now_add=True)
    vu_le      = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'notifications_appareil'

    def __str__(self):
        return f'appareil {self.plateforme} de #{self.user_id}'


class PushEnvoye(models.Model):
    """Notification déjà traitée par `envoyer_push` (poussée, ou sans appareil).
    Clé : (identifiant, date de création). Prise AVANT l'envoi : deux exécutions
    qui se chevauchent ne poussent pas deux fois."""
    notification_id = models.BigIntegerField()
    notification_le = models.DateTimeField()
    nb_appareils    = models.PositiveIntegerField(default=0)
    traite_le       = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'notifications_push_envoye'
        indexes = [
            models.Index(fields=['notification_le'], name='push_envoye_le_idx'),
        ]
        constraints = [
            models.UniqueConstraint(fields=['notification_id', 'notification_le'],
                                    name='notifications_push_envoye_unique'),
        ]

    def __str__(self):
        return f'push de la notification #{self.notification_id}'
