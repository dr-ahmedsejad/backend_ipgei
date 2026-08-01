"""
Data migration — RoleDefault v2 : matrice granulaire (Phase 3 RBAC).

Pose les RoleDefault pour les 30 nouveaux modules introduits en migration 0008,
selon la matrice valid�e avec l'utilisateur (2026-05-06).

Idempotent : update_or_create. Les RoleDefault existants sur les anciens modules
(documents, vacations, evaluations_notes, etc.) sont CONSERVES jusqu'au cleanup
final (migration 0011) pour permettre une transition progressive.

Format ROLE_DEFAULTS :
  (module_code, action_code, [roles_allowed])
"""
from django.db import migrations


# Helper: actions courantes
V = 'voir'
M = 'modifier'
S = 'supprimer'
X = 'exporter'


ROLE_DEFAULTS = (
    # ════════════════════════════════════════════════════════════════════════
    # DOCUMENTS (4 codes)
    # ════════════════════════════════════════════════════════════════════════
    ('doc_attestation',     V, ['DG', 'DA', 'DE', 'scolarite', 'responsable_filiere', 'AA']),
    ('doc_attestation',     M, ['DE', 'scolarite']),
    ('doc_releve',          V, ['DG', 'DA', 'DE', 'scolarite', 'responsable_filiere', 'AA']),
    ('doc_releve',          M, ['DE', 'scolarite']),
    # diplome : ULTRA sensible -> DE uniquement (admin bypass automatique)
    ('doc_diplome',         V, ['DG', 'DA', 'DE']),
    ('doc_diplome',         M, ['DE']),
    ('doc_registre',        V, ['DG', 'DA', 'DE', 'scolarite', 'responsable_filiere', 'AA']),
    ('doc_registre',        X, ['DG', 'DA', 'DE', 'scolarite']),

    # ════════════════════════════════════════════════════════════════════════
    # VACATIONS (3 codes)
    # ════════════════════════════════════════════════════════════════════════
    ('vac_saisie',          V, ['DG', 'DA', 'DE', 'AA']),
    ('vac_saisie',          M, ['DE', 'AA']),
    ('vac_validation',      V, ['DG', 'DA', 'DE']),
    ('vac_validation',      M, ['DE']),
    # paiement : sensible -> DE seulement (DG/DA en lecture+export)
    ('vac_paiement',        V, ['DG', 'DA', 'DE']),
    ('vac_paiement',        M, ['DE']),
    ('vac_paiement',        X, ['DG', 'DA', 'DE']),

    # ════════════════════════════════════════════════════════════════════════
    # EVALUATIONS NOTES (4 codes)
    # ════════════════════════════════════════════════════════════════════════
    ('eval_saisie',         V, ['DG', 'DA', 'DE', 'scolarite', 'responsable_filiere', 'jury_president']),
    ('eval_saisie',         M, ['DE', 'scolarite', 'responsable_filiere']),
    ('eval_saisie',         X, ['DG', 'DE', 'scolarite', 'jury_president']),
    # anonymat : DE + jury_president uniquement
    ('eval_anonymat',       V, ['DE', 'jury_president']),
    ('eval_anonymat',       M, ['DE', 'jury_president']),
    ('eval_emargement',     V, ['DG', 'DA', 'DE', 'scolarite']),
    ('eval_emargement',     M, ['DE', 'scolarite']),
    ('eval_collecte',       V, ['DG', 'DE', 'scolarite']),
    ('eval_collecte',       M, ['DE', 'scolarite']),

    # ════════════════════════════════════════════════════════════════════════
    # DELIBERATIONS (3 codes)
    # ════════════════════════════════════════════════════════════════════════
    ('delib_pv',            V, ['DG', 'DA', 'DE', 'scolarite', 'responsable_filiere', 'jury_president']),
    ('delib_pv',            M, ['DE', 'jury_president']),
    ('delib_pv',            X, ['DG', 'DE', 'jury_president']),
    ('delib_jury',          V, ['DG', 'DE']),
    ('delib_jury',          M, ['DE']),
    ('delib_rachat',        V, ['DE', 'jury_president']),
    ('delib_rachat',        M, ['DE', 'jury_president']),

    # ════════════════════════════════════════════════════════════════════════
    # INSCRIPTIONS (4 codes)
    # ════════════════════════════════════════════════════════════════════════
    ('insc_administrative', V, ['DG', 'DA', 'DE', 'scolarite']),
    ('insc_administrative', M, ['DE', 'scolarite']),
    ('insc_pedagogique',    V, ['DG', 'DA', 'DE', 'scolarite', 'responsable_filiere']),
    ('insc_pedagogique',    M, ['DE', 'scolarite', 'responsable_filiere']),
    ('insc_derogation',     V, ['DG', 'DA', 'DE', 'scolarite']),
    ('insc_derogation',     M, ['DE', 'scolarite']),
    ('insc_progression',    V, ['DE', 'scolarite']),
    ('insc_progression',    M, ['DE', 'scolarite']),

    # ════════════════════════════════════════════════════════════════════════
    # ABSENCES (4 codes)
    # ════════════════════════════════════════════════════════════════════════
    ('abs_saisie',          V, ['DG', 'DA', 'DE', 'scolarite']),
    ('abs_saisie',          M, ['DE', 'scolarite']),
    # justificatifs : DA principalement (cf decision metier)
    ('abs_justificatifs',   V, ['DG', 'DA', 'DE']),
    ('abs_justificatifs',   M, ['DA', 'DE']),
    ('abs_rapport',         V, ['DG', 'DA', 'DE', 'scolarite', 'responsable_filiere', 'AA']),
    ('abs_rapport',         X, ['DG', 'DA', 'DE', 'scolarite']),
    # import : sensible (ecrasement masse)
    ('abs_import',          V, ['DE', 'scolarite']),
    ('abs_import',          M, ['DE', 'scolarite']),

    # ════════════════════════════════════════════════════════════════════════
    # SUIVI (3 codes)
    # ════════════════════════════════════════════════════════════════════════
    ('suivi_saisie',        V, ['DG', 'DA', 'DE']),
    ('suivi_saisie',        M, ['DE']),
    ('suivi_fiches',        V, ['DG', 'DA', 'DE', 'scolarite', 'responsable_filiere', 'AA']),
    ('suivi_charges',       V, ['DG', 'DA', 'DE', 'scolarite', 'responsable_filiere', 'AA']),
    ('suivi_charges',       X, ['DG', 'DA', 'DE', 'AA']),

    # ════════════════════════════════════════════════════════════════════════
    # STAGES (4 codes)
    # ════════════════════════════════════════════════════════════════════════
    ('stage_convention',    V, ['DG', 'DA', 'DE', 'scolarite', 'responsable_filiere']),
    ('stage_convention',    M, ['DE', 'scolarite', 'responsable_filiere']),
    ('stage_evaluation',    V, ['DG', 'DA', 'DE', 'scolarite', 'responsable_filiere']),
    ('stage_evaluation',    M, ['DE', 'scolarite', 'responsable_filiere']),
    ('stage_derogation',    V, ['DG', 'DA', 'DE', 'scolarite']),
    ('stage_derogation',    M, ['DE', 'scolarite']),
    ('stage_classement',    V, ['DG', 'DE', 'scolarite', 'responsable_filiere']),
    ('stage_classement',    M, ['DE', 'scolarite', 'responsable_filiere']),

    # ════════════════════════════════════════════════════════════════════════
    # SCOLARITE ETUDIANTS — IMPORT (1 nouveau code, le reste reste sur scolarite_etudiants)
    # ════════════════════════════════════════════════════════════════════════
    ('sco_etudiants_import',V, ['DE', 'scolarite']),
    ('sco_etudiants_import',M, ['DE', 'scolarite']),
)


