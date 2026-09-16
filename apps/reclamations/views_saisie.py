"""
Saisie de notes EN LIGNE — un brouillon, et rien d'autre.

Un enseignant remplit sa grille depuis le portail ; la direction exporte le
classeur et ressaisit en local. Cette étape humaine n'est pas une lourdeur : le
miroir n'a pas autorité sur les résultats, et une note officielle ne doit pas
pouvoir naître d'une instance qui sera écrasée au push suivant.

On n'écrit donc JAMAIS dans `ipgei_note`. Tout va dans `SaisieNoteEnLigne`,
table sans clé étrangère et exclue de la synchronisation.

CE QUI A CHANGÉ. Ces vues construisaient leur grille avec `build_feuille`, de
`apps/evaluations` — la couche LMD que l'IPGEI a remplacée, et dont les tables
portent **zéro ligne**. L'écran rendait une grille vide à tout enseignant, sans
erreur : le même défaut que le portail étudiant. La source est désormais
`apps/portail/ipgei_enseignant.py`, qui lit les vraies notes IPGEI.

Le périmètre vient de l'emploi du temps (`services.feuilles_enseignant`) : un
enseignant sans séance planifiée n'a aucune feuille, et l'écran le lui dit.
"""
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ipgei.models import Note
from apps.ipgei.services import prof_de
from apps.portail import ipgei_enseignant as source
from core.permissions import IsAdminOrIT, IsEnseignant

from .models import SaisieNoteEnLigne


def _prof(request):
    """L'enseignant connecté, ou `None` — un compte sans fiche n'en a pas."""
    return prof_de(request.user)


class MesFeuillesView(APIView):
    """
    `GET /api/v1/saisie-en-ligne/mes-feuilles/?annee_universitaire=`

    Les couples (classe, matière, semestre) de l'enseignant connecté. L'écran
    en a besoin AVANT la grille : il n'a aucun moyen de deviner ce qu'un
    enseignant a en charge, et lui faire choisir dans la liste complète des
    matières l'inviterait à ouvrir des feuilles qui ne sont pas les siennes.
    """
    permission_classes = [IsEnseignant]

    def get(self, request):
        prof = _prof(request)
        if prof is None:
            return Response({'detail': 'Profil enseignant introuvable.'},
                            status=status.HTTP_403_FORBIDDEN)
        return Response(source.feuilles_de(
            prof, request.query_params.get('annee_universitaire', '')))


class SaisieFeuilleView(APIView):
    """
    `GET /api/v1/saisie-en-ligne/feuille/?classe=&matiere=&semestre=`

    La grille, notes officielles et brouillons superposés. Une case remplie en
    ligne et pas encore reprise porte `brouillon: true` : l'écran doit pouvoir
    distinguer ce qui n'est pas encore officiel de ce qui l'est.
    """
    permission_classes = [IsEnseignant]

    def get(self, request):
        prof = _prof(request)
        if prof is None:
            return Response({'detail': 'Profil enseignant introuvable.'},
                            status=status.HTTP_403_FORBIDDEN)

        classe   = request.query_params.get('classe')
        matiere  = request.query_params.get('matiere')
        semestre = request.query_params.get('semestre')
        if not (classe and matiere and semestre):
            return Response({'detail': 'Paramètres requis : classe, matiere, semestre.'},
                            status=status.HTTP_400_BAD_REQUEST)

        # Le triplet complet, et pas la seule matière : un professeur de maths
        # en MPSI A n'a rien à saisir en MP B, et l'écran passe ses paramètres
        # en clair dans l'URL.
        if not source.peut_saisir(prof, classe, matiere, semestre):
            return Response(
                {'detail': "Cette feuille n'est pas la vôtre : l'emploi du temps "
                           'ne vous y rattache aucune séance.'},
                status=status.HTTP_403_FORBIDDEN)

        return Response(source.feuille(prof, classe, matiere, semestre))


