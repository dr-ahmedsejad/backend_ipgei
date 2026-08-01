"""
Serializers backup. Pas de `__all__` : on whitelist explicitement les champs
exposes pour eviter toute fuite d'info sensible (file_path serveur, etc.).
"""
import os

from rest_framework import serializers

from .models import BackupArtifact, BackupDownloadGrant, BackupDownloadLog


class BackupArtifactSerializer(serializers.ModelSerializer):
    """
    Listing des sauvegardes disponibles. On expose le NOM de fichier
    (basename) plutot que le chemin complet pour ne pas reveler la structure
    serveur a un user IT compromis.
    """
    filename       = serializers.SerializerMethodField()
    type_label     = serializers.SerializerMethodField()
    size_human     = serializers.SerializerMethodField()
    triggered_by_username = serializers.CharField(
        source='triggered_by.username', read_only=True, default='',
    )

    class Meta:
        model  = BackupArtifact
        fields = [
            'id', 'type', 'type_label', 'created_at',
            'filename', 'file_size_bytes', 'size_human',
            'is_encrypted', 'disk_available',
            'triggered_by_username', 'notes',
        ]
        read_only_fields = fields

    def get_filename(self, obj) -> str:
        return os.path.basename(obj.file_path)

    def get_type_label(self, obj) -> str:
        return obj.get_type_display()

    def get_size_human(self, obj) -> str:
        size = obj.file_size_bytes
        for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
            if size < 1024:
                return f'{size:.1f} {unit}'.rstrip('0').rstrip('.')
            size /= 1024
        return f'{size:.1f} PB'


class BackupDownloadGrantSerializer(serializers.ModelSerializer):
    """
    Vue admin de la matrice (liste des users autorises).
    Le grant est cree via POST avec uniquement {user, notes} ; granted_by
    est rempli cote view a partir de request.user.
    """
    user_username   = serializers.CharField(source='user.username', read_only=True)
    user_name       = serializers.CharField(source='user.name',     read_only=True)
    user_role       = serializers.CharField(source='user.role',     read_only=True)
    granted_by_username = serializers.CharField(
        source='granted_by.username', read_only=True, default='',
    )

    class Meta:
        model  = BackupDownloadGrant
        fields = [
            'id', 'user',
            'user_username', 'user_name', 'user_role',
            'granted_by_username', 'granted_at', 'notes',
        ]
        read_only_fields = [
            'id', 'user_username', 'user_name', 'user_role',
            'granted_by_username', 'granted_at',
        ]


class BackupDownloadLogSerializer(serializers.ModelSerializer):
    """Audit log : strictement lecture seule (et triggers MySQL le forcent)."""
    user_username = serializers.CharField(source='user.username', read_only=True)
    artifact_filename = serializers.SerializerMethodField()

    class Meta:
        model  = BackupDownloadLog
        fields = [
            'id', 'user_username', 'artifact', 'artifact_filename',
            'downloaded_at', 'ip_address', 'user_agent',
            'success', 'bytes_sent', 'failure_reason', 'request_id',
        ]
        read_only_fields = fields

    def get_artifact_filename(self, obj) -> str:
        return os.path.basename(obj.artifact.file_path)


class ManualBackupRequestSerializer(serializers.Serializer):
    """
    Payload pour POST /backups/manual/ : juste un mdp + notes optionnelles.
    Le mdp est valide cote view via le service (longueur min via settings).
    """
    password = serializers.CharField(
        write_only=True, min_length=1, max_length=200, trim_whitespace=False,
        help_text='Mot de passe de chiffrement. Au moins BACKUP_MANUAL_MIN_PASSWORD_LENGTH caracteres.',
    )
    notes    = serializers.CharField(
        required=False, allow_blank=True, max_length=200, default='',
    )
