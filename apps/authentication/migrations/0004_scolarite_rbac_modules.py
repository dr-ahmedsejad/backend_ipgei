"""
Data migration — Ajouter les modules RBAC Scolarité LMD.
Modules créés :
  scolarite_filieres, scolarite_etudiants, inscriptions,
  evaluations_notes, evaluations_delib, documents, stages,
  institution, scolarite, em (si absent)
Rôle 'scolarite' reçoit voir+modifier sur tous ces modules.
Rôle 'DE' reçoit voir sur évaluations + inscriptions.
"""
from django.db import migrations


NEW_MODULES = [
    ('scolarite_filieres',  'Filières',                   'BookOpen',  20),
    ('scolarite_etudiants', 'Étudiants',                  'Users',     21),
    ('inscriptions',        'Inscriptions',               'ClipboardList', 22),
    ('evaluations_notes',   'Notes',                      'FileText',  23),
    ('evaluations_delib',   'Délibérations',              'Scale',     24),
    ('documents',           'Documents officiels',        'FileCheck', 25),
    ('stages',              'Stages',                     'Briefcase', 26),
    ('institution',         'Institution',                'Building',  27),
    ('scolarite',           'Scolarité LMD',              'GraduationCap', 19),
]

# (module_code, action_code, roles_allowed)
ROLE_DEFAULTS = [
    # scolarite = tout
    ('scolarite_filieres',  'voir',       ['scolarite', 'DE', 'responsable_filiere', 'jury_president']),
    ('scolarite_filieres',  'modifier',   ['scolarite']),
    ('scolarite_filieres',  'supprimer',  ['scolarite']),
    ('scolarite_etudiants', 'voir',       ['scolarite', 'DE', 'responsable_filiere', 'jury_president']),
    ('scolarite_etudiants', 'modifier',   ['scolarite']),
    ('scolarite_etudiants', 'supprimer',  ['scolarite']),
    ('inscriptions',        'voir',       ['scolarite', 'DE', 'responsable_filiere', 'jury_president']),
    ('inscriptions',        'modifier',   ['scolarite']),
    ('inscriptions',        'supprimer',  ['scolarite']),
    ('evaluations_notes',   'voir',       ['scolarite', 'DE', 'responsable_filiere', 'jury_president']),
    ('evaluations_notes',   'modifier',   ['scolarite', 'DE', 'responsable_filiere']),
    ('evaluations_notes',   'exporter',   ['scolarite', 'DE', 'responsable_filiere', 'jury_president']),
    ('evaluations_delib',   'voir',       ['scolarite', 'DE', 'responsable_filiere', 'jury_president']),
    ('evaluations_delib',   'modifier',   ['scolarite', 'jury_president']),
    ('evaluations_delib',   'supprimer',  ['scolarite']),
    ('documents',           'voir',       ['scolarite', 'DE', 'responsable_filiere', 'jury_president']),
    ('documents',           'modifier',   ['scolarite']),
    ('documents',           'supprimer',  ['scolarite']),
    ('stages',              'voir',       ['scolarite', 'DE', 'responsable_filiere']),
    ('stages',              'modifier',   ['scolarite', 'DE']),
    ('stages',              'supprimer',  ['scolarite']),
    ('institution',         'voir',       ['scolarite', 'DE']),
    ('institution',         'modifier',   ['scolarite']),
    ('scolarite',           'voir',       ['scolarite', 'DE', 'responsable_filiere', 'jury_president']),
    ('scolarite',           'modifier',   ['scolarite']),
    ('scolarite',           'supprimer',  ['scolarite']),
]


def add_scolarite_modules(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return  # base de test : baseline vide identique a --no-migrations

    Module = apps.get_model('authentication', 'Module')
    Action = apps.get_model('authentication', 'Action')
    ModuleAction = apps.get_model('authentication', 'ModuleAction')
    RoleDefault = apps.get_model('authentication', 'RoleDefault')

    # Créer les modules
    for code, nom, icone, ordre in NEW_MODULES:
        Module.objects.get_or_create(
            code=code,
            defaults={'nom': nom, 'icone': icone, 'ordre': ordre},
        )

    # S'assurer que les actions de base existent
    for action_code, action_nom in [
        ('voir', 'Voir'), ('modifier', 'Modifier'),
        ('supprimer', 'Supprimer'), ('exporter', 'Exporter'),
    ]:
        Action.objects.get_or_create(code=action_code, defaults={'nom': action_nom})

    # Créer les ModuleActions et RoleDefaults
    for module_code, action_code, roles in ROLE_DEFAULTS:
        try:
            mod = Module.objects.get(code=module_code)
            act = Action.objects.get(code=action_code)
        except (Module.DoesNotExist, Action.DoesNotExist):
            continue

        ma, _ = ModuleAction.objects.get_or_create(module=mod, action=act)

        for role in roles:
            RoleDefault.objects.get_or_create(
                role=role,
                module_action=ma,
                defaults={'allowed': True},
            )


def remove_scolarite_modules(apps, schema_editor):
    Module = apps.get_model('authentication', 'Module')
    for code, *_ in NEW_MODULES:
        Module.objects.filter(code=code).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0003_userpermission_filiere_alter_customuser_role_and_more'),
    ]

    operations = [
        migrations.RunPython(add_scolarite_modules, remove_scolarite_modules),
    ]
