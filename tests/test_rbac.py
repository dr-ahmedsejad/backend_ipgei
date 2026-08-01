"""
Tests du systeme RBAC (Module x Action) — regression tests.

Bug initial documente :
  Le user 'test' (role DE) avait emplois.modifier=allowed mais ne pouvait
  pas ouvrir le modal d'edition (403). Cause : RBACPermission OK mais 14+
  ViewSets utilisaient required_module sans le declarer -> fallback "True".

Couvre :
  - admin / superuser bypassent toujours
  - role 'admin' bypasse aussi (sans is_superuser)
  - UserPermission explicite override RoleDefault
  - RoleDefault s'applique si pas d'override
  - sans permission -> 403
  - SelectAllMixin custom action 'all' resolue en 'voir'
  - cache RBAC ne casse pas le determinisme
"""
import pytest
from unittest.mock import MagicMock

from core.permissions import RBACPermission, _has_access, _compute_access, ACTION_MAP
from tests.factories.auth import (
    UserFactory, AdminUserFactory, DEUserFactory,
    EtudiantUserFactory, EnseignantUserFactory,
    ModuleRBACFactory, ActionRBACFactory, ModuleActionRBACFactory,
    RoleDefaultFactory, UserPermissionFactory,
)


def _mock_view(action, required_module=None):
    """Mock minimal d'un ViewSet pour tester RBACPermission."""
    v = MagicMock()
    v.action = action
    if required_module is None:
        # Simule l'absence d'attribut required_module
        if hasattr(v, 'required_module'):
            del v.required_module
    else:
        v.required_module = required_module
    return v


def _mock_request(user):
    r = MagicMock()
    r.user = user
    return r


# ── Bypass admin ───────────────────────────────────────────────────────────────
class TestBypassAdmin:

    def test_superuser_passe_tout(self, db):
        user = AdminUserFactory()
        perm = RBACPermission()
        view = _mock_view('list', required_module='emplois')

        assert perm.has_permission(_mock_request(user), view) is True

    def test_role_admin_passe_meme_sans_superuser(self, db):
        user = UserFactory(role='admin')
        perm = RBACPermission()
        view = _mock_view('destroy', required_module='emplois')

        assert perm.has_permission(_mock_request(user), view) is True

    def test_user_non_authentifie_refuse(self, db):
        user = MagicMock()
        user.is_authenticated = False
        perm = RBACPermission()
        view = _mock_view('list', required_module='emplois')

        assert perm.has_permission(_mock_request(user), view) is False


# ── ACTION_MAP : action DRF -> action RBAC ─────────────────────────────────────
class TestActionMap:

    def test_action_map_contient_les_5_actions_standard(self):
        assert ACTION_MAP['list']           == 'voir'
        assert ACTION_MAP['retrieve']       == 'voir'
        assert ACTION_MAP['create']         == 'modifier'
        assert ACTION_MAP['update']         == 'modifier'
        assert ACTION_MAP['partial_update'] == 'modifier'
        assert ACTION_MAP['destroy']        == 'supprimer'
        assert ACTION_MAP['export']         == 'exporter'

    def test_action_inconnue_fallback_sur_voir(self, db):
        """SelectAllMixin custom 'all' doit etre traite comme 'voir' (par defaut)."""
        user = DEUserFactory()
        # Cree module emplois.voir + autorisation pour DE
        ma = ModuleActionRBACFactory(
            module=ModuleRBACFactory(code='emplois'),
            action=ActionRBACFactory(code='voir'),
        )
        RoleDefaultFactory(role='DE', module_action=ma, allowed=True)

        perm = RBACPermission()
        # Action 'all' (SelectAllMixin) -> ACTION_MAP fallback 'voir'
        view = _mock_view('all', required_module='emplois')
        assert perm.has_permission(_mock_request(user), view) is True


# ── required_module manquant -> False (fail-closed, durci 2026-06) ────────────────────
class TestRequiredModuleManquant:
    """Securite (fail-closed) : sans `required_module` declare sur la vue,
    RBACPermission REFUSE (return False) et log un warning. Une vue qui oublie
    de declarer son module est bloquee, pas ouverte — durcissement de l'ancien
    comportement permissif (audit 2026-05-16 / remediation 2026-06-10)."""

    def test_sans_required_module_renvoie_false(self, db):
        user = DEUserFactory()
        view = _mock_view('list', required_module=None)

        perm = RBACPermission()
        # Fail-closed : aucun module declare -> acces refuse
        assert perm.has_permission(_mock_request(user), view) is False


