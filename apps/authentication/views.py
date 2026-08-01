import logging
from datetime import datetime

from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView
from rest_framework_simplejwt.exceptions import TokenError

from core.permissions import IsAdmin, IsAdminOrIT, IsOwnerOrAdmin
from .services.rbac_service import (
    get_role_matrix, get_users_matrix, get_user_modules,
    get_user_permissions,
    toggle_user_permission, toggle_role_permission,
)
from core.throttles import LoginRateThrottle, SensitiveEndpointThrottle, AdminActionThrottle
from .models import CustomUser, Module, ModuleAction, UserContexte, UserPermission, RoleDefault
from .serializers import (
    SIGATokenObtainPairSerializer, UserSerializer, UserCreateSerializer,
    UserUpdateSerializer, ProfilUpdateSerializer, ChangePasswordSerializer,
    ModuleSerializer,
    UserPermissionSerializer, PermissionToggleSerializer,
    UserToggleSerializer, RoleToggleSerializer,
    UserContexteSerializer,
)

logger = logging.getLogger('siga')
User = get_user_model()

JWT_CONF = settings.SIMPLE_JWT


def _set_auth_cookies(response, access_token, refresh_token):
    """Pose les cookies HttpOnly JWT sur la réponse."""
    secure   = JWT_CONF.get('AUTH_COOKIE_SECURE', False)
    samesite = JWT_CONF.get('AUTH_COOKIE_SAMESITE', 'Lax')

    response.set_cookie(
        JWT_CONF.get('AUTH_COOKIE', 'access_token'),
        str(access_token),
        httponly=True, secure=secure, samesite=samesite, path='/',
        max_age=int(JWT_CONF['ACCESS_TOKEN_LIFETIME'].total_seconds()),
    )
    response.set_cookie(
        JWT_CONF.get('AUTH_COOKIE_REFRESH', 'refresh_token'),
        str(refresh_token),
        httponly=True, secure=secure, samesite=samesite, path='/',
        max_age=int(JWT_CONF['REFRESH_TOKEN_LIFETIME'].total_seconds()),
    )


