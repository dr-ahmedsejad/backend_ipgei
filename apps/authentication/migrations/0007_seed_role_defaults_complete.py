"""
Data migration — Pré-provisionnement complet des RoleDefault pour les 7 rôles non-admin.

Phase 3 RBAC : aligne les permissions par défaut sur les responsabilités métier réelles
de chaque rôle. Permet à un nouveau user de fonctionner sans intervention admin.

Idempotent : utilise `update_or_create` — peut être rejouée sans casser les overrides
manuels existants (UserPermission individuel par user prime).

Décisions validées avec l'utilisateur (2026-05-06) :
  - DG (Dir. général)        : TOUT en lecture
  - DA (Dir. administrative) : idem DG + écriture sur absences (gestion justificatifs)
  - DE (Dir. des études)     : tout sauf admin (lecture+modifier opérationnel)
  - scolarite                : inscriptions, scolarite, evaluations, stages, etc.
  - responsable_filiere      : scolarite/em/profs sur sa filière
  - jury_president           : evaluations_delib + evaluations_notes (consultation+exporter)
  - IT                       : deblocage + historique + notifications
  - AA                       : consultation avancement, statistiques, suivi, profs, vacations
"""
from django.db import migrations


# Modules en lecture pour DG (tout sauf admin pur : comptes, rbac, deblocage hardcodé IsAdmin/IT)
DG_MODULES_LECTURE = [
    'statistiques', 'emplois', 'suivi', 'avancement', 'absences', 'vacations',
    'departements', 'profs', 'em', 'salles', 'banques',
    'scolarite', 'scolarite_filieres', 'scolarite_etudiants',
    'inscriptions', 'evaluations_notes', 'evaluations_delib',
    'documents', 'stages', 'reclamations', 'notifications', 'institution',
]

# (module_code, action_code, roles_allowed)
# Reflète le besoin métier de chaque rôle.
ROLE_DEFAULTS = (
    # ── DG : tout en lecture ────────────────────────────────────────────
    *[(m, 'voir',     ['DG', 'DA']) for m in DG_MODULES_LECTURE],
    *[(m, 'exporter', ['DG', 'DA']) for m in DG_MODULES_LECTURE
      if m in ('statistiques', 'avancement', 'evaluations_notes', 'evaluations_delib',
               'vacations', 'suivi', 'documents')],

    # ── DA : DG + écriture sur absences (justificatifs Art. 23/29) ─────
    ('absences', 'modifier', ['DA']),

    # ── DE (Dir. des études) : opérationnel complet (sauf admin) ────────
    ('emplois',         'voir',     ['DE', 'scolarite', 'AA']),
    ('emplois',         'modifier', ['DE']),
    ('emplois',         'supprimer',['DE']),

    ('suivi',           'voir',     ['DE', 'scolarite', 'AA']),
    ('suivi',           'modifier', ['DE']),
    ('suivi',           'supprimer',['DE']),

    ('profs',           'voir',     ['DE', 'scolarite', 'responsable_filiere', 'AA']),
    ('profs',           'modifier', ['DE', 'scolarite']),
    ('profs',           'supprimer',['DE']),

    ('salles',          'voir',     ['DE', 'scolarite']),
    ('salles',          'modifier', ['DE']),

    ('em',              'voir',     ['DE', 'scolarite', 'responsable_filiere']),
    ('em',              'modifier', ['DE', 'scolarite']),
    ('em',              'supprimer',['DE']),

    ('departements',    'voir',     ['DE', 'scolarite']),
    ('departements',    'modifier', ['DE', 'scolarite']),

    ('absences',        'voir',     ['DE', 'scolarite']),
    # ('absences','modifier', ['DA']) déjà déclaré ci-dessus
    ('absences',        'supprimer',['DE']),

    ('vacations',       'voir',     ['DE', 'AA']),
    ('vacations',       'modifier', ['DE']),
    ('vacations',       'exporter', ['DE']),

    ('avancement',      'voir',     ['DE', 'AA']),
    ('avancement',      'exporter', ['DE']),

    ('statistiques',    'voir',     ['DE', 'AA']),
    ('statistiques',    'exporter', ['DE']),

    ('reclamations',    'voir',     ['DE', 'scolarite']),
    ('reclamations',    'modifier', ['DE', 'scolarite']),

    ('banques',         'voir',     ['DE', 'scolarite']),

    # ── Modules Scolarité LMD : completes pour DE (preexistant pour scolarite) ──
    ('scolarite',          'modifier', ['DE']),  # complement migr 0004
    ('inscriptions',       'modifier', ['DE']),  # complement migr 0004
    ('evaluations_notes',  'supprimer',['DE']),  # complement migr 0004
    ('evaluations_delib',  'modifier', ['DE']),  # complement migr 0004
    ('evaluations_delib',  'exporter', ['DE']),  # complement migr 0004
    ('stages',             'modifier', ['DE']),  # complement migr 0004
    ('documents',          'modifier', ['DE']),  # complement migr 0004

    # ── jury_president : focus sur deliberations ──────────────────────
    ('evaluations_delib',  'modifier', ['jury_president']),
    # voir + exporter deja en migr 0004

    # ── IT : tâches techniques ─────────────────────────────────────────
    ('historique',     'voir',     ['IT']),
    ('deblocage',      'voir',     ['IT']),
    ('deblocage',      'modifier', ['IT']),
    ('notifications',  'voir',     ['IT']),
    ('notifications',  'modifier', ['IT']),

    # ── notifications : tous rôles métier (consultation perso) ─────────
    ('notifications',  'voir',     ['DG', 'DA', 'DE', 'AA', 'scolarite',
                                    'responsable_filiere', 'jury_president']),
)


def seed_role_defaults(apps, schema_editor):
    """Pose les RoleDefault listés ci-dessus. Idempotent via update_or_create."""
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return  # base de test : baseline vide identique a --no-migrations

    Module        = apps.get_model('authentication', 'Module')
    Action        = apps.get_model('authentication', 'Action')
    ModuleAction  = apps.get_model('authentication', 'ModuleAction')
    RoleDefault   = apps.get_model('authentication', 'RoleDefault')

    created_count = 0
    skipped_count = 0
    missing_modules = set()

    for module_code, action_code, roles in ROLE_DEFAULTS:
        try:
            module = Module.objects.get(code=module_code)
        except Module.DoesNotExist:
            missing_modules.add(module_code)
            continue

        try:
            action = Action.objects.get(code=action_code)
        except Action.DoesNotExist:
            missing_modules.add(f'action:{action_code}')
            continue

        try:
            ma = ModuleAction.objects.get(module=module, action=action)
        except ModuleAction.DoesNotExist:
            # Crée la ModuleAction si elle manque (rare cas d'incohérence)
            ma = ModuleAction.objects.create(module=module, action=action)

        for role in roles:
            _, was_created = RoleDefault.objects.update_or_create(
                role=role, module_action=ma,
                defaults={'allowed': True},
            )
            if was_created:
                created_count += 1
            else:
                skipped_count += 1

    if missing_modules:
        print(f'[migration 0007] Modules/Actions absents en BD : {missing_modules}')
    print(f'[migration 0007] RoleDefaults : {created_count} crees, {skipped_count} deja existants')


def reverse_seed(apps, schema_editor):
    """Reverse no-op : on ne supprime pas les RoleDefaults posés (sécurité).
    Pour un vrai rollback, l'admin peut toggler manuellement via /comptes/defaults."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0006_enseignant_role_prof_user'),
    ]

    operations = [
        migrations.RunPython(seed_role_defaults, reverse_seed),
    ]
