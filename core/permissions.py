"""
RBAC permissions — miroir du système GesAFPED.
Chaque ViewSet déclare `required_module` et les actions autorisées
sont déduites de la méthode HTTP + action DRF.
"""
import logging

from django.core.cache import cache
from rest_framework.permissions import BasePermission

logger = logging.getLogger(__name__)


# ── Mapping action DRF → code action RBAC ────────────────────────────────────
ACTION_MAP = {
    'list':           'voir',
    'retrieve':       'voir',
    'create':         'modifier',
    'update':         'modifier',
    'partial_update': 'modifier',
    'destroy':        'supprimer',
    'export':         'exporter',
}

RBAC_CACHE_TTL = 300  # 5 minutes


class RBACPermission(BasePermission):
    """
    Vérifie les permissions RBAC pour un module donné.
    Le ViewSet doit définir `required_module = 'code_module'`.
    Les Admin ont toujours accès.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        user = request.user

        # Admin = accès total
        if user.role == 'admin' or user.is_superuser:
            return True

        module_code = getattr(view, 'required_module', None)
        if not module_code:
            # FAIL-CLOSED : un ViewSet qui declare RBACPermission DOIT
            # aussi declarer `required_module`. Sinon on refuse — un
            # oubli silencieux deviendrait une faille d'autorisation.
            # Pour exposer une ressource sans RBAC, utiliser une autre
            # permission (IsAuthenticated, IsAdminOrReadOnly, AllowAny).
            logger.error(
                'RBACPermission utilisee sans `required_module` sur %s '
                '(action=%s). Acces refuse par securite.',
                view.__class__.__name__, getattr(view, 'action', '?'),
            )
            return False

        action_code = ACTION_MAP.get(getattr(view, 'action', ''), 'voir')

        return _has_access(user, module_code, action_code)


class IsAdmin(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.role == 'admin')


class IsAdminOrIT(BasePermission):
    """Utilisé pour les endpoints de sécurité accessibles à admin et IT."""
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.role in ('admin', 'IT'))


class IsAdminOrReadOnly(BasePermission):
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return True
        return request.user.role == 'admin'


class EDTDepartementPermission(BasePermission):
    """
    Permission objet pour les domaines delegables EDT (emplois/suivi/vacation).

    Regles :
      - Admin / superuser : acces total.
      - Authentifie : peut lire (les filtres queryset font le scoping).
      - Pour les writes (POST/PATCH/PUT/DELETE) : le dept cible doit etre dans
        request.user.managed_departements. Le dept cible est :
          * sur create : request.data['departement']
          * sur update/delete : obj.departement_id (via has_object_permission)
          * pour les modeles M2M (Vacation) : tous les depts du payload doivent
            etre dans la liste autorisee. Ajouter `departement_payload_field`
            sur le ViewSet pour pointer le champ M2M.

    Cette permission s'utilise EN COMBINAISON avec RBACPermission/IsAuthenticated.
    Elle ne remplace pas le check RBAC global de la vue, elle filtre par dept.
    """
    departement_payload_field = 'departement'

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        u = request.user
        # Superuser : bypass (filet de securite). L'admin role passe par
        # le scoping normal via ses managed_departements.
        if u.is_superuser:
            return True
        # Lecture toujours OK : queryset scoping fait le menage
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return True
        # POST : verifier le payload departement uniquement sur le CREATE
        # standard. Les @action custom (ajouter_suivie, import_from_suivi,
        # vider, count_scoped, ...) gerent leur propre scoping interne
        # via self.user_dept_ids() et n'envoient pas de champ `departement`
        # dans leur payload.
        if request.method == 'POST':
            if getattr(view, 'action', None) != 'create':
                return True
            field = getattr(view, 'departement_payload_field', self.departement_payload_field)
            payload_dept = request.data.get(field)
            # Si la donnee envoie une liste (M2M comme Vacation.departements) :
            if isinstance(payload_dept, (list, tuple)):
                if not payload_dept:
                    # Création TRANSVERSALE sans département (ex. séance « Encadrement ») :
                    # il n'y a rien à scoper → on délègue au RBAC (vac_saisie + modifier),
                    # qui a déjà autorisé l'utilisateur. (Refuser ici cassait l'Encadrement.)
                    return True
                allowed = set(u.managed_departements.values_list('id', flat=True))
                try:
                    return all(int(d) in allowed for d in payload_dept)
                except (TypeError, ValueError):
                    return False
            # FK simple
            if payload_dept is None:
                # CREATE standard sans dept -> refus (l'API exige le champ)
                return False
            try:
                return u.managed_departements.filter(pk=int(payload_dept)).exists()
            except (TypeError, ValueError):
                return False
        # PATCH/PUT/DELETE : verification objet par has_object_permission
        return True

    def has_object_permission(self, request, view, obj):
        u = request.user
        if u.is_superuser:
            return True
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return True
        allowed = set(u.managed_departements.values_list('id', flat=True))
        # FK simple
        dept_id = getattr(obj, 'departement_id', None)
        if dept_id is not None:
            return dept_id in allowed
        # M2M (Vacation) : au moins 1 dept commun ET TOUS les depts de l'objet
        # doivent etre autorises (sinon on permettrait d'editer un cours qui
        # touche aussi des groupes hors perimetre).
        if hasattr(obj, 'departements'):
            obj_depts = set(obj.departements.values_list('id', flat=True))
            if not obj_depts:
                # Vacation TRANSVERSALE sans département (ex. « Encadrement ») :
                # rien à scoper → on délègue au RBAC (cohérent avec la création).
                return True
            return obj_depts.issubset(allowed)
        return False


class IsOwnerOrAdmin(BasePermission):
    """Utilisé pour les endpoints profil utilisateur."""
    def has_object_permission(self, request, view, obj):
        return request.user.is_authenticated and (
            request.user.role == 'admin' or obj == request.user
        )


class IsEtudiant(BasePermission):
    """Réservé aux utilisateurs avec le rôle 'etudiant'."""
    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated
            and request.user.role == 'etudiant'
        )


class IsEnseignant(BasePermission):
    """Réservé aux utilisateurs avec le rôle 'enseignant'."""
    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated
            and request.user.role == 'enseignant'
        )


# ── Cache RBAC — versionnement par user et par rôle ──────────────────────────
# La clé inclut deux versions :
#   rbac_v_user:{user_pk}  → incrémenté à chaque toggle de permission user
#   rbac_v_role:{role}     → incrémenté à chaque toggle de permission rôle
# Un incrément invalide naturellement toutes les clés calculées avec l'ancienne version.
# Si Redis est indisponible, toutes les opérations cache sont silencieusement ignorées.

def _cache_get_version(key: str) -> int:
    """Retourne la version courante ou 0 si le cache est indisponible."""
    try:
        v = cache.get(key)
        return v if isinstance(v, int) else 0
    except Exception:
        return 0


def _cache_bump_version(key: str) -> None:
    """Incrémente la version. Crée la clé si elle n'existe pas encore."""
    try:
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=None)
    except Exception:
        pass  # Cache indisponible — on continue sans cache