# ── Login ─────────────────────────────────────────────────────────────────────
class LoginView(generics.GenericAPIView):
    """
    POST /api/v1/auth/login/
    Protection double couche :
      - Couche 1 (DRF)   : LoginRateThrottle → 5 req/15min par IP (avant toute DB)
      - Couche 2 (axes)  : 5 échecs → blocage IP 15 min (persisté en DB, réponse JSON
                           via AXES_LOCKOUT_CALLABLE=core.axes_utils.axes_lockout_callback)

    Note axes 8 : quand l'IP est déjà bloquée, AxesMiddleware court-circuite la requête
    AVANT que ce post() soit appelé → AXES_LOCKOUT_CALLABLE gère ce cas.
    La vérification request.axes_locked_out ici est une sécurité supplémentaire.
    """
    serializer_class   = SIGATokenObtainPairSerializer
    permission_classes = [AllowAny]
    throttle_classes   = [LoginRateThrottle]

    def post(self, request):
        ip = request.META.get('REMOTE_ADDR', '?')

        # Garde-fou : axes_locked_out peut être mis par le middleware si configuré ainsi
        if getattr(request, 'axes_locked_out', False):
            logger.warning('Login blocked (axes_locked_out) for IP=%s', ip)
            return Response(
                {'error': 'Trop de tentatives échouées. Accès bloqué pendant 15 minutes.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except Exception as exc:
            # Quand axes verrouille au Nème échec, Django's authenticate() avale
            # AxesBackendPermissionDenied et retourne None → SimpleJWT lève
            # AuthenticationFailed. On détecte ce cas via axes.utils.is_already_locked.
            try:
                from axes.utils import is_already_locked
                if is_already_locked(request):
                    logger.warning('Login blocked (is_already_locked) for IP=%s', ip)
                    return Response(
                        {'error': 'Trop de tentatives échouées. Accès bloqué pendant 15 minutes.'},
                        status=status.HTTP_429_TOO_MANY_REQUESTS,
                    )
            except Exception:
                pass

            logger.warning('Login failed for username=%s IP=%s error=%s',
                           request.data.get('username'), ip, type(exc).__name__)
            raise

        data     = serializer.validated_data
        response = Response({'user': data.pop('user')}, status=status.HTTP_200_OK)
        _set_auth_cookies(response, data['access'], data['refresh'])
        logger.info('Login success for username=%s IP=%s', request.data.get('username'), ip)
        return response


# ── Logout ────────────────────────────────────────────────────────────────────
class LogoutView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.COOKIES.get(JWT_CONF.get('AUTH_COOKIE_REFRESH', 'refresh_token'))
        if refresh_token:
            try:
                token = RefreshToken(refresh_token)
                token.blacklist()
            except TokenError:
                pass

        response = Response({'detail': 'Déconnecté avec succès.'}, status=status.HTTP_200_OK)
        response.delete_cookie(JWT_CONF.get('AUTH_COOKIE', 'access_token'))
        response.delete_cookie(JWT_CONF.get('AUTH_COOKIE_REFRESH', 'refresh_token'))
        return response


# ── Refresh ───────────────────────────────────────────────────────────────────
class CookieTokenRefreshView(TokenRefreshView):
    """Refresh depuis le cookie HttpOnly."""
    permission_classes = [AllowAny]
    throttle_classes   = [LoginRateThrottle]

    def post(self, request, *args, **kwargs):
        refresh = request.COOKIES.get(JWT_CONF.get('AUTH_COOKIE_REFRESH', 'refresh_token'))
        if not refresh:
            return Response({'error': 'Token de refresh absent.'}, status=status.HTTP_400_BAD_REQUEST)

        # Injecter le refresh token sans modifier le QueryDict — on écrase _full_data directement
        request._full_data = {'refresh': refresh}

        response = super().post(request, *args, **kwargs)
        if response.status_code == 200:
            _set_auth_cookies(response, response.data['access'], response.data.get('refresh', refresh))
            response.data = {'detail': 'Token rafraîchi.'}
        return response


# ── Profil ────────────────────────────────────────────────────────────────────
class ProfilView(generics.RetrieveUpdateAPIView):
    # Self-service : sérialiseur SANS `role` (anti-élévation de privilège).
    serializer_class   = ProfilUpdateSerializer
    permission_classes = [IsAuthenticated]
    parser_classes     = [MultiPartParser, FormParser, JSONParser]

    def get_object(self):
        return self.request.user

    def get_serializer_class(self):
        return ProfilUpdateSerializer

    def retrieve(self, request, *args, **kwargs):
        return Response(UserSerializer(request.user).data)

    def update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        serializer = self.get_serializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        # Retourner le user complet (avec username, id, etc.)
        return Response(UserSerializer(request.user).data)


# ── Changement de mot de passe ────────────────────────────────────────────────
class ChangePasswordView(generics.UpdateAPIView):
    serializer_class   = ChangePasswordSerializer
    permission_classes = [IsAuthenticated]
    throttle_classes   = [SensitiveEndpointThrottle]
    http_method_names  = ['post']

    def post(self, request):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        request.user.set_password(serializer.validated_data['new_password'])
        request.user.save()
        logger.info('Password changed for user=%s', request.user.username)
        return Response({'detail': 'Mot de passe modifié avec succès.'})


# ── Premier accès étudiant (changement de mot de passe obligatoire) ───────────
class FirstLoginView(generics.GenericAPIView):
    """
    POST /api/v1/auth/first-login/
    Réservé aux utilisateurs avec doit_changer_mdp=True.
    Change le mot de passe, met à jour le username CNI → matricule,
    et retourne le nouveau username (matricule) pour information.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes   = [SensitiveEndpointThrottle]

    def post(self, request):
        user = request.user
        if not user.doit_changer_mdp:
            return Response(
                {'detail': 'Aucun changement de mot de passe requis.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        new_password     = request.data.get('new_password', '').strip()
        confirm_password = request.data.get('confirm_password', '').strip()

        if not new_password:
            return Response({'detail': 'Le nouveau mot de passe est obligatoire.'}, status=status.HTTP_400_BAD_REQUEST)
        if new_password != confirm_password:
            return Response({'detail': 'Les mots de passe ne correspondent pas.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(new_password) < 8:
            return Response({'detail': 'Le mot de passe doit contenir au moins 8 caractères.'}, status=status.HTTP_400_BAD_REQUEST)

        # Mettre à jour le mot de passe
        user.set_password(new_password)
        user.doit_changer_mdp = False

        # Changer le username CNI → matricule
        # 1. Si le profil est déjà lié (OneToOne)
        # 2. Sinon, chercher l'Etudiant dont le cni correspond au username courant
        nouveau_username = user.username
        try:
            etudiant = user.etudiant_profile
        except Exception:
            etudiant = None

        if etudiant is None:
            # Recherche par CNI : le username initial de l'étudiant EST son CNI
            try:
                from apps.absence.models import Etudiant as EtudiantModel
                etudiant = EtudiantModel.objects.filter(cni=user.username).first()
                if etudiant:
                    etudiant.user = user   # Lier le profil pour les prochaines requêtes
                    etudiant.save(update_fields=['user'])
            except Exception:
                etudiant = None

        if etudiant and etudiant.matricule:
            nouveau_username = etudiant.matricule
            user.username    = etudiant.matricule

        user.save()
        logger.info('First login password changed for user=%s → new_username=%s', user.pk, nouveau_username)
        return Response({
            'detail':          'Mot de passe modifié avec succès.',
            'nouveau_username': nouveau_username,
        })


# ── Contexte (année universitaire + semestre) ─────────────────────────────────
class ContexteView(generics.GenericAPIView):
    """
    GET  → retourne le contexte actif de l'utilisateur (année + semestre).
    PATCH → met à jour le contexte sans nécessiter une re-connexion.
    """
    serializer_class   = UserContexteSerializer
    permission_classes = [IsAuthenticated]

    def get(self, request):
        contexte, _ = UserContexte.objects.get_or_create(user=request.user)
        return Response(UserContexteSerializer(contexte).data)

    def patch(self, request):
        contexte, _ = UserContexte.objects.get_or_create(user=request.user)
        serializer = UserContexteSerializer(contexte, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserContexteSerializer(contexte).data)


# ── Me (validation silencieuse de session + rebuild user) ────────────────────
class MeView(generics.GenericAPIView):
    """
    GET /api/v1/auth/me/
    Retourne l'utilisateur connecté avec son contexte (annee + semestre).
    Utilisé par le frontend pour reconstruire la session après F5 ou nouvel onglet
    sans demander un re-login si le cookie JWT est encore valide.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        contexte, _ = UserContexte.objects.get_or_create(user=user)
        data = {
            'id':                  user.pk,
            'username':            user.username,
            'name':                user.name or user.get_full_name(),
            'email':               user.email,
            'role':                user.role,
            'avatar':              user.avatar.url if user.avatar else None,
            'annee_universitaire': contexte.annee_universitaire,
            'semestre':            contexte.semestre,
            'doit_changer_mdp':    user.doit_changer_mdp,
        }

        if user.role == 'etudiant':
            etudiant = None
            try:
                etudiant = user.etudiant_profile
            except Exception:
                try:
                    from apps.absence.models import Etudiant as EtudiantModel
                    etudiant = EtudiantModel.objects.filter(cni=user.username).first()
                except Exception:
                    pass
            if etudiant:
                data['etudiant_id'] = etudiant.pk
                data['matricule']   = etudiant.matricule
            else:
                data['etudiant_id'] = None
                data['matricule']   = None

        if user.role == 'enseignant':
            try:
                prof = user.prof_profile
                data['prof_id']   = prof.pk
                data['prof_nom']  = prof.nom
                data['prof_type'] = prof.type
            except Exception:
                data['prof_id']   = None
                data['prof_nom']  = None
                data['prof_type'] = None

        return Response(data)


# ── Modules accessibles à l'utilisateur connecté ──────────────────────────────
class MesModulesView(generics.GenericAPIView):
    """
    Retourne les modules + actions accessibles a l'utilisateur courant.

    Format etendu (par defaut) :
        {
          "modules":         [{"code": "emplois", "actions": ["voir","modifier","supprimer"]}, ...],
          "modules_legacy":  ["emplois", "profs", ...]   # liste plate, retro-compat
        }

    Le champ `modules_legacy` permet aux anciens clients (v1) de continuer a
    fonctionner pendant que le frontend bascule sur le format granulaire.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.authentication.services.rbac_service import (
            get_user_modules_with_actions,
        )
        granular = get_user_modules_with_actions(request.user)
        legacy   = [m['code'] for m in granular]
        return Response({
            'modules':        granular,
            'modules_legacy': legacy,
        })


# ── Users (Admin) ─────────────────────────────────────────────────────────────
class UserViewSet(viewsets.ModelViewSet):
    queryset           = CustomUser.objects.all().order_by('username')
    permission_classes = [IsAdmin]
    filterset_fields   = ['role', 'is_active']
    search_fields      = ['username', 'name', 'email']

    def get_permissions(self):
        # L'action unblock est accessible à admin + IT (même périmètre que LockedAttemptsView)
        if self.action == 'unblock':
            return [IsAdminOrIT()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action == 'create':
            return UserCreateSerializer
        if self.action in ('update', 'partial_update'):
            return UserUpdateSerializer
        return UserSerializer

    @action(detail=True, methods=['post'], url_path='toggle-active')
    def toggle_active(self, request, pk=None):
        user = self.get_object()
        user.is_active = not user.is_active
        user.save()
        return Response({'is_active': user.is_active})

    @action(detail=False, methods=['post'], url_path='unblock')
    def unblock(self, request):
        """
        Débloquer un compte ou une IP.
        Réinitialise les deux couches de protection :
          - Axes (DB AccessAttempt)
          - LoginRateThrottle (cache DRF clé throttle_login_<ip>)
        Body : { "username": "..." } | { "ip": "..." } | { "all": true }
        """
        from axes.utils import reset
        from axes.models import AccessAttempt
        from django.core.cache import cache

        username  = request.data.get('username')
        ip        = request.data.get('ip')
        reset_all = request.data.get('all', False)

        def _clear_throttle(ip_addr: str):
            """Supprime le compteur DRF LoginRateThrottle pour cette IP."""
            if ip_addr:
                cache.delete(f'throttle_login_{ip_addr}')

        if reset_all:
            # Récupérer toutes les IPs avant de tout effacer
            ips = list(AccessAttempt.objects.values_list('ip_address', flat=True).distinct())
            reset()
            for ip_addr in ips:
                _clear_throttle(ip_addr)
            logger.info('All axes+throttle lockouts reset by admin=%s', request.user.username)
            return Response({'detail': 'Tous les blocages réinitialisés.'})

        if not username and not ip:
            return Response({'error': 'username, ip ou all:true requis.'}, status=400)

        if ip:
            reset(ip=ip)
            _clear_throttle(ip)
            logger.info('Axes+throttle reset for ip=%s by admin=%s', ip, request.user.username)

        if username:
            # Trouver l'IP associée au username dans axes avant de reset
            ips_for_user = list(
                AccessAttempt.objects.filter(username=username)
                .values_list('ip_address', flat=True).distinct()
            )
            reset(username=username)
            for ip_addr in ips_for_user:
                _clear_throttle(ip_addr)
            logger.info('Axes+throttle reset for username=%s by admin=%s', username, request.user.username)

        target = username or ip
        return Response({'detail': f'{target} débloqué(e).'})


# ── Tentatives de connexion bloquées ─────────────────────────────────────────
class LockedAttemptsView(generics.GenericAPIView):
    """
    GET /api/v1/auth/locked-attempts/
    Retourne les IPs/comptes actuellement bloqués par axes
    (failures >= AXES_FAILURE_LIMIT et dans la fenêtre de cooloff).
    """
    permission_classes = [IsAdminOrIT]

    def get(self, request):
        from axes.models import AccessAttempt
        from django.utils import timezone
        from datetime import timedelta

        limit         = getattr(settings, 'AXES_FAILURE_LIMIT', 5)
        cooloff_td    = getattr(settings, 'AXES_COOLOFF_TIME', timedelta(minutes=15))
        cooloff       = cooloff_td.total_seconds() if isinstance(cooloff_td, timedelta) else cooloff_td * 3600
        since         = timezone.now() - timedelta(seconds=cooloff)

        attempts = (
            AccessAttempt.objects
            .filter(failures_since_start__gte=limit, attempt_time__gte=since)
            .order_by('-attempt_time')
        )

        # Préchargement en une requête pour éviter les N+1
        usernames = [a.username for a in attempts if a.username]
        users_map = {
            u.username: u
            for u in User.objects.filter(username__in=usernames)
        }

        now     = timezone.now()
        results = []
        for a in attempts:
            user_info = None
            if a.username and a.username in users_map:
                u = users_map[a.username]
                user_info = {
                    'id':        u.pk,
                    'username':  u.username,
                    'name':      u.name or u.get_full_name(),
                    'role':      u.role,
                    'is_active': u.is_active,
                }

            # Temps restant avant déblocage automatique
            elapsed   = (now - a.attempt_time).total_seconds()
            remaining = max(0, int(cooloff - elapsed))

            results.append({
                'ip_address': a.ip_address,
                'username':   a.username,
                'failures':   a.failures_since_start,
                'locked_at':  a.attempt_time,
                'remaining_seconds': remaining,
                'user':       user_info,
            })

        return Response(results)


# ── RBAC ──────────────────────────────────────────────────────────────────────
class ModuleViewSet(viewsets.ReadOnlyModelViewSet):
    queryset           = Module.objects.all().order_by('ordre')
    serializer_class   = ModuleSerializer
    permission_classes = [IsAuthenticated]
    pagination_class   = None


class RBACMatrixView(generics.GenericAPIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        return Response(get_role_matrix())


class TogglePermissionView(generics.GenericAPIView):
    permission_classes = [IsAdmin]
    serializer_class   = PermissionToggleSerializer

    def post(self, request):
        s = self.get_serializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        try:
            ma = ModuleAction.objects.get(pk=d['module_action_id'])
        except ModuleAction.DoesNotExist:
            return Response({'error': 'ModuleAction introuvable.'}, status=404)

        if 'user_id' in d:
            user = User.objects.get(pk=d['user_id'])
            obj, _ = UserPermission.objects.update_or_create(
                user=user, module_action=ma,
                defaults={'allowed': d['allowed']},
            )
        elif 'role' in d:
            obj, _ = RoleDefault.objects.update_or_create(
                role=d['role'], module_action=ma,
                defaults={'allowed': d['allowed']},
            )
        else:
            return Response({'error': 'user_id ou role requis.'}, status=400)

        try:
            from core.audit_helpers import write_audit
            cible = f"user#{d['user_id']}" if 'user_id' in d else f"role:{d['role']}"
            write_audit(
                action='UPDATE',
                model_name='UserPermission' if 'user_id' in d else 'RoleDefault',
                object_id=str(obj.pk),
                changes={'allowed': d['allowed'], 'module_action': str(ma), 'cible': cible},
                label=f'RBAC — {ma} → {cible} = {d["allowed"]}',
                keep_forever=True,
            )
        except Exception:
            logger.warning('Audit toggle permission RBAC échoué', exc_info=True)
        return Response({'detail': 'Permission mise à jour.', 'allowed': d['allowed']})


class UserPermissionsView(generics.ListAPIView):
    serializer_class   = UserPermissionSerializer
    permission_classes = [IsAdmin]

    def get_queryset(self):
        user_id = self.kwargs.get('user_id')
        return UserPermission.objects.filter(user_id=user_id).select_related('module_action__module', 'module_action__action')


# ── RBAC per-user matrix ───────────────────────────────────────────────────────
class UsersMatrixView(generics.GenericAPIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        try:
            page      = int(request.query_params.get('page', 1))
            page_size = int(request.query_params.get('page_size', 20))
        except (ValueError, TypeError):
            page, page_size = 1, 20
        search = request.query_params.get('search', '').strip()
        return Response(get_users_matrix(page=page, page_size=page_size, search=search))


class UserPermissionsTreeView(generics.GenericAPIView):
    """
    GET /api/v1/auth/rbac/user-permissions/?user_id=X
    Permissions effectives d'un user, indexees par (module_code, action_code).
    Format pense pour l'UI sidebar-shaped (cf. /dashboard/comptes/permissions).
    """
    permission_classes = [IsAdmin]

    def get(self, request):
        try:
            user_id = int(request.query_params.get('user_id', ''))
        except (ValueError, TypeError):
            return Response({'error': 'user_id requis (entier).'}, status=status.HTTP_400_BAD_REQUEST)
        data = get_user_permissions(user_id)
        if data is None:
            return Response({'error': 'Utilisateur introuvable.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(data)


class UserToggleView(generics.GenericAPIView):
    """Toggle a single (user, module_action) permission state: on | off | role (remove override)."""
    permission_classes = [IsAdmin]
    serializer_class   = UserToggleSerializer
    throttle_classes   = [AdminActionThrottle]

    def post(self, request):
        s = UserToggleSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            u  = User.objects.get(pk=s.validated_data['user_id'])
            ma = ModuleAction.objects.get(pk=s.validated_data['ma_id'])
        except (User.DoesNotExist, ModuleAction.DoesNotExist):
            return Response({'error': 'Introuvable.'}, status=status.HTTP_404_NOT_FOUND)

        new_state = toggle_user_permission(u, ma, s.validated_data['state'])
        try:
            from core.audit_helpers import write_audit
            write_audit(
                action='UPDATE', model_name='UserPermission', object_id=str(u.pk),
                changes={'module_action': str(ma), 'new_state': new_state, 'user': u.username},
                label=f'RBAC — {ma} → {u.username} = {new_state}', keep_forever=True,
            )
        except Exception:
            logger.warning('Audit user-toggle RBAC échoué', exc_info=True)
        return Response({'new_state': new_state})


class RoleToggleView(generics.GenericAPIView):
    """Toggle a single (role, module_action) role default on/off."""
    permission_classes = [IsAdmin]
    serializer_class   = RoleToggleSerializer
    throttle_classes   = [AdminActionThrottle]

    def post(self, request):
        s = RoleToggleSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            ma = ModuleAction.objects.get(pk=s.validated_data['ma_id'])
        except ModuleAction.DoesNotExist:
            return Response({'error': 'ModuleAction introuvable.'}, status=status.HTTP_404_NOT_FOUND)

        active = toggle_role_permission(s.validated_data['role'], ma)
        try:
            from core.audit_helpers import write_audit
            write_audit(
                action='UPDATE', model_name='RoleDefault', object_id='0',
                changes={'role': s.validated_data['role'], 'module_action': str(ma), 'active': active},
                label=f"RBAC — {ma} → rôle {s.validated_data['role']} = {active}", keep_forever=True,
            )
        except Exception:
            logger.warning('Audit role-toggle RBAC échoué', exc_info=True)
        return Response({'active': active})


# ── Delegation EDT (matrice user x departement) ──────────────────────────────
class EDTDelegationMatrixView(generics.GenericAPIView):
    """GET la matrice complete user x departement pour la delegation EDT.

    Retour :
    {
      "users": [{"id", "username", "name", "role", "managed_departement_ids":[..]}],
      "departements": [{"id", "label", "filiere_code", "niveau_nom", "nom"}],
    }

    - Exclus : superuser, admin (acces total natif), users inactifs
    - Departements : exclus is_container et nom/code contenant "stage"
    - Filtre optionnel ?annee_universitaire=2025-2026 sur les departements
    """
    permission_classes = [IsAdmin]

    def get(self, request):
        from apps.departement.models import Departement

        # Exclus : enseignant et etudiant (ne sont pas des roles administratifs
        # gerant l'EDT). L'admin EST inclus : il choisit lui-meme les depts
        # qui le concernent (pas de bypass implicite).
        users_qs = (
            CustomUser.objects
            .filter(is_active=True)
            .exclude(role__in=['enseignant', 'etudiant'])
            .order_by('name', 'username')
            .prefetch_related('managed_departements')
        )

        depts_qs = Departement.objects.filter(is_container=False)
        annee = request.query_params.get('annee_universitaire')
        if annee:
            depts_qs = depts_qs.filter(annee_universitaire=annee)
        # Exclure les stages (cours non rattaches a un groupe de planning)
        depts_qs = depts_qs.exclude(nom__icontains='stage').exclude(code__icontains='stage')
        depts_qs = depts_qs.select_related('filiere', 'niveau').order_by(
            'filiere__code', 'nom',
        )

        departements = [{
            'id':            d.id,
            'nom':           d.nom,
            'code':          d.code,
            'filiere_code':  d.filiere.code if d.filiere else None,
            'niveau_nom':    d.niveau.niveau if d.niveau else None,
            'annee_universitaire': d.annee_universitaire,
        } for d in depts_qs]

        users = [{
            'id':       u.id,
            'username': u.username,
            'name':     u.name or u.get_full_name() or u.username,
            'role':     u.role,
            'managed_departement_ids': list(u.managed_departements.values_list('id', flat=True)),
        } for u in users_qs]

        return Response({'users': users, 'departements': departements})


class EDTDelegationToggleView(generics.GenericAPIView):
    """POST {user_id, departement_id, allowed: bool} -> toggle (user, dept).

    Audit : un evenement par toggle, traceable (qui, quand, ajout/retrait).
    """
    permission_classes = [IsAdmin]
    throttle_classes   = [AdminActionThrottle]

    def post(self, request):
        from apps.departement.models import Departement
        from core.audit_helpers import write_audit

        try:
            user_id = int(request.data.get('user_id'))
            dept_id = int(request.data.get('departement_id'))
            allowed = bool(request.data.get('allowed'))
        except (TypeError, ValueError):
            return Response({'error': 'user_id, departement_id (int) et allowed (bool) requis.'}, status=400)

        try:
            u = CustomUser.objects.get(pk=user_id, is_active=True)
        except CustomUser.DoesNotExist:
            return Response({'error': 'Utilisateur introuvable.'}, status=404)

        try:
            d = Departement.objects.get(pk=dept_id)
        except Departement.DoesNotExist:
            return Response({'error': 'Departement introuvable.'}, status=404)

        currently = u.managed_departements.filter(pk=dept_id).exists()
        if allowed and not currently:
            u.managed_departements.add(d)
            action_label = 'ADD'
        elif (not allowed) and currently:
            u.managed_departements.remove(d)
            action_label = 'REMOVE'
        else:
            # No-op : l'etat demande est deja en place
            return Response({'allowed': allowed, 'noop': True})

        write_audit(
            action='UPDATE',
            model_name='CustomUser.managed_departements',
            object_id=str(u.pk),
            changes={
                'op':              action_label,
                'departement_id':  dept_id,
                'departement_nom': d.nom,
                'user_username':   u.username,
            },
            label=f'Delegation EDT {action_label} dept#{dept_id} -> user#{u.pk}',
            keep_forever=True,  # Decision RH, conserver longtemps
        )
        return Response({'allowed': allowed})


class EDTDelegationRollbackView(generics.GenericAPIView):
    """POST {user_id} -> efface TOUTE la delegation EDT du user (rollback total).

    Variante {departement_id} -> retire ce dept a TOUS les users (rollback par dept).

    Audit : un evenement de masse 'CLEAR' avec snapshot complet pour rejouer.
    """
    permission_classes = [IsAdmin]
    throttle_classes   = [AdminActionThrottle]

    def post(self, request):
        from apps.departement.models import Departement
        from core.audit_helpers import write_audit

        user_id = request.data.get('user_id')
        dept_id = request.data.get('departement_id')
        if not user_id and not dept_id:
            return Response({'error': 'user_id ou departement_id requis.'}, status=400)

        cleared = 0
        snapshot: list = []
        if user_id:
            try:
                u = CustomUser.objects.get(pk=int(user_id), is_active=True)
            except (CustomUser.DoesNotExist, TypeError, ValueError):
                return Response({'error': 'Utilisateur introuvable.'}, status=404)
            snapshot = list(u.managed_departements.values_list('id', flat=True))
            cleared = len(snapshot)
            u.managed_departements.clear()
            write_audit(
                action='UPDATE',
                model_name='CustomUser.managed_departements',
                object_id=str(u.pk),
                changes={'op': 'CLEAR_USER', 'snapshot_dept_ids': snapshot, 'user_username': u.username},
                label=f'Delegation EDT CLEAR pour user#{u.pk} ({cleared} dept)',
                keep_forever=True,
            )
        else:
            try:
                d = Departement.objects.get(pk=int(dept_id))
            except (Departement.DoesNotExist, TypeError, ValueError):
                return Response({'error': 'Departement introuvable.'}, status=404)
            users_with = list(d.edt_managers.values_list('id', flat=True))
            cleared = len(users_with)
            d.edt_managers.clear()
            write_audit(
                action='UPDATE',
                model_name='Departement.edt_managers',
                object_id=str(d.pk),
                changes={'op': 'CLEAR_DEPT', 'snapshot_user_ids': users_with, 'departement_nom': d.nom},
                label=f'Delegation EDT CLEAR pour dept#{d.pk} ({cleared} users)',
                keep_forever=True,
            )

        return Response({'cleared': cleared, 'snapshot': snapshot})
