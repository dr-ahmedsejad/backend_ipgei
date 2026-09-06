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
    aggregate_active, get_request_context, mdp_deja_trace,
    pop_old_values, stash_old_values,
)
from core.audit_helpers import (
    compute_diff, snapshot_instance, write_audit, write_audit_safe,
)
from core.models import (
    ACTION_CREATE, ACTION_DELETE, ACTION_LOGIN_FAILED, ACTION_LOGIN_SUCCESS,
    ACTION_LOGOUT, ACTION_PASSWORD_CHANGED, ACTION_PASSWORD_RESET, ACTION_UPDATE,
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
    # `Note` ne porte que la MOYENNE recalculee ; la note reellement saisie
    # vit dans `Evaluation` (DS 1, DS 2, examen...). Sans elle, le journal
    # montrait une moyenne qui bouge sans jamais dire quelle note l'avait
    # fait bouger, ni de combien — inexploitable en cas de litige. Une note
    # effacee ne laissait, elle, aucune trace du tout.
    'ipgei.Evaluation',
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


def _libelle(instance) -> str:
    """
    Le libelle lu dans le journal. `__str__` sert l'affichage courant et
    reste souvent trop court pour une trace : « Devoir surveille 1 : 12.50 »
    ne dit ni de qui ni dans quelle matiere. Un modele peut donc proposer
    `libelle_audit()`.
    """
    mieux = getattr(instance, 'libelle_audit', None)
    if callable(mieux):
        try:
            return str(mieux())[:200]
        except Exception as exc:                               # noqa: BLE001
            logger.debug('libelle_audit a echoue sur %s: %s',
                         _model_label(instance), exc)
    return str(instance)[:200]


def _handle_post_save(sender, instance, created, **kwargs):
    if aggregate_active():
        return
    new = snapshot_instance(instance)
    old = pop_old_values(_model_label(instance), instance.pk)
    diff = compute_diff(old, new) if not created else compute_diff(None, new)
    if not diff and not created:
        return  # rien n'a change : pas de log
    label = _libelle(instance)
    write_audit_safe(
        ACTION_CREATE if created else ACTION_UPDATE,
        instance, changes=diff, label=label,
    )


def _handle_post_delete(sender, instance, **kwargs):
    if aggregate_active():
        return
    snapshot = snapshot_instance(instance)
    label = _libelle(instance)
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
        user=user,
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
        user=user,
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


# ─────────────────────────────────────────────────────────────────────────────
# Mot de passe — le filet que le diff generique ne peut pas tendre
# ─────────────────────────────────────────────────────────────────────────────
#
# `password` est dans BLACKLIST_FIELDS : aucun hachage ne doit atterrir au
# journal. Effet de bord, le diff generique sur `CustomUser` ne voit JAMAIS un
# changement de mot de passe. Une remise a zero depuis l'admin Django, un shell
# ou un script ne laissait donc aucune trace — alors que c'est precisement le
# geste qu'on vient chercher dans un journal.
#
# On compare le hachage avant/apres, sans jamais l'ecrire. Qui agit decide de
# l'action : le titulaire lui-meme -> CHANGED, un tiers (ou personne, cas d'une
# commande) -> RESET.

_MDP = 'auth.password'


def _mdp_pre_save(sender, instance, **kwargs):
    if not instance.pk:
        return                                    # creation : pas de « avant »
    try:
        ancien = (sender.objects.filter(pk=instance.pk)
                  .values_list('password', flat=True).first())
    except Exception as exc:                                       # noqa: BLE001
        logger.debug('Lecture du hachage impossible pk=%s: %s', instance.pk, exc)
        return
    stash_old_values(_MDP, instance.pk, {'h': ancien})


def _mdp_post_save(sender, instance, created, **kwargs):
    if created:
        return
    avant = pop_old_values(_MDP, instance.pk)
    if avant is None or avant.get('h') == instance.password:
        return                                    # le mot de passe n'a pas bouge
    if mdp_deja_trace(instance.pk):
        return                                    # une vue a deja dit mieux

    acteur = get_request_context().get('user')
    par_le_titulaire = acteur is not None and acteur.pk == instance.pk
    if par_le_titulaire:
        write_audit(
            action=ACTION_PASSWORD_CHANGED, model_name='CustomUser',
            object_id=str(instance.pk), changes={},
            label=f'Mot de passe modifié — {instance.username}',
            keep_forever=True, user=acteur,
        )
        return
    write_audit(
        action=ACTION_PASSWORD_RESET, model_name='CustomUser',
        object_id=str(instance.pk), changes={},
        label=(f'Mot de passe réinitialisé — {instance.username} '
               f'par {acteur.username if acteur is not None else "console/script"}'),
        keep_forever=True, user=acteur,
    )


def _connecter_signaux_mot_de_passe():
    from django.contrib.auth import get_user_model
    modele = get_user_model()
    pre_save.connect(_mdp_pre_save, sender=modele, dispatch_uid='audit_mdp_pre')
    post_save.connect(_mdp_post_save, sender=modele, dispatch_uid='audit_mdp_post')


_connecter_signaux_mot_de_passe()
