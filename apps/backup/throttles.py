"""
Throttles dedies au module backup.

  - BackupDownloadThrottle : limite anti-exfiltration de masse
  - ManualBackupThrottle   : limite la generation manuelle (operation lourde)
"""
from rest_framework.throttling import UserRateThrottle


class BackupDownloadThrottle(UserRateThrottle):
    """10 telechargements par heure max. Bypass pour superuser."""
    scope = 'backup_download'
    rate  = '10/hour'

    def allow_request(self, request, view):
        if request.user.is_authenticated and request.user.is_superuser:
            return True
        return super().allow_request(request, view)


class ManualBackupThrottle(UserRateThrottle):
    """5 generations manuelles par heure max. Operation lourde (CPU + I/O)."""
    scope = 'backup_manual'
    rate  = '5/hour'

    def allow_request(self, request, view):
        if request.user.is_authenticated and request.user.is_superuser:
            return True
        return super().allow_request(request, view)
