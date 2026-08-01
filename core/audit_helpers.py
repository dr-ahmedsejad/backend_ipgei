"""
Helpers d'audit : capture du diff old/new, ecriture safe, decorateur d'agregation.

Garantie de performance :
  - L'INSERT AuditLog est differé via transaction.on_commit() : il s'execute
    APRES le commit principal, pas pendant. Latence utilisateur ~0.

Securite :
  - _write_audit_safe() ne leve jamais d'exception (audit ne doit pas casser
    une fonctionnalite metier).
"""
from __future__ import annotations
import functools
import logging
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models.fields import NOT_PROVIDED
from django.db.models.fields.related import ForeignKey, OneToOneField

from core.audit_context import (
    aggregate_active, get_request_context, set_aggregate_active,
)

logger = logging.getLogger('siga')


# Champs jamais inclus dans le diff (bruit ou sensibles).
BLACKLIST_FIELDS = {
    'id', 'pk',
    'created_at', 'updated_at', 'date_creation', 'date_modification',
    'last_login', 'date_joined',
    'password',
}

# Champs binaires → garder uniquement le nom du fichier.
FILE_FIELD_TYPES = ('FileField', 'ImageField')


def _serialize_value(value: Any) -> Any:
    """Convertit une valeur ORM en JSON-serialisable + label humain pour FK."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    # FK : retourne {id, label}
    if hasattr(value, 'pk') and hasattr(value, '_meta'):
        try:
            return {'id': value.pk, 'label': str(value)[:200]}
        except Exception:
            return {'id': value.pk, 'label': ''}
    # FileField : prendre name uniquement
    if hasattr(value, 'name'):
        return str(value.name)
    return str(value)[:500]


def snapshot_instance(instance) -> dict:
    """Retourne un dict {champ: valeur serialisable} d'une instance."""
    snapshot = {}
    for field in instance._meta.concrete_fields:
        name = field.name
        if name in BLACKLIST_FIELDS:
            continue
        if field.__class__.__name__ in FILE_FIELD_TYPES:
            value = getattr(instance, name, None)
            snapshot[name] = value.name if value and getattr(value, 'name', '') else None
            continue
        # FK : recuperer l'objet complet (pour avoir le label)
        if isinstance(field, (ForeignKey, OneToOneField)):
            value = getattr(instance, name, None)
            snapshot[name] = _serialize_value(value)
            continue
        value = getattr(instance, name, NOT_PROVIDED)
        if value is NOT_PROVIDED:
            continue
        snapshot[name] = _serialize_value(value)
    return snapshot


def compute_diff(old: dict | None, new: dict) -> dict:
    """Calcule {champ: {old, new}} pour les champs modifies."""
    diff = {}
    if old is None:
        # CREATE : on stocke toutes les valeurs comme 'new'
        for k, v in new.items():
            diff[k] = {'old': None, 'new': v}
        return diff
    for k, new_v in new.items():
        old_v = old.get(k)
        if old_v != new_v:
            diff[k] = {'old': old_v, 'new': new_v}
    return diff


def _resolve_institution(instance) -> int | None:
    """Tente de remonter l'institution_id depuis l'instance."""
    inst = getattr(instance, 'institution', None)
    if inst is not None:
        return getattr(inst, 'pk', None)
    inst_id = getattr(instance, 'institution_id', None)
    if inst_id:
        return inst_id
    return None


def write_audit(
    *,
    action: str,
    model_name: str,
    object_id: str,
    changes: dict | None = None,
    label: str = '',
    institution_id: int | None = None,
    keep_forever: bool = False,
) -> None:
    """Ecrit un AuditLog via transaction.on_commit (asynchrone, safe)."""
    ctx = get_request_context()

    def _do_write():
        try:
            from core.models import AuditLog
            AuditLog.objects.create(
                user        = ctx.get('user'),
                action      = action,
                model_name  = model_name,
                object_id   = str(object_id)[:50],
                changes     = changes or {},
                ip_address  = ctx.get('ip_address'),
                user_agent  = (ctx.get('user_agent') or '')[:1000],
                institution_id = institution_id,
                request_id  = ctx.get('request_id', '') or '',
                label       = (label or '')[:200],
                endpoint    = (ctx.get('endpoint') or '')[:200],
                http_method = ctx.get('http_method', '') or '',
                keep_forever = keep_forever,
            )
        except Exception as exc:
            logger.warning('AuditLog write failed for %s#%s: %s',
                           model_name, object_id, exc)

    # Si on est dans une transaction, defere l'ecriture au commit.
    # Sinon (ex: shell, signaux hors HTTP), ecrit immediatement.
    try:
        transaction.on_commit(_do_write)
    except Exception:
        _do_write()


