"""Endpoint de contrôle d'accès aux fichiers /media/ via Nginx `auth_request`.

Problème : en production Nginx sert /media/ directement (deploy/nginx.conf),
donc tout fichier uploadé (justificatifs, diplômes, pièces d'identité, CV) est
téléchargeable par URL sans authentification — les numéros de série étant
séquentiels, les chemins sont énumérables.

Mitigation : Nginx interroge cet endpoint (sous-requête `auth_request`) avant de
servir un fichier. 204 = autorisé (utilisateur authentifié via cookie JWT),
403/401 = refusé. Aucun changement d'URL ni de serializer requis.
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


class MediaAuthView(APIView):
    """Sous-requête Nginx : 204 si l'appelant est authentifié, 401/403 sinon."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(status=204)
