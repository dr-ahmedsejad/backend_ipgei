"""
Signals d'audit — branche pre_save/post_save/post_delete sur les modeles
metier critiques pour ecrire un AuditLog par mutation.

Architecture :
  - pre_save  : capture l'etat actuel en DB -> stash dans audit_context
  - post_save : recupere old, calcule diff, ecrit AuditLog (CREATE ou UPDATE)
  - post_delete : ecrit AuditLog DELETE

Performance :
  - L'INSERT AuditLog est differé via transaction.on_commit() (latence ~0)
  - Si @audit_aggregate actif : skip les signals individuels

Auth :
  - user_logged_in / user_logged_out / user_login_failed branchés en bas
"""
import logging

from django.apps import apps as django_apps
from django.contrib.auth.signals import (
    user_logged_in, user_logged_out, user_login_failed,
)
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from core.audit_context import (
    aggregate_active, get_request_context, pop_old_values, stash_old_values,
)
from core.audit_helpers import (
    compute_diff, snapshot_instance, write_audit, write_audit_safe,
)
from core.models import (
    ACTION_CREATE, ACTION_DELETE, ACTION_LOGIN_FAILED, ACTION_LOGIN_SUCCESS,
    ACTION_LOGOUT, ACTION_UPDATE,
)

logger = logging.getLogger('siga')


# ─────────────────────────────────────────────────────────────────────────────
# Liste des modeles a tracer (label app.Model)
# ─────────────────────────────────────────────────────────────────────────────
TRACKED_MODELS = [
    # IPGEI — emploi du temps et evaluation.
    #
    # La suppression des pointages etait tracee, mais PAS la modification de
    # l'emploi du temps qui l'avait provoquee : en cas de litige sur des heures,
    # on savait que des donnees avaient disparu sans pouvoir dire qui avait
    # change quoi, ni quelle etait la valeur d'origine. `SeanceReelle` porte
    # l'emploi du temps reel, `SeanceType` le patron dont il decoule.
    'ipgei.SeanceReelle',
    'ipgei.SeanceType',
    'ipgei.Note',
    'ipgei.Deliberation',
    'ipgei.LigneDeliberation',
    # Suivi pedagogique
    'suivi.Suivie',
    'suivi.SuiviePointage',
    'suivi.ChargeInstitution',
    # Emplois
    'emplois.Emplois',
    'emplois.EmploisArchive',
    # Absences
    'absence.Presence',
    # Vacations
    'vacation.Vacation',
    'vacation.Surveillance',
    # Evaluations (deja traces auparavant, on enrichit avec le diff)
    'evaluations.Note',
    'evaluations.ResultatElement',
    'evaluations.ResultatModule',
    'evaluations.PVDeliberation',
    'evaluations.LigneDeliberation',
    'evaluations.RachatNote',
    # Documents (deja traces)
    'documents.DocumentOfficiel',
    'documents.RegistreDiplome',
    # Referentiel
    'departement.Departement',
    'scolarite.Filiere',
    'prof.Prof',
    'em.EM',
    'absence.Etudiant',
    # Inscriptions
    'inscriptions.InscriptionAdministrative',
    'inscriptions.InscriptionPedagogique',
    'inscriptions.InscriptionElement',
    # Reclamations
    'reclamations.Reclamation',
    'reclamations.PeriodeReclamation',
    # Stages
    'stages.DerogationMedicale',
    # Configuration
    'parametres.Institution',
    'parametres.Paiement',          # taux de paiement vacataires (financier)
    # Securite (events login/logout traites separement)
    'authentication.CustomUser',
]


# ─────────────────────────────────────────────────────────────────────────────
# Handlers generiques
# ─────────────────────────────────────────────────────────────────────────────

def _model_label(instance) -> str:
    return f'{instance._meta.app_label}.{instance.__class__.__name__}'


def _handle_pre_save(sender, instance, **kwargs):
    """Capture old values avant la mutation."""
    if aggregate_active():
        return
    if not instance.pk:
        return  # CREATE : pas de old
    try:
        old = sender.objects.filter(pk=instance.pk).first()
        if old is not None:
            stash_old_values(_model_label(instance), instance.pk,
                             snapshot_instance(old))
    except Exception as exc:
        logger.debug('pre_save snapshot failed for %s#%s: %s',
                     _model_label(instance), instance.pk, exc)


def _handle_post_save(sender, instance, created, **kwargs):
    if aggregate_active():
        return
    new = snapshot_instance(instance)
    old = pop_old_values(_model_label(instance), instance.pk)
    diff = compute_diff(old, new) if not created else compute_diff(None, new)
    if not diff and not created:
        return  # rien n'a change : pas de log
    label = str(instance)[:200]
    write_audit_safe(
        ACTION_CREATE if created else ACTION_UPDATE,
        instance, changes=diff, label=label,
    )


def _handle_post_delete(sender, instance, **kwargs):
    if aggregate_active():
        return
    snapshot = snapshot_instance(instance)
    label = str(instance)[:200]
    write_audit_safe(
        ACTION_DELETE, instance,
        changes={k: {'old': v, 'new': None} for k, v in snapshot.items()},
        label=label,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Branchement automatique sur tous les TRACKED_MODELS
# ─────────────────────────────────────────────────────────────────────────────

def _connect_signals():
    for label in TRACKED_MODELS:
        try:
            model = django_apps.get_model(label)
        except LookupError:
            logger.debug('Audit: model %s introuvable, skip', label)
            continue
        # Identifiants uniques pour pouvoir disconnect dans les tests
        pre_save.connect(_handle_pre_save, sender=model,
                         dispatch_uid=f'audit_pre_save_{label}')
        post_save.connect(_handle_post_save, sender=model,
                          dispatch_uid=f'audit_post_save_{label}')
        post_delete.connect(_handle_post_delete, sender=model,
                            dispatch_uid=f'audit_post_delete_{label}')


_connect_signals()


# ─────────────────────────────────────────────────────────────────────────────
# Authentification (Django + Axes)
# ─────────────────────────────────────────────────────────────────────────────

@receiver(user_logged_in)
def audit_login_success(sender, request, user, **kwargs):
    write_audit(
        action=ACTION_LOGIN_SUCCESS,
        model_name='CustomUser',
        object_id=str(user.pk),
        changes={},
        label=f'Connexion {user.username}',
    )


@receiver(user_logged_out)
def audit_logout(sender, request, user, **kwargs):
    if user is None:
        return
    write_audit(
        action=ACTION_LOGOUT,
        model_name='CustomUser',
        object_id=str(user.pk),
        changes={},
        label=f'Déconnexion {user.username}',
    )


@receiver(user_login_failed)
def audit_login_failed(sender, credentials, request=None, **kwargs):
    username = (credentials or {}).get('username', '?')
    write_audit(
        action=ACTION_LOGIN_FAILED,
        model_name='CustomUser',
        object_id='0',
        changes={'attempted_username': username},
        label=f'Échec connexion {username}',
        keep_forever=True,  # securite : conserver indefiniment
    )
