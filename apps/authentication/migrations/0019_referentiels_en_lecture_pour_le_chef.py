"""
Data migration — le chef de departement lit les referentiels dont ses ecrans
ont besoin, et les modules `salles` et `profs` sont enfin enregistres.

Ses ecrans reclament des listes qu'il n'avait pas le droit de lire : les SALLES
(occupation des salles, fiche d'emargement), les ENSEIGNANTS (emploi par
enseignant, charge de sa discipline) et les REFERENTIELS IPGEI — niveaux,
semestres, semaines — dont dependent tous les selecteurs. Sans elles, ses pages
s'ouvrent sur des menus vides sans dire pourquoi.

`salles` et `profs` etaient dans le meme etat que `avancement` et
`reclamations` : declares dans le code, jamais poses dans la table `Module`,
donc fermes a TOUS sauf l'admin — y compris a la direction, que le menu vise
explicitement (groupe « Enseignants », roles MANAGE). On les enregistre et on
accorde ce que le menu annonce.

Tout est en LECTURE pour le chef : il consulte ces listes, il ne les tient pas.
La modification reste a la direction et a la scolarite — et peut lui etre donnee
nominativement via Comptes -> Permissions.

`allowed=True` est pose EXPLICITEMENT : le defaut du modele est False, et
l'oublier cree des droits qui refusent (c'etait le bug de la migration 0014).
"""
from django.db import migrations

MODULES = [
    ('salles', 'Salles',      'DoorOpen', 33),
    ('profs',  'Enseignants', 'Users',    34),
]
ACTIONS = [('voir', 'Voir'), ('modifier', 'Modifier'),
           ('supprimer', 'Supprimer'), ('exporter', 'Exporter')]

DROITS = {
    # Le chef et le coordinateur : lecture seule des referentiels.
    'chef_departement': {'salles': ['voir'], 'profs': ['voir'],
                         'ipgei_parametres': ['voir']},
    # Ce que le menu annonce deja pour ces deux modules (groupe « Enseignants »
    # en MANAGE, salles consultees par les ecrans d'emploi du temps).
    'DG':        {'salles': ['voir', 'modifier'], 'profs': ['voir', 'modifier']},
    'DA':        {'salles': ['voir', 'modifier'], 'profs': ['voir', 'modifier']},
    'DE':        {'salles': ['voir', 'modifier'], 'profs': ['voir', 'modifier']},
    'scolarite': {'salles': ['voir'],             'profs': ['voir']},
}


def poser(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return

    Module       = apps.get_model('authentication', 'Module')
    Action       = apps.get_model('authentication', 'Action')
    ModuleAction = apps.get_model('authentication', 'ModuleAction')
    RoleDefault  = apps.get_model('authentication', 'RoleDefault')

    for code, nom in ACTIONS:
        Action.objects.get_or_create(code=code, defaults={'nom': nom})

    for code, nom, icone, ordre in MODULES:
        module, _ = Module.objects.get_or_create(
            code=code, defaults={'nom': nom, 'icone': icone, 'ordre': ordre})
        for code_action, _ in ACTIONS:
            act = Action.objects.filter(code=code_action).first()
            if act:
                ModuleAction.objects.get_or_create(module=module, action=act)

    poses = 0
    for role, modules in DROITS.items():
        for code_module, actions in modules.items():
            for code_action in actions:
                ma = (ModuleAction.objects
                      .filter(module__code=code_module, action__code=code_action).first())
                if ma is None:
                    continue
                _, cree = RoleDefault.objects.get_or_create(
                    role=role, module_action=ma, defaults={'allowed': True})
                if cree:
                    poses += 1
                else:
                    RoleDefault.objects.filter(role=role, module_action=ma,
                                               allowed=False).update(allowed=True)
    print(f"[migration 0019] modules 'salles' et 'profs' poses, {poses} droits accordes.")


def retirer(apps, schema_editor):
    Module      = apps.get_model('authentication', 'Module')
    RoleDefault = apps.get_model('authentication', 'RoleDefault')
    RoleDefault.objects.filter(role='chef_departement',
                               module_action__module__code='ipgei_parametres').delete()
    codes = [c for c, _, _, _ in MODULES]
    RoleDefault.objects.filter(module_action__module__code__in=codes).delete()
    Module.objects.filter(code__in=codes).delete()


class Migration(migrations.Migration):
    dependencies = [('authentication', '0018_edt_modifier_hors_defaut_du_chef')]
    operations = [migrations.RunPython(poser, retirer)]
