"""
RBAC service layer — thin controllers in views.py delegate here.
All database / cache logic lives in this module; views only handle
HTTP concerns (serialization, status codes, error responses).
"""
from __future__ import annotations

from django.contrib.auth import get_user_model

from core.permissions import _has_access, invalidate_user_rbac, invalidate_role_rbac
from ..models import Module, Action, ModuleAction, RoleDefault, UserPermission
from ..serializers import ModuleSerializer, ActionSerializer, UserSerializer

User = get_user_model()


# ── Role matrix (defaults page) ───────────────────────────────────────────────

def get_role_matrix() -> dict:
    """
    Returns the full role×action permission matrix.
    Presence of a RoleDefault record = permission granted.
    """
    modules = Module.objects.all().order_by('ordre').prefetch_related('actions__action')
    roles   = [r[0] for r in User._meta.get_field('role').choices]
    rd_set  = set(RoleDefault.objects.values_list('role', 'module_action_id'))

    matrix = []
    for mod in modules:
        row = {'module': ModuleSerializer(mod).data, 'actions': []}
        for ma in mod.actions.select_related('action'):
            action_row = {
                'id':     ma.id,
                'action': ActionSerializer(ma.action).data,
                'roles':  {role: (role, ma.id) in rd_set for role in roles},
            }
            row['actions'].append(action_row)
        matrix.append(row)

    return {'roles': roles, 'matrix': matrix}


# ── User permissions (sidebar-shaped page, single user) ───────────────────────

def get_user_permissions(user_id: int) -> dict | None:
    """
    Retourne l'ensemble des permissions effectives pour un user, indexees par
    (module_code, action_code). Format pense pour l'UI sidebar-shaped :

        {
          'user':        {...UserSerializer},
          'is_admin':    bool,
          'cells':       [{ma_id, module_code, action_code, state}, ...],
          'by_key':      { 'doc_attestation:modifier': {ma_id, state}, ... },
        }

    `state` ∈ {'admin', 'on', 'off', 'role', 'none'} (meme semantique que get_users_matrix).
    """
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return None

    is_admin_user = (user.role == 'admin' or user.is_superuser)
    module_actions = list(
        ModuleAction.objects
        .select_related('module', 'action')
        .order_by('module__ordre', 'action__code')
    )

    up_lookup = {
        up.module_action_id: up.allowed
        for up in UserPermission.objects.filter(user=user, departement__isnull=True)
    }
    rd_set = set(
        RoleDefault.objects
        .filter(role=user.role)
        .values_list('module_action_id', flat=True)
    )

    cells = []
    by_key: dict[str, dict] = {}
    for ma in module_actions:
        if is_admin_user:
            state = 'admin'
        else:
            explicit = up_lookup.get(ma.pk)
            if explicit is True:
                state = 'on'
            elif explicit is False:
                state = 'off'
            elif ma.pk in rd_set:
                state = 'role'
            else:
                state = 'none'
        cell = {
            'ma_id':       ma.pk,
            'module_code': ma.module.code,
            'action_code': ma.action.code,
            'state':       state,
        }
        cells.append(cell)
        by_key[f'{ma.module.code}:{ma.action.code}'] = {'ma_id': ma.pk, 'state': state}

    return {
        'user':     UserSerializer(user).data,
        'is_admin': is_admin_user,
        'cells':    cells,
        'by_key':   by_key,
    }


# ── Users matrix (permissions page, paginated) ────────────────────────────────

