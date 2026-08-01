from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    LoginView, LogoutView, CookieTokenRefreshView,
    MeView, ProfilView, ChangePasswordView, FirstLoginView,
    ContexteView, MesModulesView,
    UserViewSet, ModuleViewSet,
    RBACMatrixView, TogglePermissionView, UserPermissionsView, UserPermissionsTreeView,
    UsersMatrixView, UserToggleView, RoleToggleView,
    LockedAttemptsView,
    EDTDelegationMatrixView, EDTDelegationToggleView, EDTDelegationRollbackView,
)

router = DefaultRouter()
router.register('users',   UserViewSet,   basename='users')
router.register('modules', ModuleViewSet, basename='modules')

urlpatterns = [
    # Auth
    path('login/',           LoginView.as_view(),              name='login'),
    path('logout/',          LogoutView.as_view(),             name='logout'),
    path('token/refresh/',   CookieTokenRefreshView.as_view(), name='token-refresh'),

    # Session silencieuse
    path('me/',              MeView.as_view(),                 name='me'),

    # Profil
    path('profil/',          ProfilView.as_view(),             name='profil'),
    path('change-password/', ChangePasswordView.as_view(),     name='change-password'),
    path('first-login/',     FirstLoginView.as_view(),         name='first-login'),

    # Contexte métier (année universitaire + semestre)
    path('contexte/',        ContexteView.as_view(),           name='contexte'),
    path('mes-modules/',     MesModulesView.as_view(),         name='mes-modules'),

    # Blocages axes
    path('locked-attempts/', LockedAttemptsView.as_view(), name='locked-attempts'),

    # RBAC
    path('rbac/matrix/',        RBACMatrixView.as_view(),        name='rbac-matrix'),
    path('rbac/toggle/',        TogglePermissionView.as_view(),  name='rbac-toggle'),
    path('rbac/users-matrix/',  UsersMatrixView.as_view(),       name='rbac-users-matrix'),
    path('rbac/user-toggle/',   UserToggleView.as_view(),        name='rbac-user-toggle'),
    path('rbac/role-toggle/',   RoleToggleView.as_view(),        name='rbac-role-toggle'),
    path('rbac/user/<int:user_id>/permissions/', UserPermissionsView.as_view(),     name='user-permissions'),
    path('rbac/user-permissions/',                UserPermissionsTreeView.as_view(), name='user-permissions-tree'),

    # Delegation EDT (matrice user x departement, admin-only)
    path('edt-delegation/matrix/',   EDTDelegationMatrixView.as_view(),   name='edt-delegation-matrix'),
    path('edt-delegation/toggle/',   EDTDelegationToggleView.as_view(),   name='edt-delegation-toggle'),
    path('edt-delegation/rollback/', EDTDelegationRollbackView.as_view(), name='edt-delegation-rollback'),

    # Router (users CRUD + modules)
    path('', include(router.urls)),
]
