"""
Modeles du module Sauvegardes :
  - BackupArtifact      : un fichier .sql.gz cree par cron ou manuellement
  - BackupDownloadGrant : liste blanche des users autorises a telecharger
  - BackupDownloadLog   : audit immuable de chaque telechargement
"""
from django.db import models


# ── Types de sauvegardes (alignes avec les scripts shell cron) ───────────────
TYPE_DAILY_2H   = 'daily_2h'
TYPE_DAILY_14H  = 'daily_14h'
TYPE_WEEKLY     = 'weekly'
TYPE_MONTHLY    = 'monthly'
TYPE_MANUAL     = 'manual'

BACKUP_TYPE_CHOICES = [
    (TYPE_DAILY_2H,  'Quotidien 02h'),
    (TYPE_DAILY_14H, 'Quotidien 14h'),
    (TYPE_WEEKLY,    'Hebdomadaire'),
    (TYPE_MONTHLY,   'Mensuel'),
    (TYPE_MANUAL,    'Manuel chiffre'),
]


class BackupArtifact(models.Model):
    """
    Represente un fichier de sauvegarde present sur le disque du serveur.
    Cree :
      - automatiquement par le scanner periodique (services/scanner.py)
        qui detecte les nouveaux .sql.gz dans /home/backups/...
      - directement par l'endpoint manuel chiffre (lui pointe vers un fichier
        temporaire ou stream direct, selon implementation phase 2)

    L'instance peut survivre a la suppression du fichier disque (rotation
    quotidienne) — dans ce cas le flag `disk_available` passe a False.
    """
    type            = models.CharField(
        max_length=20, choices=BACKUP_TYPE_CHOICES, db_index=True,
    )
    created_at      = models.DateTimeField(db_index=True)
    file_path       = models.CharField(max_length=500)
    file_size_bytes = models.BigIntegerField()
    sha256_hash     = models.CharField(max_length=64, blank=True, default='')
    is_encrypted    = models.BooleanField(default=False)
    triggered_by    = models.ForeignKey(
        'authentication.CustomUser',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
        help_text='Null = sauvegarde automatique cron, sinon user a l\'origine',
    )
    notes           = models.CharField(max_length=200, blank=True, default='')
    disk_available  = models.BooleanField(
        default=True,
        help_text='False quand le fichier disque a ete supprime (rotation)',
    )
    detected_at     = models.DateTimeField(
        auto_now_add=True,
        help_text='Date de premiere detection par le scanner',
    )

    class Meta:
        db_table = 'backup_artifact'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['type', '-created_at']),
            models.Index(fields=['disk_available', '-created_at']),
        ]

    def __str__(self):
        return f'[{self.type}] {self.created_at:%Y-%m-%d %H:%M} ({self.file_size_bytes // 1024} KB)'


class BackupDownloadGrant(models.Model):
    """
    Liste blanche : un user autorise a telecharger les sauvegardes.

    Modele binaire (presence = autorisation). Si l'user n'a pas d'entree ici,
    il ne voit ni la page liste ni le bouton telecharger (sauf admin qui passe
    toujours via le bypass de la permission).

    NB : OneToOne car un user a soit acces, soit pas. Pas de granularite par
    type de backup (decision metier : qui peut telecharger, peut tout).
    """
    user        = models.OneToOneField(
        'authentication.CustomUser',
        on_delete=models.CASCADE,
        related_name='backup_grant',
    )
    granted_by  = models.ForeignKey(
        'authentication.CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        related_name='+',
        help_text='Admin qui a accorde le droit',
    )
    granted_at  = models.DateTimeField(auto_now_add=True)
    notes       = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        db_table = 'backup_download_grant'
        ordering = ['-granted_at']

    def __str__(self):
        return f'Grant : {self.user} (par {self.granted_by})'


class BackupDownloadLog(models.Model):
    """
    Audit IMMUABLE de chaque telechargement.

    Une entree par tentative (reussie OU echouee). Source de verite pour les
    questions "qui a telecharge quoi quand". A ne JAMAIS UPDATE ni DELETE :
    un trigger MySQL est ajoute en migration pour interdire ces operations.
    """
    user            = models.ForeignKey(
        'authentication.CustomUser',
        on_delete=models.PROTECT,
        related_name='+',
    )
    artifact        = models.ForeignKey(
        BackupArtifact,
        on_delete=models.PROTECT,
        related_name='downloads',
    )
    downloaded_at   = models.DateTimeField(auto_now_add=True, db_index=True)
    ip_address      = models.GenericIPAddressField()
    user_agent      = models.TextField(blank=True, default='')
    success         = models.BooleanField(default=False)
    bytes_sent      = models.BigIntegerField(null=True, blank=True)
    failure_reason  = models.CharField(max_length=200, blank=True, default='')
    request_id      = models.CharField(
        max_length=64, blank=True, default='',
        help_text='Correlation avec core_audit_log',
    )

    class Meta:
        db_table = 'backup_download_log'
        ordering = ['-downloaded_at']
        indexes = [
            models.Index(fields=['user', '-downloaded_at']),
            models.Index(fields=['artifact', '-downloaded_at']),
        ]

    def __str__(self):
        status = 'OK' if self.success else 'KO'
        return f'[{status}] {self.user} -> {self.artifact} @ {self.downloaded_at:%Y-%m-%d %H:%M}'