# ── _compute_access : resolution UserPermission vs RoleDefault ─────────────────
class TestComputeAccess:

    def test_module_inexistant_renvoie_false(self, db):
        user = DEUserFactory()
        result = _compute_access(user, 'inexistant', 'voir')
        assert result is False

    def test_user_permission_explicite_allow(self, db):
        user = DEUserFactory()
        ma = ModuleActionRBACFactory(
            module=ModuleRBACFactory(code='emplois'),
            action=ActionRBACFactory(code='modifier'),
        )
        UserPermissionFactory(user=user, module_action=ma, allowed=True)

        assert _compute_access(user, 'emplois', 'modifier') is True

    def test_user_permission_explicite_deny_override_role(self, db):
        """UserPermission allowed=False prime sur RoleDefault allowed=True."""
        user = DEUserFactory()
        ma = ModuleActionRBACFactory(
            module=ModuleRBACFactory(code='emplois'),
            action=ActionRBACFactory(code='modifier'),
        )
        RoleDefaultFactory(role='DE', module_action=ma, allowed=True)
        UserPermissionFactory(user=user, module_action=ma, allowed=False)

        assert _compute_access(user, 'emplois', 'modifier') is False

    def test_role_default_applique_sans_user_permission(self, db):
        user = DEUserFactory()
        ma = ModuleActionRBACFactory(
            module=ModuleRBACFactory(code='suivi'),
            action=ActionRBACFactory(code='voir'),
        )
        RoleDefaultFactory(role='DE', module_action=ma, allowed=True)

        assert _compute_access(user, 'suivi', 'voir') is True

    def test_aucun_default_ni_user_perm_renvoie_false(self, db):
        user = DEUserFactory()
        ModuleActionRBACFactory(
            module=ModuleRBACFactory(code='evaluations_delib'),
            action=ActionRBACFactory(code='supprimer'),
        )
        # Pas de RoleDefault, pas de UserPermission

        assert _compute_access(user, 'evaluations_delib', 'supprimer') is False

    def test_role_default_deny_explicite(self, db):
        """RoleDefault avec allowed=False -> refuse."""
        user = DEUserFactory()
        ma = ModuleActionRBACFactory(
            module=ModuleRBACFactory(code='admin_users'),
            action=ActionRBACFactory(code='voir'),
        )
        RoleDefaultFactory(role='DE', module_action=ma, allowed=False)

        assert _compute_access(user, 'admin_users', 'voir') is False


# ── Regression bug DE : DE avec emplois.modifier doit pouvoir editer ───────────
class TestRegressionBugDE:
    """Bug initial : DE avec emplois.modifier=True ne pouvait pas ouvrir le modal."""

    def test_de_avec_modifier_peut_modifier(self, db):
        user = DEUserFactory()
        ma = ModuleActionRBACFactory(
            module=ModuleRBACFactory(code='emplois'),
            action=ActionRBACFactory(code='modifier'),
        )
        UserPermissionFactory(user=user, module_action=ma, allowed=True)

        perm = RBACPermission()
        view = _mock_view('partial_update', required_module='emplois')
        assert perm.has_permission(_mock_request(user), view) is True

    def test_de_sans_modifier_refuse_patch(self, db):
        user = DEUserFactory()
        # Module/action existent mais pas de permission accordee
        ModuleActionRBACFactory(
            module=ModuleRBACFactory(code='emplois'),
            action=ActionRBACFactory(code='modifier'),
        )

        perm = RBACPermission()
        view = _mock_view('partial_update', required_module='emplois')
        assert perm.has_permission(_mock_request(user), view) is False

    def test_de_avec_voir_seulement_peut_lister_pas_modifier(self, db):
        user = DEUserFactory()
        m_emplois = ModuleRBACFactory(code='emplois')
        ma_voir = ModuleActionRBACFactory(
            module=m_emplois, action=ActionRBACFactory(code='voir'),
        )
        ma_mod  = ModuleActionRBACFactory(
            module=m_emplois, action=ActionRBACFactory(code='modifier'),
        )
        UserPermissionFactory(user=user, module_action=ma_voir, allowed=True)
        # Pas de UserPermission pour 'modifier' -> denied par defaut

        perm = RBACPermission()
        view_list = _mock_view('list',           required_module='emplois')
        view_edit = _mock_view('partial_update', required_module='emplois')

        assert perm.has_permission(_mock_request(user), view_list) is True
        assert perm.has_permission(_mock_request(user), view_edit) is False


# ── Consistance entre _has_access et RBACPermission ────────────────────────────
class TestConsistance:

    def test_has_access_meme_resultat_que_compute(self, db):
        """Le cache versionne ne doit pas modifier le resultat (juste accelerer)."""
        user = DEUserFactory()
        ma = ModuleActionRBACFactory(
            module=ModuleRBACFactory(code='vacations'),
            action=ActionRBACFactory(code='voir'),
        )
        RoleDefaultFactory(role='DE', module_action=ma, allowed=True)

        # 2 appels successifs : doivent renvoyer la meme chose
        r1 = _has_access(user, 'vacations', 'voir')
        r2 = _has_access(user, 'vacations', 'voir')
        r3 = _compute_access(user, 'vacations', 'voir')

        assert r1 == r2 == r3 is True
