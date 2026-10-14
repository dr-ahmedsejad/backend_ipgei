"""
La vue de surveillance, exposée : à l'écran, et en PDF pour le poste de garde.

`/api/v1/ipgei/surveillance/?annee_universitaire=&date=` rend la journée ;
`/pdf/` l'imprime — la journée avec `date`, la semaine entière avec
`type_semestre` et `numero_semaine`, une page par jour.

Module RBAC « surveillance » (migration authentication 0026) : DA et AA par
défaut, d'autres rôles depuis la matrice des permissions, l'administrateur
toujours. Voir `surveillance.py`.
"""
import datetime as dt

from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import RBACPermission

from . import surveillance as svc


def _date(valeur):
    try:
        return dt.date.fromisoformat((valeur or '').strip())
    except ValueError:
        return None


class VueSurveillanceView(APIView):
    permission_classes = [RBACPermission]
    required_module = 'surveillance'

    def get(self, request):
        annee = (request.query_params.get('annee_universitaire') or '').strip()
        date = _date(request.query_params.get('date'))
        if not annee or date is None:
            return Response({'detail': "Indiquez l'année universitaire et la date (AAAA-MM-JJ)."},
                            status=400)
        return Response(svc.journee(annee, date, request.user))


class VueSurveillancePDFView(APIView):
    permission_classes = [RBACPermission]
    required_module = 'surveillance'

    def get(self, request):
        from . import edt_pdf

        p = request.query_params
        annee = (p.get('annee_universitaire') or '').strip()
        if not annee:
            return Response({'detail': "Indiquez l'année universitaire."}, status=400)

        if p.get('numero_semaine'):
            type_semestre = (p.get('type_semestre') or '').strip()
            numero = (p.get('numero_semaine') or '').strip()
            if type_semestre not in ('I', 'P') or not numero.isdigit():
                return Response({'detail': 'Indiquez la période (I ou P) et le numéro de semaine.'},
                                status=400)
            journees = svc.semaine(annee, type_semestre, int(numero))
            fichier = f'surveillance_{annee}_semaine_{numero}.pdf'
        else:
            date = _date(p.get('date'))
            if date is None:
                return Response({'detail': 'Indiquez la date (AAAA-MM-JJ) ou la semaine.'},
                                status=400)
            journees = [svc.journee(annee, date)]
            fichier = f'surveillance_{date.isoformat()}.pdf'

        if not journees:
            return Response({'detail': 'Aucune journée de cours pour cette semaine.'}, status=404)
        for j in journees:
            j['lignes'] = svc.grille(j)
        # Paysage, sans pied de page : le gabarit des emplois du temps IPGEI.
        return edt_pdf.reponse_pdf('ipgei/surveillance_pdf.html',
                                   {'annee': annee, 'journees': journees}, fichier)
