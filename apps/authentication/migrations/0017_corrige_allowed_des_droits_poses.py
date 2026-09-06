"""
Data migration — repare les droits poses par 0014, 0015 et 0016.

`RoleDefault.allowed` vaut **False par defaut**, et ces trois migrations
creaient leurs lignes par `get_or_create(role=..., module_action=...)` sans
jamais poser le drapeau. Resultat : des droits presents en base, mais en REFUS
explicite — pire que leur absence, puisqu'une ligne a False ecrase le defaut et
se lit comme une decision.

Le symptome etait total : un chef de departement recevait 403 sur tout, et son
menu revenait vide. Les entrees « Avancement » et « Statistiques » restaient
fermees au directeur alors qu'elles lui avaient ete accordees.

On ne repose donc pas les lignes — elles existent — on redresse leur drapeau,
et seulement pour les couples que ces trois migrations visaient.

Idempotente : reappliquee, elle ne change rien de plus.
"""
from django.db import migrations

# role -> module -> actions  (l'intention exacte de 0014, 0015 et 0016)
DROITS = {
    'chef_departement': {
        'ipgei_matieres':     ['voir', 'modifier', 'exporter'],
        'ipgei_notes':        ['voir', 'exporter'],
        'ipgei_deliberation': ['voir'],
        'ipgei_classes':      ['voir'],
        'suivi_charges':      ['voir'],
        'avancement':         ['voir', 'exporter'],
        'ipgei_edt':          ['voir', 'modifier'],
        'reclamations':       ['voir', 'modifier'],
    },
    'DG': {'avancement': ['voir', 'exporter'], 'statistiques': ['voir', 'exporter']},
    'DA': {'avancement': ['voir', 'exporter'], 'statistiques': ['voir', 'exporter']},
    'DE': {'avancement': ['voir', 'exporter'], 'statistiques': ['voir', 'exporter'],
           'reclamations': ['voir', 'modifier']},
    'scolarite':           {'reclamations': ['voir', 'modifier']},
    'responsable_filiere': {'reclamations': ['voir', 'modifier']},
}


def redresser(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return

    RoleDefault = apps.get_model('authentication', 'RoleDefault')
    corriges = 0
    for role, modules in DROITS.items():
        for code_module, actions in modules.items():
            corriges += (RoleDefault.objects
                         .filter(role=role,
                                 module_action__module__code=code_module,
                                 module_action__action__code__in=actions,
                                 allowed=False)
                         .update(allowed=True))
    print(f'[migration 0017] droits redresses (allowed=True) : {corriges}')


def defaire(apps, schema_editor):
    RoleDefault = apps.get_model('authentication', 'RoleDefault')
    for role, modules in DROITS.items():
        for code_module, actions in modules.items():
            RoleDefault.objects.filter(
                role=role, module_action__module__code=code_module,
                module_action__action__code__in=actions).update(allowed=False)


class Migration(migrations.Migration):
    dependencies = [('authentication', '0016_droits_chef_departement_edt_reclamations')]
    operations = [migrations.RunPython(redresser, defaire)]