def seed_role_defaults_granular(apps, schema_editor):
    """Pose les RoleDefault granulaires. Idempotent."""
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return  # base de test : baseline vide identique a --no-migrations

    Module        = apps.get_model('authentication', 'Module')
    Action        = apps.get_model('authentication', 'Action')
    ModuleAction  = apps.get_model('authentication', 'ModuleAction')
    RoleDefault   = apps.get_model('authentication', 'RoleDefault')

    created_count = 0
    skipped_count = 0
    missing       = []

    for module_code, action_code, roles in ROLE_DEFAULTS:
        try:
            module = Module.objects.get(code=module_code)
        except Module.DoesNotExist:
            missing.append(f'module:{module_code}')
            continue
        try:
            action = Action.objects.get(code=action_code)
        except Action.DoesNotExist:
            missing.append(f'action:{action_code}')
            continue
        ma, _ = ModuleAction.objects.get_or_create(module=module, action=action)
        for role in roles:
            _, was_created = RoleDefault.objects.update_or_create(
                role=role, module_action=ma,
                defaults={'allowed': True},
            )
            if was_created:
                created_count += 1
            else:
                skipped_count += 1

    if missing:
        print(f'[migration 0009] Modules/Actions manquants : {missing}')
    print(f'[migration 0009] RoleDefault granulaires : {created_count} crees, '
          f'{skipped_count} deja existants')


def reverse_seed(apps, schema_editor):
    """Reverse no-op (securite). Pour rollback : retoucher manuellement via /comptes/defaults."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0008_split_granular_modules'),
    ]

    operations = [
        migrations.RunPython(seed_role_defaults_granular, reverse_seed),
    ]
