"""
Data migration — modules RBAC du moteur académique IPGEI.

Chaque écran IPGEI est adossé à un module RBAC, dans la continuité du socle :
les ViewSets déclarent `required_module`, la matrice de permissions les expose,
et l'admin conserve son bypass. Les RoleDefault posés ici sont les accès
raisonnables par défaut — ils restent affinables dans la matrice.

Idempotent (get_or_create / update_or_create).
"""
from django.db import migrations


# (code, nom, icone, ordre)
MODULES_IPGEI = [
    ('ipgei_classes',       'IPGEI : classes et sous-groupes',   'Users',        200),
    ('ipgei_inscriptions',  'IPGEI : inscriptions étudiants',    'UserCheck',    201),
    ('ipgei_matieres',      'IPGEI : matières et pondération',   'BookOpen',     202),
    ('ipgei_notes',         'IPGEI : saisie des notes',          'Edit3',        203),
    ('ipgei_deliberation',  'IPGEI : délibération',              'Scale',        204),
    ('ipgei_absences',      'IPGEI : absences',                  'UserX',        205),
    ('ipgei_edt',           'IPGEI : emploi du temps',           'CalendarDays', 206),
    ('ipgei_permutations',  'IPGEI : permutations',              'Repeat',       207),
    ('ipgei_documents',     'IPGEI : documents officiels',       'FileBadge',    208),
    ('ipgei_parametres',    'IPGEI : paramètres du cursus',      'Settings',     209),
]

ACTIONS = [
    ('voir',      'Voir'),
    ('modifier',  'Modifier'),
    ('supprimer', 'Supprimer'),
    ('exporter',  'Exporter'),
]

V, M, S, X = 'voir', 'modifier', 'supprimer', 'exporter'

# Accès par défaut. `ipgei_parametres` n'apparaît volontairement pas : le
# paramétrage du cursus (seuil, plafond, droit de redoublement) reste réservé
# à l'admin tant qu'il n'est pas explicitement délégué.
ROLE_DEFAULTS = [
    ('ipgei_classes',      V, ['DG', 'DA', 'DE', 'AA', 'scolarite']),
    ('ipgei_classes',      M, ['DE', 'scolarite']),

    ('ipgei_inscriptions', V, ['DG', 'DA', 'DE', 'AA', 'scolarite']),
    ('ipgei_inscriptions', M, ['DE', 'scolarite']),
    ('ipgei_inscriptions', X, ['DG', 'DE', 'scolarite']),

    ('ipgei_matieres',     V, ['DG', 'DA', 'DE', 'AA', 'scolarite']),
    ('ipgei_matieres',     M, ['DE', 'scolarite']),

    ('ipgei_notes',        V, ['DG', 'DA', 'DE', 'scolarite']),
    ('ipgei_notes',        M, ['DE', 'scolarite']),
    ('ipgei_notes',        X, ['DG', 'DE', 'scolarite']),

    ('ipgei_deliberation', V, ['DG', 'DA', 'DE', 'scolarite', 'jury_president']),
    # La validation d'un jury engage l'institution : réservée à la direction
    # de l'enseignement et au président de jury.
    ('ipgei_deliberation', M, ['DE', 'jury_president']),
    ('ipgei_deliberation', X, ['DG', 'DE', 'scolarite', 'jury_president']),

    ('ipgei_absences',     V, ['DG', 'DA', 'DE', 'AA', 'scolarite']),
    ('ipgei_absences',     M, ['DE', 'AA', 'scolarite']),
    ('ipgei_absences',     X, ['DG', 'DE', 'AA']),

    ('ipgei_edt',          V, ['DG', 'DA', 'DE', 'AA', 'scolarite']),
    ('ipgei_edt',          M, ['DE', 'AA']),

    ('ipgei_permutations', V, ['DG', 'DA', 'DE', 'AA']),
    ('ipgei_permutations', M, ['DE']),

    ('ipgei_documents',    V, ['DG', 'DA', 'DE', 'scolarite']),
    ('ipgei_documents',    M, ['DE', 'scolarite']),
    ('ipgei_documents',    X, ['DG', 'DE', 'scolarite']),
]


def creer_modules(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return  # base de test : baseline vide, identique a --no-migrations

    Module       = apps.get_model('authentication', 'Module')
    Action       = apps.get_model('authentication', 'Action')
    ModuleAction = apps.get_model('authentication', 'ModuleAction')
    RoleDefault  = apps.get_model('authentication', 'RoleDefault')

    for code, nom in ACTIONS:
        Action.objects.get_or_create(code=code, defaults={'nom': nom})

    for code, nom, icone, ordre in MODULES_IPGEI:
        module, _ = Module.objects.get_or_create(
            code=code, defaults={'nom': nom, 'icone': icone, 'ordre': ordre},
        )
        for action_code, _lib in ACTIONS:
            action = Action.objects.get(code=action_code)
            ModuleAction.objects.get_or_create(module=module, action=action)

    for module_code, action_code, roles in ROLE_DEFAULTS:
        module = Module.objects.filter(code=module_code).first()
        action = Action.objects.filter(code=action_code).first()
        if module is None or action is None:
            continue
        ma, _ = ModuleAction.objects.get_or_create(module=module, action=action)
        for role in roles:
            RoleDefault.objects.update_or_create(
                role=role, module_action=ma, defaults={'allowed': True},
            )

    print(f'[ipgei 0002] {len(MODULES_IPGEI)} modules RBAC IPGEI prets.')


def supprimer_modules(apps, schema_editor):
    Module = apps.get_model('authentication', 'Module')
    Module.objects.filter(code__in=[c for c, *_ in MODULES_IPGEI]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('ipgei', '0001_initial'),
        ('authentication', '0012_add_grille_frais_module'),
    ]

    operations = [
        migrations.RunPython(creer_modules, supprimer_modules),
    ]
