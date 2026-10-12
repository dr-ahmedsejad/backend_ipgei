"""
Data migration — « ipgei_sous_groupes », détaché de « ipgei_classes ».

Les sous-groupes de TP se géraient sous le module des classes : qui pouvait
toucher aux classes pouvait composer les sous-groupes, et réciproquement. Un
chef de département peut avoir à répartir ses TP sans avoir à créer ou
supprimer des classes — d'où la séparation.

**Les droits existants sont RECOPIÉS**, et c'est tout l'enjeu de cette
migration. Créer le module sans rien accorder reviendrait à retirer d'un coup
la gestion des sous-groupes à tous ceux qui l'avaient — un 403 pour tout le
monde sauf l'administrateur, sans que personne n'ait rien demandé. On part donc
de l'état exact de `ipgei_classes`, défauts de rôle et permissions nominatives
comprises. Séparer, ce n'est pas reprendre : les deux droits divergeront
ensuite, à la main, quand quelqu'un le décidera.

Idempotent via `get_or_create`.
"""
from django.db import migrations

# (code, nom, icone, ordre) — juste après les classes, dont il est issu.
NOUVEAU_MODULE = ('ipgei_sous_groupes', 'IPGEI : sous-groupes de TP',
                  'Users', 201)
MODULE_SOURCE = 'ipgei_classes'
ACTIONS = [('voir', 'Voir'), ('modifier', 'Modifier'),
           ('supprimer', 'Supprimer'), ('exporter', 'Exporter')]


def detacher_le_module(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return  # base de test : baseline vide, identique à --no-migrations

    Module         = apps.get_model('authentication', 'Module')
    Action         = apps.get_model('authentication', 'Action')
    ModuleAction   = apps.get_model('authentication', 'ModuleAction')
    RoleDefault    = apps.get_model('authentication', 'RoleDefault')
    UserPermission = apps.get_model('authentication', 'UserPermission')

    for code, nom in ACTIONS:
        Action.objects.get_or_create(code=code, defaults={'nom': nom})

    code, nom, icone, ordre = NOUVEAU_MODULE
    module, _ = Module.objects.get_or_create(
        code=code, defaults={'nom': nom, 'icone': icone, 'ordre': ordre},
    )

    # Les paires (module, action) du nouveau module, indexées par code d'action.
    paires = {}
    for action_code, _nom in ACTIONS:
        try:
            action = Action.objects.get(code=action_code)
        except Action.DoesNotExist:
            continue
        paire, _ = ModuleAction.objects.get_or_create(module=module, action=action)
        paires[action_code] = paire

    source = Module.objects.filter(code=MODULE_SOURCE).first()
    if source is None:
        print("[migration 0047] Module 'ipgei_sous_groupes' cree ; "
              "'ipgei_classes' absent, aucun droit a recopier.")
        return

    defauts = nominatives = 0
    for ancienne in ModuleAction.objects.filter(module=source).select_related('action'):
        nouvelle = paires.get(ancienne.action.code)
        if nouvelle is None:
            continue
        for rd in RoleDefault.objects.filter(module_action=ancienne):
            _, cree = RoleDefault.objects.get_or_create(
                role=rd.role, module_action=nouvelle,
                defaults={'allowed': rd.allowed},
            )
            defauts += int(cree)
        for up in UserPermission.objects.filter(module_action=ancienne):
            _, cree = UserPermission.objects.get_or_create(
                user_id=up.user_id, module_action=nouvelle,
                defaults={'allowed': up.allowed, 'departement_id': up.departement_id,
                          'filiere_id': up.filiere_id},
            )
            nominatives += int(cree)

    print(f"[migration 0047] Module 'ipgei_sous_groupes' detache de "
          f"'ipgei_classes' : {defauts} defaut(s) de role et {nominatives} "
          f"permission(s) nominative(s) recopies. Personne ne perd d'acces.")


def rattacher_au_module_des_classes(apps, schema_editor):
    """Marche arrière : le module disparaît, ses droits avec lui.

    Les permissions recopiées partent par cascade — elles n'étaient qu'un
    reflet de celles des classes, qui, elles, restent en place.
    """
    Module = apps.get_model('authentication', 'Module')
    Module.objects.filter(code=NOUVEAU_MODULE[0]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('ipgei', '0046_decalage_de_rentree_du_semestre'),
        ('authentication', '0024_module_banques'),
    ]

    operations = [
        migrations.RunPython(detacher_le_module, rattacher_au_module_des_classes),
    ]
