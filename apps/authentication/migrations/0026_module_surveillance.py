"""
Data migration — Module RBAC « surveillance » (la vue de surveillance de
l'emploi du temps : toutes les classes et leurs sous-groupes de TP sur une
journée, /dashboard/ipgei/edt/surveillance).

Attribué par défaut au DA et à l'AA, comme à l'ISS. L'admin y accède toujours
(bypass RBACPermission) ; d'autres rôles s'y ajoutent depuis la matrice des
permissions.

AJOUTE des lignes RBAC (module, action, droits par défaut) ; ne modifie ni ne
supprime aucune ligne existante. Idempotent via get_or_create.
"""
from django.db import migrations

MODULE = ('surveillance', 'Vue de surveillance', 'Eye', 86)
ACTIONS = ['voir']
ROLES_PAR_DEFAUT = ['DA', 'AA']


def ajouter(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return  # base de test : baseline vide identique a --no-migrations

    Module = apps.get_model('authentication', 'Module')
    Action = apps.get_model('authentication', 'Action')
    ModuleAction = apps.get_model('authentication', 'ModuleAction')
    RoleDefault = apps.get_model('authentication', 'RoleDefault')

    Action.objects.get_or_create(code='voir', defaults={'nom': 'Voir'})
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
        ('authentication', '0025_module_annonces'),
    ]

    operations = [
        migrations.RunPython(ajouter, retirer),
    ]
