"""
Permissions specifiques au module backup.

  - CanDownloadBackup : admin OU presence d'un BackupDownloadGrant pour le user
  - IsBackupGrantManager : admin / superuser uniquement (gere la matrice)
"""
from rest_framework.permissions import BasePermission

from .models import BackupDownloadGrant


def user_can_download(user) -> bool:
    """Verite metier reutilisable hors-permission (serializer, helper)."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.role == 'admin':
        return True
    return BackupDownloadGrant.objects.filter(user=user).exists()


class CanDownloadBackup(BasePermission):
    """
    Acces au listing et au telechargement des sauvegardes.
    Admin = bypass (filet de securite, doit toujours pouvoir intervenir).
    Sinon : presence d'un BackupDownloadGrant.
    """
    message = "Vous n'etes pas autorise a acceder aux sauvegardes."

    def has_permission(self, request, view):
        return user_can_download(request.user)


class IsBackupGrantManager(BasePermission):
    """
    Gestion de la matrice d'autorisation : strictement admin/superuser.
    Pas de delegation possible — eviter qu'un user "moitie autorise" puisse
    s'auto-ajouter ou ajouter ses copains.
    """
    message = "Seul un administrateur peut gerer les autorisations de telechargement."

    def has_permission(self, request, view):
        u = request.user
        return bool(
            u and u.is_authenticated
            and (u.is_superuser or u.role == 'admin')
        )
