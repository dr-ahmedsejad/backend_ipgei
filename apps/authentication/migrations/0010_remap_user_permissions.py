"""
Data migration — Remap UserPermission legacy -> granulaire (Phase 4 RBAC).

Pour chaque UserPermission qui pointe vers un module legacy (documents, vacations,
evaluations_notes, evaluations_delib, inscriptions, absences, suivi, stages),
on cree des UserPermission equivalentes sur les nouveaux modules granulaires
(qui couvrent le perimetre de l'ancien).

But : preserver les overrides existants pour qu'aucun user ne perde l'acces
qu'il avait pendant la transition. Le nettoyage des anciennes UserPermission
sera fait en migration 0011 (apres validation en prod).

Idempotent via update_or_create. La valeur 'allowed' est preservee
(allowed=True/False), permettant a un refus explicite (off) de se propager
correctement aux sous-modules.
"""
from django.db import migrations


# Mapping legacy_code -> [nouveau_code, ...]
REMAP = {
    'documents':         ['doc_attestation', 'doc_releve', 'doc_diplome', 'doc_registre'],
    'vacations':         ['vac_saisie', 'vac_validation', 'vac_paiement'],
    'evaluations_notes': ['eval_saisie', 'eval_anonymat', 'eval_emargement', 'eval_collecte'],
    'evaluations_delib': ['delib_pv', 'delib_jury', 'delib_rachat'],
    'inscriptions':      ['insc_administrative', 'insc_pedagogique', 'insc_derogation', 'insc_progression'],
    'absences':          ['abs_saisie', 'abs_justificatifs', 'abs_rapport', 'abs_import'],
    'suivi':             ['suivi_saisie', 'suivi_fiches', 'suivi_charges'],
    'stages':            ['stage_convention', 'stage_evaluation', 'stage_derogation', 'stage_classement'],
}


def remap_user_permissions(apps, schema_editor):
    UserPermission = apps.get_model('authentication', 'UserPermission')
    Module         = apps.get_model('authentication', 'Module')
    ModuleAction   = apps.get_model('authentication', 'ModuleAction')

    created_count = 0
    skipped_count = 0
    legacy_count  = 0

    for legacy_code, new_codes in REMAP.items():
        try:
            legacy_module = Module.objects.get(code=legacy_code)
        except Module.DoesNotExist:
            print(f'[migration 0010] Module legacy absent : {legacy_code} (skip)')
            continue

        legacy_perms = UserPermission.objects.filter(
            module_action__module=legacy_module,
        ).select_related('module_action__action')

        for legacy_perm in legacy_perms:
            legacy_count += 1
            action = legacy_perm.module_action.action

            for new_code in new_codes:
                try:
                    new_module = Module.objects.get(code=new_code)
                except Module.DoesNotExist:
                    continue
                new_ma, _ = ModuleAction.objects.get_or_create(
                    module=new_module, action=action,
                )
                _, was_created = UserPermission.objects.update_or_create(
                    user=legacy_perm.user,
                    module_action=new_ma,
                    defaults={'allowed': legacy_perm.allowed},
                )
                if was_created:
                    created_count += 1
                else:
                    skipped_count += 1

    print(f'[migration 0010] Remap UserPermission : {legacy_count} legacy lu(s), '
          f'{created_count} crees, {skipped_count} deja existants/maj')


def reverse_remap(apps, schema_editor):
    """Reverse no-op : on ne supprime pas les UserPermission ajoutees (securite).
    Pour rollback : passer manuellement sur la BD ou via /comptes/permissions."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0009_seed_role_defaults_granular'),
    ]

    operations = [
        migrations.RunPython(remap_user_permissions, reverse_remap),
    ]
