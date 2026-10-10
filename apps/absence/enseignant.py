"""
Liste des étudiants d'une séance, pour l'ENSEIGNANT (app mobile, même adresse
qu'à l'ISS).

Lecture seule : l'appel reste sur la fiche papier. L'enseignant consulte, pour
une séance de SA grille (`suivi/pointages/grille/`, identifiant = pointage),
qui doit être là : groupe par groupe, la liste de la fiche de présence de l'IPGEI (inscriptions IPGEI de la classe ou du sous-groupe),
dans l'ordre croissant des matricules.
"""
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsEnseignant


def ordre_matricule(matricule):
    """Matricules en NOMBRE (« 9999 » avant « 10000 ») ; un matricule non
    numérique passe après, par ordre alphabétique."""
    m = (matricule or '').strip()
    return (0, int(m), '') if m.isdigit() else (1, 0, m)


def _groupe(dep, sp):
    """À l'IPGEI, le groupe d'une séance est une CLASSE ou un sous-groupe de TP
    (`apps/ipgei/miroir.py`) : ses étudiants sont ceux de l'inscription IPGEI
    de l'année (auditeurs compris : ils assistent aux cours)."""
    from django.db.models import Q

    from apps.ipgei.models import InscriptionIPGEI

    inscriptions = (InscriptionIPGEI.objects
                    .filter(Q(classe_id=dep.pk) | Q(sous_groupe_id=dep.pk),
                            annee_universitaire=sp.annee_universitaire, actif=True)
                    .select_related('etudiant'))
    lignes = [{'matricule': i.etudiant.matricule or '', 'nom': i.etudiant.nom or '', 'statut': ''}
              for i in inscriptions]
    return lignes, 'classe'


class ListeSeanceEnseignantView(APIView):
    """GET /api/v1/absences/enseignant/liste/?pointage=<id>

    Réponse (même forme qu'à l'ISS) :
      {seance: {em_code, em_intitule, type_seance, date, numero_semaine},
       groupes: [{id, nom, source, etudiants: [{matricule, nom, statut}]}],
       total}
    """
    permission_classes = [IsEnseignant]

    def get(self, request):
        from apps.suivi.models import SuiviePointage

        prof = getattr(request.user, 'prof_profile', None)
        if prof is None:
            return Response({'error': 'Profil enseignant introuvable.'}, status=403)
        pid = request.query_params.get('pointage', '')
        if not pid.isdigit():
            return Response({'error': 'Paramètre pointage requis.'}, status=400)
        sp = (SuiviePointage.objects.filter(pk=int(pid), prof_id=prof.pk)
              .select_related('em', 'type_seance_fk').prefetch_related('departements').first())
        if sp is None:
            # Pas la sienne, ou inexistante : on ne dit pas laquelle.
            return Response({'error': 'Séance introuvable.'}, status=404)

        groupes, total = [], 0
        for dep in sorted(sp.departements.all(), key=lambda d: d.nom or ''):
            lignes, source = _groupe(dep, sp)
            lignes.sort(key=lambda l: ordre_matricule(l['matricule']))
            total += len(lignes)
            groupes.append({'id': dep.pk, 'nom': dep.nom or '', 'source': source, 'etudiants': lignes})

        return Response({
            'seance': {
                'em_code':        sp.em.code_em if sp.em_id else '',
                'em_intitule':    sp.em.intitule if sp.em_id else '',
                'type_seance':    sp.type_seance_fk.type_seance if sp.type_seance_fk_id else '',
                'date':           sp.date_suivie.isoformat() if sp.date_suivie else None,
                'numero_semaine': sp.numero_semaine,
            },
            'groupes': groupes,
            'total':   total,
        })
