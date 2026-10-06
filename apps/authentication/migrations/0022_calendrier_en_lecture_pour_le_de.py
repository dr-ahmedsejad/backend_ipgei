"""
Data migration — le Directeur des etudes lit le calendrier.

« Gerer les emplois » charge d'abord les SEMESTRES, puis les SEMAINES de ce
semestre : deux adresses protegees par le module `ipgei_parametres`. Le DE n'y
avait aucun droit, alors qu'il a `ipgei_edt` en MODIFICATION. Son ecran
repondait donc 403 sur les deux listes, sans message : les selecteurs restaient
vides, et il ne pouvait poser aucune seance.

On lui accorde la LECTURE, comme au chef de departement (migration 0019). Il
consulte le calendrier, il ne le tient pas : qualifier une semaine ou clore un
semestre reste a la scolarite et a l'administrateur — et peut lui etre donne
nominativement via Comptes -> Permissions.

`allowed=True` est pose EXPLICITEMENT : le defaut du modele est False, et
l'oublier cree des droits qui refusent (c'etait le bug de la migration 0014).
"""
from django.db import migrations

ROLE = 'DE'
MODULE = 'ipgei_parametres'
ACTIONS = ['voir']


def poser(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return

    ModuleAction = apps.get_model('authentication', 'ModuleAction')
    RoleDefault  = apps.get_model('authentication', 'RoleDefault')

    poses = 0
    for code_action in ACTIONS:
        ma = (ModuleAction.objects
              .filter(module__code=MODULE, action__code=code_action).first())
        if ma is None:
            continue
        _, cree = RoleDefault.objects.get_or_create(
            role=ROLE, module_action=ma, defaults={'allowed': True})
        if cree:
            poses += 1
        else:
            RoleDefault.objects.filter(role=ROLE, module_action=ma,
                                       allowed=False).update(allowed=True)
    print(f'[migration 0022] {poses} droit(s) de lecture du calendrier accorde(s) au {ROLE}.')


def retirer(apps, schema_editor):
    RoleDefault = apps.get_model('authentication', 'RoleDefault')
    RoleDefault.objects.filter(role=ROLE,
                               module_action__module__code=MODULE,
                               module_action__action__code__in=ACTIONS).delete()


class Migration(migrations.Migration):

    dependencies = [('authentication', '0021_chef_hors_deliberation')]

    operations = [migrations.RunPython(poser, retirer)]
