"""
Data migration — le chef de departement gere l'emploi du temps et les
reclamations de ses matieres ; le module 'reclamations' est enregistre.

Deux choses que la migration 0014 n'ouvrait pas, parce qu'elles auraient donne
acces a TOUT l'etablissement. Le perimetre disciplinaire (`ipgei.perimetre`)
existe desormais : l'EDT et les reclamations y sont filtres par matiere, et ces
droits deviennent surs — le chef de ST ne deplace qu'un cours de ST.

Le module 'reclamations' est dans le meme cas que 'avancement' (migration 0015) :
present dans le code et le menu, jamais enregistre — donc invisible a tous sauf
l'admin. On le pose, et on accorde ce que le menu annonce (groupe SCOLARITE :
DE, scolarite, responsable_filiere), plus le chef de departement.

Idempotente via get_or_create.
"""
from django.db import migrations

MODULE_RECLAMATIONS = ('reclamations', 'Réclamations', 'AlertCircle', 32)
ACTIONS = [('voir', 'Voir'), ('modifier', 'Modifier'),
           ('supprimer', 'Supprimer'), ('exporter', 'Exporter')]

DROITS = {
    'chef_departement':    {'ipgei_edt': ['voir', 'modifier'],
                            'reclamations': ['voir', 'modifier']},
    'DE':                  {'reclamations': ['voir', 'modifier']},
    'scolarite':           {'reclamations': ['voir', 'modifier']},
    'responsable_filiere': {'reclamations': ['voir', 'modifier']},
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

    code, nom, icone, ordre = MODULE_RECLAMATIONS
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
                _, cree = RoleDefault.objects.get_or_create(role=role, module_action=ma)
                poses += cree
    print(f"[migration 0016] module 'reclamations' pose, {poses} droits accordes.")


def retirer(apps, schema_editor):
    RoleDefault = apps.get_model('authentication', 'RoleDefault')
    RoleDefault.objects.filter(role='chef_departement',
                               module_action__module__code__in=['ipgei_edt', 'reclamations']).delete()
    RoleDefault.objects.filter(module_action__module__code='reclamations').delete()
    apps.get_model('authentication', 'Module').objects.filter(code='reclamations').delete()


class Migration(migrations.Migration):
    dependencies = [('authentication', '0015_modules_avancement_statistiques')]
    operations = [migrations.RunPython(poser, retirer)]