def invalidate_user_rbac(user_pk: int) -> None:
    """Invalide le cache RBAC pour un utilisateur donné (après toggle de permission individuelle)."""
    _cache_bump_version(f'rbac_v_user:{user_pk}')


def invalidate_role_rbac(role: str) -> None:
    """Invalide le cache RBAC pour tous les utilisateurs d'un rôle (après toggle de rôle par défaut)."""
    _cache_bump_version(f'rbac_v_role:{role}')


# ── Helper interne ────────────────────────────────────────────────────────────
def _compute_access(user, module_code: str, action_code: str) -> bool:
    """
    Résolution des permissions (sans cache) :
    1. Cherche une UserPermission explicite
    2. Sinon, utilise le RoleDefault du rôle de l'utilisateur
    """
    from apps.authentication.models import UserPermission, RoleDefault, ModuleAction, Module, Action

    try:
        module = Module.objects.get(code=module_code)
        action = Action.objects.get(code=action_code)
        ma = ModuleAction.objects.get(module=module, action=action)
    except Exception:
        return False

    # 1. Permission utilisateur explicite
    up = UserPermission.objects.filter(user=user, module_action=ma).first()
    if up is not None:
        return up.allowed

    # 2. Défaut du rôle (présence = autorisé)
    rd = RoleDefault.objects.filter(role=user.role, module_action=ma).first()
    return rd.allowed if rd is not None else False


def _has_access(user, module_code: str, action_code: str) -> bool:
    """
    Vérifie l'accès avec cache versionné.
    Si le cache (Redis) est indisponible, délègue directement à _compute_access().
    TTL : 5 minutes. Invalidation automatique via versionnement.
    """
    # Tentative de lecture du cache avec version courante
    try:
        user_v = _cache_get_version(f'rbac_v_user:{user.pk}')
        role_v = _cache_get_version(f'rbac_v_role:{user.role}')
        cache_key = f'rbac:{user.pk}:{user_v}:{role_v}:{module_code}:{action_code}'
        cached = cache.get(cache_key)
        if cached is not None:
            return bool(cached)
    except Exception:
        # Cache indisponible → calcul direct sans mise en cache
        return _compute_access(user, module_code, action_code)

    # Calcul réel
    result = _compute_access(user, module_code, action_code)

    # Mise en cache avec TTL
    try:
        cache.set(cache_key, result, timeout=RBAC_CACHE_TTL)
    except Exception:
        pass  # Cache indisponible — on retourne le résultat sans cacher

    return result
