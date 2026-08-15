"""
AuditLog — journal d'audit en base de donnees.

Table APPEND-ONLY : les methodes update() et delete() sont bloquees
via le manager custom AppendOnlyManager.

Complete le logging fichier existant (AuditMixin) avec une tracabilite
durable et requetable en DB.
"""
from django.db import models


# ── Constantes d'actions ─────────────────────────────────────────────────────
ACTION_CREATE            = 'CREATE'
ACTION_UPDATE            = 'UPDATE'
ACTION_DELETE            = 'DELETE'
ACTION_BULK_CREATE       = 'BULK_CREATE'
ACTION_BULK_UPDATE       = 'BULK_UPDATE'
ACTION_BULK_DELETE       = 'BULK_DELETE'
ACTION_ARCHIVE           = 'ARCHIVE'
ACTION_RESTORE           = 'RESTORE'
ACTION_LOGIN_SUCCESS     = 'LOGIN_SUCCESS'
ACTION_LOGIN_FAILED      = 'LOGIN_FAILED'
ACTION_LOGOUT            = 'LOGOUT'
ACTION_PASSWORD_CHANGED  = 'PASSWORD_CHANGED'
ACTION_PASSWORD_RESET    = 'PASSWORD_RESET'
ACTION_PERMISSION_DENIED = 'PERMISSION_DENIED'

ACTION_CHOICES = [
    (ACTION_CREATE,            'Création'),
    (ACTION_UPDATE,            'Modification'),
    (ACTION_DELETE,            'Suppression'),
    (ACTION_BULK_CREATE,       'Création en masse'),
    (ACTION_BULK_UPDATE,       'Modification en masse'),
    (ACTION_BULK_DELETE,       'Suppression en masse'),
    (ACTION_ARCHIVE,           'Archivage'),
    (ACTION_RESTORE,           'Restauration'),
    (ACTION_LOGIN_SUCCESS,     'Connexion réussie'),
    (ACTION_LOGIN_FAILED,      'Échec de connexion'),
    (ACTION_LOGOUT,            'Déconnexion'),
    (ACTION_PASSWORD_CHANGED,  'Mot de passe modifié'),
    (ACTION_PASSWORD_RESET,    'Réinitialisation MDP'),
    (ACTION_PERMISSION_DENIED, 'Accès refusé'),
]


class AppendOnlyManager(models.Manager):
    """Manager qui interdit toute modification du journal d'audit."""
    pass


class AuditLogBase(models.Model):
    """Champs communs entre AuditLog (hot) et AuditLogArchive (warm)."""
    user        = models.ForeignKey(
        'authentication.CustomUser',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    action       = models.CharField(max_length=20, choices=ACTION_CHOICES)
    model_name   = models.CharField(max_length=100)
    object_id    = models.CharField(max_length=50)
    changes      = models.JSONField(default=dict)
    ip_address   = models.GenericIPAddressField(null=True, blank=True)
    user_agent   = models.TextField(blank=True, default='')
    timestamp    = models.DateTimeField(auto_now_add=True, db_index=True)

    institution  = models.ForeignKey(
        'parametres.Institution',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    request_id   = models.CharField(max_length=36, blank=True, default='')
    label        = models.CharField(max_length=200, blank=True, default='')
    endpoint     = models.CharField(max_length=200, blank=True, default='')
    http_method  = models.CharField(max_length=10, blank=True, default='')
    keep_forever = models.BooleanField(default=False)

    class Meta:
        abstract = True

    def __str__(self):
        return f'[{self.timestamp:%Y-%m-%d %H:%M}] {self.action} {self.model_name}#{self.object_id}'

    def save(self, *args, **kwargs):
        """Autorise uniquement l'INSERT (pk is None), bloque tout UPDATE."""
        if self.pk is not None:
            raise PermissionError('AuditLog est immuable — les mises à jour sont interdites.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'AuditLog est immuable — utiliser purge_audit_logs pour la rétention.'
        )


class AuditLog(AuditLogBase):
    """Journal d'audit HOT (0–90 jours, recherche instantanée)."""
    objects = AppendOnlyManager()

    class Meta:
        db_table = 'core_audit_log'
        ordering = ['-timestamp']
        indexes  = [
            models.Index(fields=['model_name', 'object_id', 'timestamp'],
                         name='idx_audit_model_obj_ts'),
            models.Index(fields=['user', 'timestamp']),
            models.Index(fields=['action', 'timestamp'],
                         name='idx_audit_action_ts'),
            models.Index(fields=['institution', 'timestamp'],
                         name='idx_audit_inst_ts'),
        ]


class AuditLogArchive(AuditLogBase):
    """
    Journal d'audit ARCHIVE (90 jours - 1 an, recherche un peu plus lente).
    Alimente par la commande archive_audit_logs.
    """
    objects = AppendOnlyManager()

    class Meta:
        db_table = 'core_audit_log_archive'
        ordering = ['-timestamp']
        indexes  = [
            models.Index(fields=['model_name', 'object_id', 'timestamp'],
                         name='idx_auditarc_mod_obj_ts'),
            models.Index(fields=['user', 'timestamp']),
            models.Index(fields=['action', 'timestamp'],
                         name='idx_auditarc_action_ts'),
            models.Index(fields=['institution', 'timestamp'],
                         name='idx_auditarc_inst_ts'),
        ]


class SyncLog(models.Model):
    """
    Historique des publications LOCAL → VPS : une entrée par appui sur
    « Publier », qu'elle aboutisse ou non.

    Écrit sur le serveur LOCAL, seul autorisé à pousser. Le VPS le reçoit à la
    synchronisation suivante — il y lit donc l'histoire jusqu'à l'avant-dernier
    push, ce qui suffit à répondre à la seule question qui compte en ligne :
    « de quand date ce que je consulte ? »

    L'auteur est un INSTANTANÉ de son nom, pas une clé étrangère : un compte
    supprimé ne doit pas emporter la trace de ce qu'il a publié.
    """
    STATUT_CHOICES = [
        ('synced',     'Synchronisé'),
        ('dump_ready', 'Dump prêt (local)'),
        ('error',      'Échec'),
    ]

    cree_le     = models.DateTimeField(auto_now_add=True, db_index=True)
    par         = models.CharField(max_length=150, blank=True, default='')
    statut      = models.CharField(max_length=20, choices=STATUT_CHOICES,
                                   default='synced')
    transferred = models.BooleanField(default=False)
    size        = models.BigIntegerField(default=0)
    sha256      = models.CharField(max_length=64, blank=True, default='')
    duree_s     = models.FloatField(default=0)
    vps         = models.TextField(blank=True, default='')
    erreur      = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'core_sync_log'
        ordering = ['-cree_le']

    def __str__(self):
        return f'{self.cree_le:%Y-%m-%d %H:%M} — {self.statut} ({self.par})'
