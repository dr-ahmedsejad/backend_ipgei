"""
Contexte d'audit thread-local — utilise par AuditMiddleware (HTTP) et
les signals (DB) pour transmettre les meta-donnees de la requete courante.

Aussi : registre _old_values pour pre_save (capture old) -> post_save (diff).
Aussi : flag _aggregate_active pour @audit_aggregate (skip signals naifs).
"""
import threading


_local = threading.local()


# ─────────────────────────────────────────────────────────────────────────────
# Contexte requete (set par middleware)
# ─────────────────────────────────────────────────────────────────────────────

def set_request_context(*, user=None, ip_address=None, user_agent='',
                        request_id='', endpoint='', http_method=''):
    _local.user        = user
    _local.ip_address  = ip_address
    _local.user_agent  = user_agent
    _local.request_id  = request_id
    _local.endpoint    = endpoint
    _local.http_method = http_method


def clear_request_context():
    for attr in ('user', 'ip_address', 'user_agent', 'request_id',
                 'endpoint', 'http_method'):
        if hasattr(_local, attr):
            delattr(_local, attr)


def get_request_context() -> dict:
    """Retourne un dict des meta-donnees, valeurs vides par defaut."""
    return {
        'user':        getattr(_local, 'user', None),
        'ip_address':  getattr(_local, 'ip_address', None),
        'user_agent':  getattr(_local, 'user_agent', ''),
        'request_id':  getattr(_local, 'request_id', ''),
        'endpoint':    getattr(_local, 'endpoint', ''),
        'http_method': getattr(_local, 'http_method', ''),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Old values (pre_save -> post_save)
# ─────────────────────────────────────────────────────────────────────────────

def stash_old_values(model_label: str, pk, values: dict) -> None:
    if not hasattr(_local, 'old_values'):
        _local.old_values = {}
    _local.old_values[(model_label, pk)] = values


def pop_old_values(model_label: str, pk) -> dict | None:
    store = getattr(_local, 'old_values', None)
    if not store:
        return None
    return store.pop((model_label, pk), None)


# ─────────────────────────────────────────────────────────────────────────────
# Agregation (decorateur @audit_aggregate)
# ─────────────────────────────────────────────────────────────────────────────

def aggregate_active() -> bool:
    return bool(getattr(_local, 'aggregate_active', False))


def set_aggregate_active(active: bool) -> None:
    _local.aggregate_active = active
