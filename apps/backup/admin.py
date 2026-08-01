from django.contrib import admin

from .models import BackupArtifact, BackupDownloadGrant, BackupDownloadLog


@admin.register(BackupArtifact)
class BackupArtifactAdmin(admin.ModelAdmin):
    list_display  = ('type', 'created_at', 'file_size_bytes', 'is_encrypted',
                     'disk_available', 'triggered_by')
    list_filter   = ('type', 'is_encrypted', 'disk_available')
    search_fields = ('file_path', 'sha256_hash', 'notes')
    readonly_fields = ('detected_at',)
    ordering      = ('-created_at',)


@admin.register(BackupDownloadGrant)
class BackupDownloadGrantAdmin(admin.ModelAdmin):
    list_display  = ('user', 'granted_by', 'granted_at', 'notes')
    search_fields = ('user__username', 'user__name', 'notes')
    raw_id_fields = ('user', 'granted_by')


@admin.register(BackupDownloadLog)
class BackupDownloadLogAdmin(admin.ModelAdmin):
    list_display  = ('user', 'artifact', 'downloaded_at', 'success',
                     'ip_address', 'bytes_sent')
    list_filter   = ('success',)
    search_fields = ('user__username', 'ip_address', 'request_id')
    readonly_fields = tuple(f.name for f in BackupDownloadLog._meta.fields)
    ordering      = ('-downloaded_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
