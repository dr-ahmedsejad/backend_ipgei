"""
Data migration — l'ecriture de l'emploi du temps sort des droits par defaut du
chef de departement.

Elle y etait entree avec la migration 0016. C'etait trop : un chef consulte
l'emploi du temps de ses enseignants, il ne le construit pas — et un
coordinateur de matiere encore moins. L'entree « Gerer les emplois » apparaissait
pourtant a l'un comme a l'autre.

`ipgei_edt:voir` RESTE : c'est la consultation, et elle est legitime pour tous.
Seul `modifier` s'en va du defaut, et devient une autorisation NOMINATIVE :
l'admin la donne a tel chef depuis Comptes → Permissions, et `UserPermission`
prime sur le defaut du role (`core.permissions._compute_access` la lit en
premier). Le geste existe deja, on ne construit rien pour cela.

On SUPPRIME la ligne plutot que de la mettre a False : un refus explicite se
lirait comme une decision prise, alors qu'il n'y a simplement pas de defaut.
"""
from django.db import migrations


def retirer_du_defaut(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return

    supprimees = (apps.get_model('authentication', 'RoleDefault').objects
                  .filter(role='chef_departement',
                          module_action__module__code='ipgei_edt',
                          module_action__action__code='modifier')
                  .delete())[0]
    print(f'[migration 0018] ipgei_edt:modifier retire du defaut du chef : {supprimees}')


def remettre(apps, schema_editor):
    ModuleAction = apps.get_model('authentication', 'ModuleAction')
    RoleDefault  = apps.get_model('authentication', 'RoleDefault')
    ma = ModuleAction.objects.filter(module__code='ipgei_edt',
                                     action__code='modifier').first()
    if ma:
        RoleDefault.objects.get_or_create(role='chef_departement', module_action=ma,
                                          defaults={'allowed': True})


class Migration(migrations.Migration):
    dependencies = [('authentication', '0017_corrige_allowed_des_droits_poses')]
    operations = [migrations.RunPython(retirer_du_defaut, remettre)]
