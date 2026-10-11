"""
Data migration — Module RBAC « annonces » (envoyer un message aux étudiants).

AJOUTE des lignes RBAC (module, ses actions, et les droits par défaut de la
direction de l'enseignement et de la scolarité) ; ne modifie ni ne supprime
aucune ligne existante. L'admin y accède toujours (bypass RBACPermission) ;
d'autres rôles s'y ajoutent depuis la matrice des permissions.

Idempotent via get_or_create.
"""
from django.db import migrations

MODULE = ('annonces', 'Annonces aux étudiants', 'Megaphone', 85)
ACTIONS = ['voir', 'modifier']
ROLES_PAR_DEFAUT = ['DE', 'scolarite']


def ajouter(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return  # base de test : baseline vide identique a --no-migrations

    Module = apps.get_model('authentication', 'Module')
    Action = apps.get_model('authentication', 'Action')
    ModuleAction = apps.get_model('authentication', 'ModuleAction')
    RoleDefault = apps.get_model('authentication', 'RoleDefault')

    for code, nom in [('voir', 'Voir'), ('modifier', 'Modifier')]:
        Action.objects.get_or_create(code=code, defaults={'nom': nom})
    code, nom, icone, ordre = MODULE
    mod, _ = Module.objects.get_or_create(
        code=code, defaults={'nom': nom, 'icone': icone, 'ordre': ordre})
    for action_code in ACTIONS:
        ma, _ = ModuleAction.objects.get_or_create(
            module=mod, action=Action.objects.get(code=action_code))
        for role in ROLES_PAR_DEFAUT:
            RoleDefault.objects.get_or_create(
                role=role, module_action=ma, defaults={'allowed': True})


def retirer(apps, schema_editor):
    Module = apps.get_model('authentication', 'Module')
    Module.objects.filter(code=MODULE[0]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0024_module_banques'),
    ]

    operations = [
        migrations.RunPython(ajouter, retirer),
    ]