def get_users_matrix(page: int = 1, page_size: int = 20, search: str = '') -> dict:
    """
    Returns a paginated user×action permission matrix.

    Each cell state is one of: 'admin' | 'on' | 'off' | 'role' | 'none'.
    """
    qs = User.objects.order_by('username')
    if search:
        from django.db.models import Q
        qs = qs.filter(
            Q(username__icontains=search) |
            Q(name__icontains=search) |
            Q(email__icontains=search)
        )

    total      = qs.count()
    page       = max(1, page)
    page_size  = min(max(1, page_size), 100)   # clamp 1–100
    offset     = (page - 1) * page_size
    page_users = list(qs[offset: offset + page_size])

    module_actions = list(
        ModuleAction.objects
        .select_related('module', 'action')
        .order_by('module__ordre', 'action__code')
    )

    user_pks = [u.pk for u in page_users]

    # Only load UserPermission rows for users on this page
    up_lookup = {
        (up.user_id, up.module_action_id): up.allowed
        for up in UserPermission.objects.filter(
            user_id__in=user_pks,
            departement__isnull=True,
        )
    }
    rd_set = set(RoleDefault.objects.values_list('role', 'module_action_id'))

    rows = []
    for u in page_users:
        is_admin_user = (u.role == 'admin' or u.is_superuser)
        cells = []
        for ma in module_actions:
            if is_admin_user:
                state = 'admin'
            else:
                explicit = up_lookup.get((u.pk, ma.pk))
                role_def = (u.role, ma.pk) in rd_set
                if explicit is True:
                    state = 'on'
                elif explicit is False:
                    state = 'off'
                elif role_def:
                    state = 'role'
                else:
                    state = 'none'
            cells.append({'ma_id': ma.pk, 'state': state, 'module_code': ma.module.code})
        rows.append({'user': UserSerializer(u).data, 'cells': cells, 'is_admin': is_admin_user})

    ma_data = [
        {
            'id':     ma.pk,
            'module': {'id': ma.module.pk, 'code': ma.module.code, 'nom': ma.module.nom},
            'action': {'id': ma.action.pk, 'code': ma.action.code, 'nom': ma.action.nom},
        }
        for ma in module_actions
    ]

    pages = max(1, (total + page_size - 1) // page_size)
    return {
        'count':          total,
        'page':           page,
        'pages':          pages,
        'page_size':      page_size,
        'rows':           rows,
        'module_actions': ma_data,
    }


# ── Accessible modules for a user ─────────────────────────────────────────────

def get_user_modules(user) -> list[str]:
    """Returns list of module codes accessible to the user (action 'voir')."""
    if user.role == 'admin' or user.is_superuser:
        return list(Module.objects.order_by('ordre').values_list('code', flat=True))

    return [
        code
        for code in Module.objects.order_by('ordre').values_list('code', flat=True)
        if _has_access(user, code, 'voir')
    ]


def get_user_modules_with_actions(user) -> list[dict]:
    """
    Returns granular access list for the user :
        [{'code': 'emplois', 'actions': ['voir','modifier','supprimer']}, ...]

    Inclut UNIQUEMENT les modules ou l'utilisateur a au moins l'action 'voir'.
    Pour chaque module visible, liste les actions effectivement accessibles
    (resolution UserPermission > RoleDefault > deny).

    Format consomme par le frontend pour :
      - Filtrer la sidebar (action 'voir' minimum requise)
      - Cacher boutons modifier/supprimer/exporter selon droits
    """
    is_admin_bypass = user.role == 'admin' or user.is_superuser

    result: list[dict] = []
    modules = list(Module.objects.order_by('ordre'))
    actions_qs = list(Action.objects.all())

    if is_admin_bypass:
        # Admin = toutes les actions de tous les modules
        for m in modules:
            result.append({
                'code': m.code,
                'actions': [a.code for a in actions_qs],
            })
        return result

    # Resolution non-admin : check action par action via _has_access (cache versionne)
    for m in modules:
        # Actions effectivement liees au module via ModuleAction (sinon inutile de checker)
        ma_actions = (
            ModuleAction.objects
            .filter(module=m)
            .select_related('action')
            .values_list('action__code', flat=True)
        )
        allowed = [
            a_code for a_code in ma_actions
            if _has_access(user, m.code, a_code)
        ]
        # On expose le module seulement si 'voir' est accessible (sinon il n'a pas a etre visible)
        if 'voir' in allowed:
            result.append({'code': m.code, 'actions': allowed})

    return result


# ── Toggle helpers ─────────────────────────────────────────────────────────────

def toggle_user_permission(user, module_action, state: str) -> str:
    """
    Apply a permission toggle for a specific user.

    state: 'on' | 'off' | 'role' (remove override)
    Returns the new state string: 'admin' | 'on' | 'off' | 'role' | 'none'.
    """
    if user.role == 'admin' or user.is_superuser:
        return 'admin'

    if state == 'role':
        UserPermission.objects.filter(
            user=user, module_action=module_action, departement__isnull=True
        ).delete()
    else:
        UserPermission.objects.update_or_create(
            user=user, module_action=module_action, departement=None,
            defaults={'allowed': state == 'on'},
        )

    try:
        up = UserPermission.objects.get(user=user, module_action=module_action, departement__isnull=True)
        new_state = 'on' if up.allowed else 'off'
    except UserPermission.DoesNotExist:
        has_rd    = RoleDefault.objects.filter(role=user.role, module_action=module_action).exists()
        new_state = 'role' if has_rd else 'none'

    invalidate_user_rbac(user.pk)
    return new_state


def toggle_role_permission(role: str, module_action) -> bool:
    """
    Toggle a role-level default permission (presence = allowed, absence = denied).
    Returns True if the permission is now active, False if removed.
    """
    rd, created = RoleDefault.objects.get_or_create(
        role=role, module_action=module_action, defaults={'allowed': True}
    )
    if not created:
        rd.delete()
        active = False
    else:
        active = True

    invalidate_role_rbac(role)
    return active
