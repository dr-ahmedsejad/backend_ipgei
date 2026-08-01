from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    """
    Admin basé sur UserAdmin → gère correctement le mot de passe :
    - le hash n'est jamais éditable en clair ;
    - un lien « changer le mot de passe » (formulaire dédié, hachage propre)
      est disponible sur la fiche utilisateur → réinitialisation du mdp.
    """
    list_display  = ('username', 'email', 'name', 'role', 'is_active', 'is_staff', 'doit_changer_mdp')
    list_filter   = ('role', 'is_active', 'is_staff', 'is_superuser')
    search_fields = ('username', 'email', 'name', 'first_name', 'last_name')
    ordering      = ('username',)
    filter_horizontal = ('groups', 'user_permissions', 'managed_departements')

    # Fiche existante : on ajoute les champs SIGA à la structure standard UserAdmin
    # (qui contient déjà le champ mot de passe + lien de changement).
    fieldsets = UserAdmin.fieldsets + (
        ('SIGA', {'fields': ('role', 'name', 'avatar', 'doit_changer_mdp', 'managed_departements')}),
    )
    # Création : username + password1/password2 (UserAdmin) + email/rôle obligatoires.
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('SIGA', {'fields': ('email', 'role', 'name')}),
    )
