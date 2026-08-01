"""
Data migration — Decoupage RBAC granulaire (Phase 1).

Ajoute 21 nouveaux modules qui remplacent (en plus fin) 8 modules trop grossiers.
Les anciens modules ne sont PAS supprimes ici (migration 0011 le fera apres remap
des UserPermission existants en migration 0010).

Mapping AVANT -> APRES :
  documents          -> doc_attestation, doc_releve, doc_diplome, doc_registre
  vacations          -> vac_saisie, vac_validation, vac_paiement
  evaluations_notes  -> eval_saisie, eval_anonymat, eval_emargement, eval_collecte
  evaluations_delib  -> delib_pv, delib_jury, delib_rachat
  inscriptions       -> insc_administrative, insc_pedagogique, insc_derogation, insc_progression
  absences           -> abs_saisie, abs_justificatifs, abs_rapport, abs_import
  suivi              -> suivi_saisie, suivi_fiches, suivi_charges
  stages             -> stage_convention, stage_evaluation, stage_derogation, stage_classement
  scolarite_etudiants -> sco_etudiants_import (nouveau code, scolarite_etudiants reste)

Idempotent via get_or_create.
"""
from django.db import migrations


# (code, nom, icone, ordre)
NEW_MODULES = [
    # documents (4)
    ('doc_attestation',     'Documents : attestations',  'FileBadge',     50),
    ('doc_releve',          'Documents : releves',        'FileText',      51),
    ('doc_diplome',         'Documents : diplomes',       'GraduationCap', 52),
    ('doc_registre',        'Documents : registre',       'BookMarked',    53),
    # vacations (3)
    ('vac_saisie',          'Vacations : saisie',         'Banknote',      54),
    ('vac_validation',      'Vacations : validation',     'CheckCircle',   55),
    ('vac_paiement',        'Vacations : paiement',       'Coins',         56),
    # evaluations_notes (4)
    ('eval_saisie',         'Evaluations : saisie notes', 'Edit3',         57),
    ('eval_anonymat',       'Evaluations : anonymat',     'EyeOff',        58),
    ('eval_emargement',     'Evaluations : emargement',   'ClipboardCheck',59),
    ('eval_collecte',       'Evaluations : collecte',     'ListChecks',    60),
    # evaluations_delib (3)
    ('delib_pv',            'Deliberations : PV',         'FileText',      61),
    ('delib_jury',          'Deliberations : jury',       'Users',         62),
    ('delib_rachat',        'Deliberations : rachats',    'TrendingUp',    63),
    # inscriptions (4)
    ('insc_administrative', 'Inscriptions : admin.',      'UserCheck',     64),
    ('insc_pedagogique',    'Inscriptions : pedag.',      'BookOpen',      65),
    ('insc_derogation',     'Inscriptions : derogations', 'AlertCircle',   66),
    ('insc_progression',    'Inscriptions : progressions','ArrowUpCircle', 67),
    # absences (4)
    ('abs_saisie',          'Absences : saisie',          'UserX',         68),
    ('abs_justificatifs',   'Absences : justificatifs',   'FileCheck',     69),
    ('abs_rapport',         'Absences : rapports',        'BarChart2',     70),
    ('abs_import',          'Absences : import',          'Upload',        71),
    # suivi (3)
    ('suivi_saisie',        'Suivi : saisie',             'ClipboardList', 72),
    ('suivi_fiches',        'Suivi : fiches',             'FileText',      73),
    ('suivi_charges',       'Suivi : charges',            'Briefcase',     74),
    # stages (4)
    ('stage_convention',    'Stages : conventions',       'FileText',      75),
    ('stage_evaluation',    'Stages : evaluations',       'Star',          76),
    ('stage_derogation',    'Stages : derogations',       'AlertCircle',   77),
    ('stage_classement',    'Stages : classement',        'ListOrdered',   78),
    # scolarite_etudiants (1 nouveau code complementaire)
    ('sco_etudiants_import','Etudiants : import Excel',   'Upload',        79),
]

# Toutes les actions appliquees sur tous les nouveaux modules.
# (les RoleDefault sont poses dans la migration 0009)
DEFAULT_ACTIONS = ['voir', 'modifier', 'supprimer', 'exporter']


def add_granular_modules(apps, schema_editor):
    from django.conf import settings
    if getattr(settings, 'SEEDS_DESACTIVES_POUR_TESTS', False):
        return  # base de test : baseline vide identique a --no-migrations

    Module       = apps.get_model('authentication', 'Module')
    Action       = apps.get_model('authentication', 'Action')
    ModuleAction = apps.get_model('authentication', 'ModuleAction')

    # S'assurer que les 4 actions de base existent
    for code, nom in [('voir', 'Voir'), ('modifier', 'Modifier'),
                      ('supprimer', 'Supprimer'), ('exporter', 'Exporter')]:
        Action.objects.get_or_create(code=code, defaults={'nom': nom})

    created = 0
    for code, nom, icone, ordre in NEW_MODULES:
        mod, was_created = Module.objects.get_or_create(
            code=code,
            defaults={'nom': nom, 'icone': icone, 'ordre': ordre},
        )
        if was_created:
            created += 1
        # Toujours s'assurer que les 4 ModuleAction existent pour ce module
        for action_code in DEFAULT_ACTIONS:
            try:
                act = Action.objects.get(code=action_code)
            except Action.DoesNotExist:
                continue
            ModuleAction.objects.get_or_create(module=mod, action=act)

    print(f'[migration 0008] Modules granulaires : {created} crees, '
          f'{len(NEW_MODULES) - created} deja presents')


def remove_granular_modules(apps, schema_editor):
    """Reverse : on supprime les modules ajoutes (CASCADE -> ModuleAction, RoleDefault, UserPermission)."""
    Module = apps.get_model('authentication', 'Module')
    for code, *_ in NEW_MODULES:
        Module.objects.filter(code=code).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0007_seed_role_defaults_complete'),
    ]

    operations = [
        migrations.RunPython(add_granular_modules, remove_granular_modules),
    ]
