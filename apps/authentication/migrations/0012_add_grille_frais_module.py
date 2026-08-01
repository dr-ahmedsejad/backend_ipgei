"""
Data migration — Module RBAC dédié 'insc_grille_frais' (grille tarifaire).

La grille tarifaire des frais d'inscription est une configuration sensible (prix).
On lui donne son propre module RBAC afin qu'elle soit :
  - accessible à l'admin (bypass RBACPermission),
  - attribuable finement à d'autres rôles via la matrice de permissions,
  - PAR DÉFAUT réservée à l'admin (aucun RoleDefault posé ici → seuls les
    utilisateurs explicitement autorisés y accèdent).

Idempotent via get_or_create.
"""
from django.db import migrations


# (code, nom, icone, ordre)
NEW_MODULE = ('insc_grille_frais', 'Inscriptions : grille tarifaire', 'Coins', 80)
DEFAULT_ACTIONS = ['voir', 'modifier', 'supprimer', 'exporter']


def add_module(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return  # base de test : baseline vide identique a --no-migrations

    Module       = apps.get_model('authentication', 'Module')
    Action       = apps.get_model('authentication', 'Action')
    ModuleAction = apps.get_model('authentication', 'ModuleAction')

    for code, nom in [('voir', 'Voir'), ('modifier', 'Modifier'),
                      ('supprimer', 'Supprimer'), ('exporter', 'Exporter')]:
        Action.objects.get_or_create(code=code, defaults={'nom': nom})

    code, nom, icone, ordre = NEW_MODULE
    mod, _ = Module.objects.get_or_create(
        code=code, defaults={'nom': nom, 'icone': icone, 'ordre': ordre},
    )
    for action_code in DEFAULT_ACTIONS:
        try:
            act = Action.objects.get(code=action_code)
        except Action.DoesNotExist:
            continue
        ModuleAction.objects.get_or_create(module=mod, action=act)

    print("[migration 0012] Module 'insc_grille_frais' pret (admin + attribuable RBAC).")


def remove_module(apps, schema_editor):
    Module = apps.get_model('authentication', 'Module')
    Module.objects.filter(code=NEW_MODULE[0]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0011_add_managed_departements'),
    ]

    operations = [
        migrations.RunPython(add_module, remove_module),
    ]
