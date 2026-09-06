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
                        request_id='', endpoint='', http_method='', request=None):
    _local.user        = user
    _local.request     = request
    _local.ip_address  = ip_address
    _local.user_agent  = user_agent
    _local.request_id  = request_id
    _local.endpoint    = endpoint
    _local.http_method = http_method


def clear_request_context():
    for attr in ('user', 'request', 'ip_address', 'user_agent', 'request_id',
                 'endpoint', 'http_method', 'mdp_traces'):
        if hasattr(_local, attr):
            delattr(_local, attr)


def _auteur():
    """
    Qui agit — resolu AU MOMENT DE L'APPEL, pas a l'entree du middleware.

    `AuditMiddleware` tourne apres `AuthenticationMiddleware`, ce qui suffit a
    l'authentification par SESSION. Mais l'API est en JWT, et DRF n'authentifie
    qu'a l'entree de la vue : au passage du middleware, `request.user` est
    encore anonyme. Lu trop tot, l'auteur manquait sur 4 lignes sur 5.

    DRF propage l'utilisateur vers la requete Django sous-jacente, donc relire
    `request.user` plus tard donne le bon compte.
    """
    connu = getattr(_local, 'user', None)
    if connu is not None:
        return connu
    requete = getattr(_local, 'request', None)
    if requete is None:
        return None
    tardif = getattr(requete, 'user', None)
    if tardif is None or not getattr(tardif, 'is_authenticated', False):
        return None
    _local.user = tardif                       # une seule resolution par requete
    return tardif


def get_request_context() -> dict:
    """Retourne un dict des meta-donnees, valeurs vides par defaut."""
    return {
        'user':        _auteur(),
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


# ─────────────────────────────────────────────────────────────────────────────
# Mot de passe : une vue a-t-elle deja trace le changement ?
# ─────────────────────────────────────────────────────────────────────────────
#
# Le champ `password` est dans BLACKLIST_FIELDS : le diff generique ne peut donc
# JAMAIS voir un changement de mot de passe. Un filet au niveau du signal le
# rattrape — mais il ne doit pas doubler la trace des vues qui, elles, savent
# dire dans quel contexte le changement s'est produit (premier acces…).

def marquer_mdp_trace(pk) -> None:
    """La vue courante a deja ecrit une trace pour ce compte : le signal passe."""
    if not hasattr(_local, 'mdp_traces'):
        _local.mdp_traces = set()
    _local.mdp_traces.add(pk)


def mdp_deja_trace(pk) -> bool:
    return pk in getattr(_local, 'mdp_traces', ())
