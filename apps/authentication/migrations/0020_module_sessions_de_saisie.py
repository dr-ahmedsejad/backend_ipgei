"""
Data migration — les sessions de saisie prennent leur propre module.

Ouvrir, clore ou rouvrir une campagne de saisie engage TOUT l'etablissement :
c'est un acte de scolarite. Il vivait pourtant sous `ipgei_notes`, le meme
module que la consultation des notes — si bien qu'un chef de departement,
autorise a lire les notes de sa discipline, atteignait l'ecran « Sessions de
saisie » et pouvait decider quand l'institut entier saisit les siennes.

`ipgei_sessions` recoit donc les roles qui tenaient deja `ipgei_notes:modifier`
— la scolarite et la direction des etudes —, et eux seuls. Le chef de
departement garde sa lecture des notes ; il perd cet ecran, qui n'a jamais ete
le sien.

`allowed=True` pose explicitement : le defaut du modele est False.
"""
from django.db import migrations

MODULE = ('ipgei_sessions', 'IPGEI : sessions de saisie', 'CalendarClock', 206)
ACTIONS = [('voir', 'Voir'), ('modifier', 'Modifier'),
           ('supprimer', 'Supprimer'), ('exporter', 'Exporter')]
#: Ceux qui detenaient `ipgei_notes:modifier` avant cette migration.
ROLES = {'DE': ['voir', 'modifier'], 'scolarite': ['voir', 'modifier']}


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

    code, nom, icone, ordre = MODULE
    module, _ = Module.objects.get_or_create(
        code=code, defaults={'nom': nom, 'icone': icone, 'ordre': ordre})
    for code_action, _ in ACTIONS:
        act = Action.objects.filter(code=code_action).first()
        if act:
            ModuleAction.objects.get_or_create(module=module, action=act)

    poses = 0
    for role, actions in ROLES.items():
        for code_action in actions:
            ma = ModuleAction.objects.filter(module=module,
                                             action__code=code_action).first()
            if ma is None:
                continue
            _, cree = RoleDefault.objects.get_or_create(
                role=role, module_action=ma, defaults={'allowed': True})
            if cree:
                poses += 1
            else:
                RoleDefault.objects.filter(role=role, module_action=ma,
                                           allowed=False).update(allowed=True)
    print(f"[migration 0020] module 'ipgei_sessions' pose, {poses} droits accordes.")


def retirer(apps, schema_editor):
    Module = apps.get_model('authentication', 'Module')
    apps.get_model('authentication', 'RoleDefault').objects.filter(
        module_action__module__code='ipgei_sessions').delete()
    Module.objects.filter(code='ipgei_sessions').delete()


class Migration(migrations.Migration):
    dependencies = [('authentication', '0019_referentiels_en_lecture_pour_le_chef')]
    operations = [migrations.RunPython(poser, retirer)]
