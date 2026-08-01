"""F-5 : Middleware Cache-Control pour endpoints lookup (donnees de reference).

Ajoute `Cache-Control: private, max-age=300` sur les GET vers les endpoints
quasi-immutables (jours, creneaux, seances, salles, semestres, niveaux, banques,
departements, annees) — uniquement le sous-chemin `/all/` qui retourne la liste
complete pour les <select> du frontend.

Pourquoi `private` : la donnee depend de RBAC/institution → pas safe en cache
partage (CDN, proxy). Le navigateur du user, lui, peut cacher 5 min.

Pourquoi 5 min : aligne avec STALE_REFERENCE cote frontend (lib/api/_constants.ts).
Mutations CRUD admin invalident TanStack Query → fraicheur garantie.

NB : ne touche PAS aux endpoints calcul (vacations/, suivi/, etc.) — uniquement
les lookup statiques.
"""
import re

# Endpoints lookup safe a cacher (regex compilees une fois)
_CACHEABLE_PATTERNS = [
    re.compile(r'^/api/v1/parametres/jours/all/?$'),
    re.compile(r'^/api/v1/parametres/creneaux/all/?$'),
    re.compile(r'^/api/v1/parametres/seances/all/?$'),
    re.compile(r'^/api/v1/parametres/salles/all/?$'),
    re.compile(r'^/api/v1/parametres/semestres/all/?$'),
    re.compile(r'^/api/v1/parametres/niveaux/all/?$'),
    re.compile(r'^/api/v1/parametres/annees/all/?$'),
    re.compile(r'^/api/v1/banques/all/?$'),
    re.compile(r'^/api/v1/departements/all/?$'),
]

CACHE_MAX_AGE_LOOKUP = 300  # 5 minutes


def cache_control_lookup_middleware(get_response):
    def middleware(request):
        response = get_response(request)
        if request.method == 'GET' and 200 <= response.status_code < 300:
            path = request.path or ''
            for pat in _CACHEABLE_PATTERNS:
                if pat.match(path):
                    response['Cache-Control'] = f'private, max-age={CACHE_MAX_AGE_LOOKUP}'
                    break
        return response
    return middleware
