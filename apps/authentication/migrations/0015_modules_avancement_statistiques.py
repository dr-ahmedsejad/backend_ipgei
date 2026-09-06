"""
Data migration — enregistre les modules 'avancement' et 'statistiques'.

Les deux existent dans le CODE (`required_module` sur les vues de
`apps/avancement`) et dans le MENU (`lib/nav-config.ts`, groupes « Avancement »
et « Statistiques », declares pour MANAGE). Mais ils n'avaient jamais ete
enregistres dans la table `Module` : `canAccess` ne trouvait aucun droit, et les
deux entrees restaient invisibles pour TOUS sauf l'admin — le directeur compris,
alors que le menu les lui destine.

On pose donc les modules, puis les droits que le menu annonce deja :
  - DG, DA, DE : voir et exporter, sur les deux — c'est ce que MANAGE veut dire ;
  - chef_departement : 'avancement' seulement, en lecture. Il suit l'avancement
    des enseignements de sa discipline ; les statistiques de l'etablissement ne
    sont pas son objet.

Idempotent via get_or_create.
"""
from django.db import migrations

MODULES = [
    ('avancement',   'Avancement des enseignements', 'TrendingUp', 30),
    ('statistiques', 'Statistiques',                 'BarChart2',  31),
]
ACTIONS = [('voir', 'Voir'), ('modifier', 'Modifier'),
           ('supprimer', 'Supprimer'), ('exporter', 'Exporter')]

# role -> modules ouverts en lecture/export
DROITS = {
    'DG': ['avancement', 'statistiques'],
    'DA': ['avancement', 'statistiques'],
    'DE': ['avancement', 'statistiques'],
    'chef_departement': ['avancement'],
}


def poser(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return  # base de test : baseline vide identique a --no-migrations

    Module       = apps.get_model('authentication', 'Module')
    Action       = apps.get_model('authentication', 'Action')
    ModuleAction = apps.get_model('authentication', 'ModuleAction')
    RoleDefault  = apps.get_model('authentication', 'RoleDefault')

    for code, nom in ACTIONS:
        Action.objects.get_or_create(code=code, defaults={'nom': nom})

    for code, nom, icone, ordre in MODULES:
        mod, _ = Module.objects.get_or_create(
            code=code, defaults={'nom': nom, 'icone': icone, 'ordre': ordre})
        for code_action, _ in ACTIONS:
            act = Action.objects.filter(code=code_action).first()
            if act:
                ModuleAction.objects.get_or_create(module=mod, action=act)

    poses = 0
    for role, codes in DROITS.items():
        for code in codes:
            for code_action in ('voir', 'exporter'):
                ma = (ModuleAction.objects
                      .filter(module__code=code, action__code=code_action).first())
                if ma is None:
                    continue
                _, cree = RoleDefault.objects.get_or_create(role=role, module_action=ma)
                poses += cree

    print(f"[migration 0015] modules 'avancement' et 'statistiques' poses, "
          f"{poses} droits accordes.")


def retirer(apps, schema_editor):
    Module      = apps.get_model('authentication', 'Module')
    RoleDefault = apps.get_model('authentication', 'RoleDefault')
    codes = [c for c, _, _, _ in MODULES]
    RoleDefault.objects.filter(module_action__module__code__in=codes).delete()
    Module.objects.filter(code__in=codes).delete()


class Migration(migrations.Migration):
    dependencies = [('authentication', '0014_droits_chef_departement')]
    operations = [migrations.RunPython(poser, retirer)]
