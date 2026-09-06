"""
Data migration — droits par defaut du role 'chef_departement'.

Le chef de departement (ST, Langues) et le coordinateur de matiere repondent
d'une DISCIPLINE : la maquette de leurs matieres, leur ponderation, le niveau
atteint par les etudiants. Ils ne tiennent PAS l'emploi du temps, qui reste a la
scolarite — d'ou l'absence de `ipgei_edt` ici, demandee explicitement.

Portee : le RBAC est par MODULE, non par discipline. Ce role donne donc acces a
toutes les matieres de l'institut, non aux seules siennes — `Matiere` ne porte
aucun champ de discipline, et la delegation `managed_departements` ne vaut que
pour l'emploi du temps. Restreindre supposerait d'ajouter cette notion au
modele ; c'est une decision qui n'appartient pas a une migration.

Idempotent via get_or_create.
"""
from django.db import migrations

ROLE = 'chef_departement'

# (module, actions) — 'modifier' n'est pose que la ou il repond vraiment.
DROITS = [
    ('ipgei_matieres',     ['voir', 'modifier', 'exporter']),
    ('ipgei_notes',        ['voir', 'exporter']),
    ('ipgei_deliberation', ['voir']),
    ('ipgei_classes',      ['voir']),
    ('suivi_charges',      ['voir']),
]


def poser(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return  # base de test : baseline vide identique a --no-migrations

    ModuleAction = apps.get_model('authentication', 'ModuleAction')
    RoleDefault  = apps.get_model('authentication', 'RoleDefault')

    poses, absents = 0, []
    for code_module, actions in DROITS:
        for code_action in actions:
            ma = (ModuleAction.objects
                  .filter(module__code=code_module, action__code=code_action)
                  .first())
            if ma is None:
                # Le module n'existe pas dans cette base : on le signale plutot
                # que de creer un droit qui n'ouvrirait rien.
                absents.append(f'{code_module}:{code_action}')
                continue
            _, cree = RoleDefault.objects.get_or_create(role=ROLE, module_action=ma)
            poses += cree

    print(f"[migration 0014] role '{ROLE}' : {poses} droits poses.")
    if absents:
        print(f"[migration 0014] modules absents, ignores : {', '.join(absents)}")


def retirer(apps, schema_editor):
    apps.get_model('authentication', 'RoleDefault').objects.filter(role=ROLE).delete()


class Migration(migrations.Migration):
    dependencies = [('authentication', '0013_role_chef_departement')]
    operations = [migrations.RunPython(poser, retirer)]
