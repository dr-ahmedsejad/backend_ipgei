"""
Data migration — droit d'ÉCRIRE des présences (`abs_saisie` / `modifier`)
pour le DA.

À l'IPGEI, enregistrer un appel exige `abs_saisie` / `modifier` (une action
POST demande `modifier`). Mesuré sur `IPGEI` le 17/10/2026 : le DE et la
scolarité l'ont ; le DA n'a que `voir` — il consultait la grille et la
surveillance mais ne pouvait pas enregistrer un appel. L'utilisateur a décidé
de le lui donner, comme à l'ISS. Le DG, qui consulte, n'est PAS concerné.

AJOUTE un droit par défaut ; ne modifie ni ne supprime aucune ligne
existante — `get_or_create` : un refus déjà posé dans la matrice des
permissions reste un refus. Idempotent. Le retour arrière ne retire rien :
on ne sait plus, alors, si la ligne existait avant.

Le cache RBAC (5 min, versionné) n'est pas invalidé ici, comme pour les
autres migrations de droits : le nouveau droit s'applique au plus tard cinq
minutes après la migration.
"""
from django.db import migrations

MODULE = ('abs_saisie', 'Absences : saisie', 'UserX', 68)
ACTION = ('modifier', 'Modifier')
ROLES = ['DA']


def ajouter(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return  # base de test : baseline vide identique a --no-migrations

    Module = apps.get_model('authentication', 'Module')
    Action = apps.get_model('authentication', 'Action')
    ModuleAction = apps.get_model('authentication', 'ModuleAction')
    RoleDefault = apps.get_model('authentication', 'RoleDefault')

    code, nom, icone, ordre = MODULE
    mod, _ = Module.objects.get_or_create(
        code=code, defaults={'nom': nom, 'icone': icone, 'ordre': ordre})
    act, _ = Action.objects.get_or_create(code=ACTION[0], defaults={'nom': ACTION[1]})
    ma, _ = ModuleAction.objects.get_or_create(module=mod, action=act)
    for role in ROLES:
        RoleDefault.objects.get_or_create(
            role=role, module_action=ma, defaults={'allowed': True})


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0026_module_surveillance'),
    ]

    operations = [
        migrations.RunPython(ajouter, migrations.RunPython.noop),
    ]
