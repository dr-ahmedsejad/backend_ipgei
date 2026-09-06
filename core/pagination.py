from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardPagination(PageNumberPagination):
    page_size             = 10
    page_size_query_param = 'page_size'
    # Le frontend demande jusqu'à 1 000 lignes (écrans d'affectation, listes
    # déroulantes). À 200, le surplus était TRONQUÉ EN SILENCE : ni erreur, ni
    # message — une année de 210 inscrits en perdait dix sur l'écran
    # d'affectation. Le plafond couvre désormais la plus grande demande émise.
    max_page_size         = 1000

    def get_paginated_response(self, data):
        return Response({
            'count':    self.page.paginator.count,
            'pages':    self.page.paginator.num_pages,
            'next':     self.get_next_link(),
            'previous': self.get_previous_link(),
            'results':  data,
        })

    def get_paginated_response_schema(self, schema):
        return {
            'type': 'object',
            'properties': {
                'count':    {'type': 'integer'},
                'pages':    {'type': 'integer'},
                'next':     {'type': 'string', 'nullable': True},
                'previous': {'type': 'string', 'nullable': True},
                'results':  schema,
            },
        }


class NoPagination(PageNumberPagination):
    """Retourne tous les résultats (pour les listes de référence)."""
    page_size = None

    def paginate_queryset(self, queryset, request, view=None):
        return None
