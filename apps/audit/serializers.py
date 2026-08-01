"""
Serializers pour AuditLog — exposition lecture seule du journal d'audit.
"""
from rest_framework import serializers

from core.models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    """Vue complete d'une entree d'audit, avec champs derives pour l'UI."""
    user_username = serializers.SerializerMethodField()
    user_full_name = serializers.SerializerMethodField()
    user_role = serializers.SerializerMethodField()
    institution_nom = serializers.SerializerMethodField()
    action_label = serializers.CharField(source='get_action_display', read_only=True)
    timestamp_iso = serializers.DateTimeField(source='timestamp', read_only=True)

    class Meta:
        model = AuditLog
        fields = [
            'id',
            'timestamp', 'timestamp_iso',
            'user', 'user_username', 'user_full_name', 'user_role',
            'action', 'action_label',
            'model_name', 'object_id', 'label',
            'changes',
            'institution', 'institution_nom',
            'ip_address', 'user_agent',
            'request_id', 'endpoint', 'http_method',
            'keep_forever',
        ]
        read_only_fields = fields  # journal append-only : tout est read-only

    def get_user_username(self, obj):
        return obj.user.username if obj.user_id else None

    def get_user_full_name(self, obj):
        if not obj.user_id:
            return None
        u = obj.user
        full = f'{getattr(u, "first_name", "")} {getattr(u, "last_name", "")}'.strip()
        return full or u.username

    def get_user_role(self, obj):
        return getattr(obj.user, 'role', None) if obj.user_id else None

    def get_institution_nom(self, obj):
        if not obj.institution_id:
            return None
        try:
            return str(obj.institution)[:200]
        except Exception:
            return None


class AuditLogListSerializer(AuditLogSerializer):
    """Variante allegee pour la liste : pas de user_agent (volumineux)."""
    class Meta(AuditLogSerializer.Meta):
        fields = [
            'id', 'timestamp', 'timestamp_iso',
            'user', 'user_username', 'user_full_name', 'user_role',
            'action', 'action_label',
            'model_name', 'object_id', 'label',
            'institution', 'institution_nom',
            'ip_address',
            'endpoint', 'http_method',
        ]
        read_only_fields = fields
