"""
Mode MIROIR — un seul code, deux rôles d'instance.

Le même dépôt tourne à deux endroits, et le drapeau `MIRROR_MODE` (.env) décide
lequel :

  * serveur **LOCAL** (`MIRROR_MODE=False`) — l'autorité. On y écrit tout, et
    c'est de lui que part la synchronisation ;
  * **VPS** (`MIRROR_MODE=True`) — un miroir en LECTURE SEULE. Il reflète le
    local, et n'accepte d'écritures que celles de la « boîte de réception » :
    ce que les enseignants déposent en ligne et que le local viendra chercher.

Sans ce garde-fou, une écriture faite en ligne serait écrasée au push suivant —
le miroir se restaure à partir du dump du local. Mieux vaut la refuser tout de
suite, avec un message qui dit où aller, que la perdre silencieusement une
semaine plus tard.
"""
from django.conf import settings
from django.http import JsonResponse

#: Les méthodes qui ne modifient rien : elles passent toujours.
METHODES_SURES = frozenset({'GET', 'HEAD', 'OPTIONS', 'TRACE'})

#: Préfixes d'URL autorisés en écriture MÊME en miroir. Le réglage
#: `MIRROR_WRITE_ALLOWLIST` prend le pas sur cette valeur de repli.
ALLOWLIST_PAR_DEFAUT = (
    '/api/v1/auth/',                    # se connecter, se déconnecter, rafraîchir
    '/admin/login',                     # connexion à l'admin Django (consultation)
)


class MirrorReadOnlyMiddleware:
    """
    En miroir : refuse toute écriture hors liste blanche. Sur le local :
    parfaitement transparent — le middleware ne fait rien du tout.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.actif = bool(getattr(settings, 'MIRROR_MODE', False))
        self.allowlist = tuple(getattr(settings, 'MIRROR_WRITE_ALLOWLIST',
                                       ALLOWLIST_PAR_DEFAUT))

    def __call__(self, request):
        if self.actif and request.method not in METHODES_SURES:
            chemin = request.path
            if not any(chemin.startswith(prefixe) for prefixe in self.allowlist):
                return JsonResponse(
                    {'detail': 'Instance miroir (lecture seule) — cette écriture '
                               "n'est pas autorisée en ligne. À effectuer sur le "
                               'serveur local.',
                     'code': 'mirror_readonly'},
                    status=403)
        return self.get_response(request)


def instance_info(request):
    """
    `GET /api/v1/instance/` — le rôle de l'instance, en accès public.

    Lu par le frontend AVANT la connexion : c'est ce qui lui permet d'afficher le
    bandeau « miroir », de masquer la synchronisation et de router les écritures
    des enseignants vers la boîte de réception. Public à dessein — l'information
    n'est pas un secret, et la demander après authentification obligerait à
    dessiner l'écran de connexion sans savoir sur quelle instance on se trouve.
    """
    miroir = bool(getattr(settings, 'MIRROR_MODE', False))
    return JsonResponse({
        'role':          'mirror' if miroir else 'local',
        'mirror_mode':   miroir,
        'nom':           'Portail en ligne' if miroir else 'Serveur local',
        'lecture_seule': miroir,
    })