class SaisieBulkView(APIView):
    """
    `POST /api/v1/saisie-en-ligne/saisir-bulk/`
    `{classe, matiere, semestre, cases: [{note, type, numero, valeur}]}`

    Un brouillon par case : la saisie se rejoue à volonté, elle remplace au
    lieu d'empiler. Une case vidée SUPPRIME le brouillon — c'est ainsi que
    l'enseignant revient sur une erreur, et cela doit rester possible tant que
    la direction n'a pas exporté.
    """
    permission_classes = [IsEnseignant]

    def post(self, request):
        prof = _prof(request)
        if prof is None:
            return Response({'detail': 'Profil enseignant introuvable.'},
                            status=status.HTTP_403_FORBIDDEN)

        classe   = request.data.get('classe')
        matiere  = request.data.get('matiere')
        semestre = request.data.get('semestre')
        if not (classe and matiere and semestre):
            return Response({'detail': 'Paramètres requis : classe, matiere, semestre.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not source.peut_saisir(prof, classe, matiere, semestre):
            return Response({'detail': "Cette feuille n'est pas la vôtre."},
                            status=status.HTTP_403_FORBIDDEN)

        # Les notes de CETTE feuille, chargées en une fois : elles bornent ce
        # que la requête peut atteindre. Un identifiant de note venu d'ailleurs
        # — autre classe, autre matière — ne se trouve pas ici et sera rejeté,
        # sans qu'on ait à le vérifier case par case.
        notes = {
            n.pk: n for n in Note.objects
            .filter(inscription__classe_id=classe, matiere_id=matiere,
                    semestre_id=semestre, inscription__actif=True)
            .select_related('inscription__etudiant', 'inscription__classe',
                            'matiere', 'semestre')
        }

        bilan = {'cree': 0, 'efface': 0, 'inchange': 0}
        refus = []
        for index, case in enumerate(request.data.get('cases') or []):
            note = notes.get(case.get('note'))
            if note is None:
                refus.append(f'case {index + 1} : ligne de notes inconnue '
                             'pour cette feuille')
                continue
            try:
                valeur = source.valeur_note(case.get('valeur'))
                resultat = source.deposer(prof, request.user, note,
                                          case.get('type'), case.get('numero') or 1,
                                          valeur)
            except ValueError as erreur:
                refus.append(f'{note.inscription.etudiant.matricule} : {erreur}')
                continue
            bilan[resultat] += 1

        # 400 seulement si RIEN n'a pu être déposé : sur une grille de trente
        # lignes, rejeter l'ensemble pour une case illisible ferait retaper les
        # vingt-neuf autres.
        if refus and not (bilan['cree'] or bilan['efface']):
            return Response({'detail': ' ; '.join(refus[:5])},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({**bilan, 'refus': refus})


class SaisieListView(APIView):
    """`GET /api/v1/saisie-en-ligne/` — les brouillons, pour la direction."""
    permission_classes = [IsAdminOrIT]

    def get(self, request):
        from .serializers import SaisieNoteEnLigneSerializer

        brouillons = SaisieNoteEnLigne.objects.all()
        for filtre in ('classe_id', 'matiere_id', 'semestre_id', 'prof_id',
                       'statut', 'annee_universitaire'):
            valeur = request.query_params.get(filtre)
            if valeur:
                brouillons = brouillons.filter(**{filtre: valeur})
        # Borne haute assumée : cet écran sert à surveiller une campagne, pas à
        # tout déverser. Au-delà, on filtre par feuille ou par enseignant.
        return Response(SaisieNoteEnLigneSerializer(brouillons[:2000],
                                                    many=True).data)


class SaisieExportView(APIView):
    """
    `GET /api/v1/saisie-en-ligne/export/?classe=&matiere=&semestre=&prof_id=`

    Marque au passage les lignes comme exportées : c'est ce qui permet à la
    direction de savoir ce qu'elle a déjà repris en local, et à l'enseignant de
    voir que son brouillon est parti.
    """
    permission_classes = [IsAdminOrIT]

    def get(self, request):
        from openpyxl import Workbook

        brouillons = SaisieNoteEnLigne.objects.all().order_by(
            'classe_nom', 'matiere_code', 'etudiant_matricule',
            'type_note', 'numero')
        correspondance = {'classe': 'classe_id', 'matiere': 'matiere_id',
                          'semestre': 'semestre_id', 'prof_id': 'prof_id'}
        for parametre, champ in correspondance.items():
            valeur = request.query_params.get(parametre)
            if valeur:
                brouillons = brouillons.filter(**{champ: valeur})
        lignes = list(brouillons)

        classeur = Workbook()
        feuille = classeur.active
        feuille.title = 'Saisie en ligne'
        feuille.append(['Matricule', 'Étudiant', 'Classe', 'Code matière',
                        'Matière', 'Semestre', 'Année', 'Type', 'N°', 'Note',
                        'Enseignant', 'Saisie le'])
        for ligne in lignes:
            feuille.append([
                ligne.etudiant_matricule, ligne.etudiant_nom, ligne.classe_nom,
                ligne.matiere_code, ligne.matiere_intitule, ligne.semestre_code,
                ligne.annee_universitaire, ligne.type_note, ligne.numero,
                float(ligne.valeur) if ligne.valeur is not None else '',
                ligne.prof_nom,
                ligne.date_modification.strftime('%Y-%m-%d %H:%M')
                if ligne.date_modification else '',
            ])

        a_marquer = [l.id for l in lignes if l.statut != 'exporte']
        if a_marquer:
            SaisieNoteEnLigne.objects.filter(id__in=a_marquer).update(
                statut='exporte', exporte_le=timezone.now())

        reponse = HttpResponse(
            content_type='application/vnd.openxmlformats-officedocument.'
                         'spreadsheetml.sheet')
        reponse['Content-Disposition'] = 'attachment; filename="saisie-en-ligne.xlsx"'
        classeur.save(reponse)
        return reponse
