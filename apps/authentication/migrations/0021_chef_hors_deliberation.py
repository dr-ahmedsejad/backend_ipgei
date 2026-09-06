"""
Data migration — le chef de departement et le coordinateur sortent du jury.

`ipgei_deliberation:voir` leur avait ete accorde par la migration 0014, au motif
qu'un chef suit le detail de ses matieres en jury. La direction en a decide
autrement : la deliberation est l'affaire du jury et de la scolarite, pas du
suivi disciplinaire.

Le module gouverne TROIS entrees de menu — « Deliberations », « Classement S3 »
et « Resultat du concours (CNIM) ». Les trois disparaissent donc ensemble ; il
n'y a pas moyen de n'en retirer qu'une sans leur donner des modules distincts.

On SUPPRIME la ligne plutot que de la passer a False : un refus explicite se
lirait comme une decision de refus, alors qu'il n'y a simplement plus de defaut.
Un chef qui devrait sieger peut recevoir le droit nominativement, via
Comptes -> Permissions.
"""
from django.db import migrations


def retirer(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return

    supprimees = (apps.get_model('authentication', 'RoleDefault').objects
                  .filter(role='chef_departement',
                          module_action__module__code='ipgei_deliberation')
                  .delete())[0]
    print(f'[migration 0021] droits de deliberation retires au chef : {supprimees}')


def remettre(apps, schema_editor):
    ModuleAction = apps.get_model('authentication', 'ModuleAction')
    RoleDefault  = apps.get_model('authentication', 'RoleDefault')
    ma = ModuleAction.objects.filter(module__code='ipgei_deliberation',
                                     action__code='voir').first()
    if ma:
        RoleDefault.objects.get_or_create(role='chef_departement', module_action=ma,
                                          defaults={'allowed': True})


class Migration(migrations.Migration):
    dependencies = [('authentication', '0020_module_sessions_de_saisie')]
    operations = [migrations.RunPython(retirer, remettre)]
