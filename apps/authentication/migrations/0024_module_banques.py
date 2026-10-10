"""
Data migration — le module RBAC « banques », qui manquait au catalogue.

`BanqueViewSet` déclare `required_module = 'banques'` depuis toujours, mais
aucune migration n'avait créé le module correspondant. Les conséquences se
voyaient à deux endroits, sans qu'aucun message ne les relie :

  - côté API, `_compute_access` rend `False` pour un module inconnu : seuls
    les administrateurs pouvaient lire ou modifier les banques ;
  - côté écran « Permissions utilisateurs », la ligne « Banques » s'affichait
    — l'arbre est construit depuis le MENU — mais le clic ne faisait rien :
    `handleToggle` sort en silence quand la paire (module, action) n'existe
    pas en base.

Cette migration ne DONNE aucun droit : aucun `RoleDefault` n'est posé. Elle
rend seulement l'interrupteur utilisable, pour qu'un administrateur puisse
attribuer l'accès depuis l'écran. Personne ne gagne ni ne perd quoi que ce
soit tant que la case n'est pas cochée.

Idempotent via `get_or_create`.
"""
from django.db import migrations

# (code, nom, icone, ordre) — l'ordre place le module dans la section Finances,
# juste après la paie des vacations.
NOUVEAU_MODULE = ('banques', 'Banques', 'Landmark', 85)

# Les quatre actions du catalogue. `supprimer` y figure parce que l'écran le
# permet : un module sans cette action rendrait le bouton inopérant comme
# l'était la ligne elle-même.
ACTIONS = [('voir', 'Voir'), ('modifier', 'Modifier'),
           ('supprimer', 'Supprimer'), ('exporter', 'Exporter')]


def creer_le_module(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return  # base de test : baseline vide, identique à --no-migrations

    Module       = apps.get_model('authentication', 'Module')
    Action       = apps.get_model('authentication', 'Action')
    ModuleAction = apps.get_model('authentication', 'ModuleAction')

    for code, nom in ACTIONS:
        Action.objects.get_or_create(code=code, defaults={'nom': nom})

    code, nom, icone, ordre = NOUVEAU_MODULE
    module, _ = Module.objects.get_or_create(
        code=code, defaults={'nom': nom, 'icone': icone, 'ordre': ordre},
    )
    for action_code, _nom in ACTIONS:
        try:
            action = Action.objects.get(code=action_code)
        except Action.DoesNotExist:
            continue
        ModuleAction.objects.get_or_create(module=module, action=action)

    print("[migration 0024] Module 'banques' pret — attribuable depuis l'ecran "
          "Permissions. Aucun droit accorde par defaut.")


def retirer_le_module(apps, schema_editor):
    """Marche arrière.

    Les `UserPermission` qui pointeraient vers ces paires partent avec elles —
    c'est la cascade voulue : sans module, elles ne désignent plus rien.
    """
    Module = apps.get_model('authentication', 'Module')
    Module.objects.filter(code=NOUVEAU_MODULE[0]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0023_edt_et_suivi_complets_pour_le_de'),
    ]

    operations = [
        migrations.RunPython(creer_le_module, retirer_le_module),
    ]