def write_audit_safe(action: str, instance, changes: dict | None = None,
                     label: str = '', keep_forever: bool = False) -> None:
    """Variante haute-niveau : extrait model_name/pk/institution depuis l'instance."""
    if aggregate_active():
        return  # un @audit_aggregate parent prend en charge le bulk
    write_audit(
        action=action,
        model_name=instance.__class__.__name__,
        object_id=str(instance.pk),
        changes=changes,
        label=label,
        institution_id=_resolve_institution(instance),
        keep_forever=keep_forever,
    )


def write_audit_bulk(model_name: str, action: str, items: list,
                     label: str = '', request=None) -> None:
    """Ecrit N audit entries en BULK (asynchrone via on_commit, safe).

    `items` est une liste de dicts {object_id, changes, institution_id?}.

    Utilise quand l'ORM bulk_create/bulk_update ne declenche PAS les signals
    d'audit (Django limitation). Permet de combler ces trous avec un seul
    bulk_create AuditLog plutot que N write_audit individuels.

    Quand `request` est fourni et que request.user est authentifie (DRF JWT
    auth via cookie), on l'utilise prioritairement : la AuditMiddleware tourne
    AVANT DRF auth donc capture user=None dans le ctx thread-local.

    Defensif : toute exception est swallowed pour ne pas casser le flux metier.
    """
    if not items:
        return
    ctx = get_request_context()
    user = ctx.get('user')
    if request is not None:
        req_user = getattr(request, 'user', None)
        if req_user is not None and getattr(req_user, 'is_authenticated', False):
            user = req_user

    def _do():
        try:
            from core.models import AuditLog
            audit_objs = [
                AuditLog(
                    user           = user,
                    action         = action,
                    model_name     = model_name,
                    object_id      = str(it['object_id'])[:50],
                    changes        = it.get('changes') or {},
                    ip_address     = ctx.get('ip_address'),
                    user_agent     = (ctx.get('user_agent') or '')[:1000],
                    institution_id = it.get('institution_id'),
                    request_id     = ctx.get('request_id', '') or '',
                    label          = (label or '')[:200],
                    endpoint       = (ctx.get('endpoint') or '')[:200],
                    http_method    = ctx.get('http_method', '') or '',
                )
                for it in items
            ]
            AuditLog.objects.bulk_create(audit_objs, batch_size=500)
        except Exception as exc:
            logger.warning('Audit bulk write failed for %s (%d items): %s',
                           model_name, len(items), exc)

    try:
        transaction.on_commit(_do)
    except Exception:
        _do()


# ─────────────────────────────────────────────────────────────────────────────
# Decorateur @audit_aggregate
# ─────────────────────────────────────────────────────────────────────────────

@contextmanager
def audit_aggregate_block():
    """Context manager : desactive les signals individuels pendant le bloc."""
    was_active = aggregate_active()
    set_aggregate_active(True)
    try:
        yield
    finally:
        set_aggregate_active(was_active)


def audit_aggregate(label: str, action: str = 'BULK_UPDATE',
                    model_name: str = 'BulkOperation',
                    object_id: str = '0',
                    keep_forever: bool = False):
    """
    Decorateur : execute la fonction sans signals individuels, puis ecrit
    UN SEUL AuditLog avec le retour de la fonction comme `changes` (si dict).

    Usage :
        @audit_aggregate(label='Generation suivi', action='BULK_CREATE')
        def ajouter_suivie(self, request):
            ...
            return {'count_suivies': 200, ...}
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with audit_aggregate_block():
                result = fn(*args, **kwargs)
            # Extraction des stats : on cherche dict dans .data si DRF Response
            stats = {}
            if hasattr(result, 'data') and isinstance(result.data, dict):
                stats = {k: v for k, v in result.data.items()
                         if isinstance(v, (int, float, str, bool))}
            elif isinstance(result, dict):
                stats = {k: v for k, v in result.items()
                         if isinstance(v, (int, float, str, bool))}
            write_audit(
                action=action,
                model_name=model_name,
                object_id=str(object_id),
                changes=stats,
                label=label,
                institution_id=None,
                keep_forever=keep_forever,
            )
            return result
        return wrapper
    return decorator
