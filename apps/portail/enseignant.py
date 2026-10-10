"""
Profil de l'ENSEIGNANT (app « ISS Enseignant ») : sa fiche Prof, et ses
coordonnées (téléphone, email) qu'il peut corriger lui-même.
"""
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsEnseignant


def _profil(prof, user):
    return {
        'nom':       prof.nom,
        'nni':       prof.NNI,
        'type':      prof.type,
        'grade':     prof.get_grade_display() if prof.grade else '',
        'diplome':   prof.get_niveau_de_diplome_display() if prof.niveau_de_diplome else '',
        'telephone': str(prof.telephone) if prof.telephone else '',
        'email':     prof.email or user.email or '',
        'charge':    prof.charge,
        'decharge':  prof.decharge or 0,
    }


class ProfilEnseignantView(APIView):
    """GET / PATCH /api/v1/portail/enseignant/profil/

    PATCH : {telephone?, email?} — seules les coordonnées changent.
    """
    permission_classes = [IsEnseignant]

    def _prof(self, request):
        return getattr(request.user, 'prof_profile', None)

    def get(self, request):
        prof = self._prof(request)
        if prof is None:
            return Response({'detail': 'Profil enseignant introuvable.'}, status=404)
        return Response(_profil(prof, request.user))

    def patch(self, request):
        from django.core.exceptions import ValidationError
        from django.core.validators import validate_email

        prof = self._prof(request)
        if prof is None:
            return Response({'detail': 'Profil enseignant introuvable.'}, status=404)
        champs = []
        if 'telephone' in request.data:
            tel = ''.join(c for c in str(request.data.get('telephone') or '') if c.isdigit())
            if tel and not 8 <= len(tel) <= 15:
                return Response({'detail': 'Numéro de téléphone invalide.'}, status=400)
            prof.telephone = int(tel) if tel else None
            champs.append('telephone')
        if 'email' in request.data:
            email = str(request.data.get('email') or '').strip()
            if email:
                try:
                    validate_email(email)
                except ValidationError:
                    return Response({'detail': 'Adresse email invalide.'}, status=400)
            prof.email = email
            champs.append('email')
        if champs:
            prof.save(update_fields=champs)
        return Response(_profil(prof, request.user))
