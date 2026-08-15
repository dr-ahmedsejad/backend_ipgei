"""
Saisie de notes EN LIGNE — un brouillon, et rien d'autre.

Un enseignant remplit sa grille depuis le portail ; la direction exporte le
classeur et ressaisit en local. Cette étape humaine n'est pas une lourdeur : le
miroir n'a pas autorité sur les résultats, et une note officielle ne doit pas
pouvoir naître d'une instance qui sera écrasée au push suivant.

On n'écrit donc JAMAIS dans `evaluations_note`. Tout va dans `SaisieNoteEnLigne`,
table sans clé étrangère et exclue de la synchronisation.

La grille est la MÊME que celle de la saisie ordinaire — `build_feuille` est
réutilisée telle quelle — mais bornée au périmètre du professeur et recouverte
de ses brouillons. Un enseignant qui bascule du local au portail doit retrouver
son écran, pas en apprendre un second.
"""
from decimal import Decimal, InvalidOperation
from io import BytesIO

from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.em.models import EM
from apps.evaluations.models import SessionEvaluation
from apps.evaluations.services.note_access import peut_acceder_em
from apps.evaluations.services.note_lecture import build_feuille
from apps.inscriptions.models import InscriptionElement
from core.permissions import IsAdminOrIT, IsEnseignant

from .models import SaisieNoteEnLigne

#: Les trois cases d'une ligne, et le type de note qu'elles portent en base.
TYPES = (('cc', 'CC'), ('tp', 'TP'), ('exam', 'EXAM'))


def _note(valeur):
    """Une note sur 20, ou None — ce qui vaut effacement du brouillon."""
    if valeur in (None, ''):
        return None
    try:
        nombre = Decimal(str(valeur))
    except (InvalidOperation, TypeError):
        return None
    return nombre if 0 <= nombre <= 20 else None


class SaisieFeuilleView(APIView):
    """
    `GET /api/v1/saisie-en-ligne/feuille/?session=&em=`

    La grille du professeur, recouverte de ses brouillons. Chaque case ainsi
    remplacée porte `brouillon: true` : l'écran doit pouvoir distinguer ce qui
    n'est pas encore officiel de ce qui l'est.
    """
    permission_classes = [IsEnseignant]

    def get(self, request):
        prof = getattr(request.user, 'prof_profile', None)
        if prof is None:
            return Response({'detail': 'Profil enseignant introuvable.'},
                            status=status.HTTP_403_FORBIDDEN)

        # `build_feuille` applique déjà `peut_acceder_em` : un EM hors périmètre
        # ressort en 403 sans qu'on ait à le revérifier ici.
        base = build_feuille(request)
        if getattr(base, 'status_code', 200) != 200:
            return base
        lignes = base.data

        session_id = request.query_params.get('session')
        em_id      = request.query_params.get('em')
        brouillons = {
            (d.inscription_element_id, d.type_note): d.valeur
            for d in SaisieNoteEnLigne.objects.filter(
                prof_id=prof.pk, session_id=session_id, em_id=em_id)
        }

        for ligne in lignes:
            inscription = ligne['inscription_element']
            for champ, type_note in TYPES:
                if (inscription, type_note) in brouillons:
                    case = ligne.get(champ) or {'id': None, 'valeur': None}
                    case['valeur'] = str(brouillons[(inscription, type_note)])
                    case['brouillon'] = True
                    ligne[champ] = case
        return Response(lignes)


class SaisieBulkView(APIView):
    """
    `POST /api/v1/saisie-en-ligne/saisir-bulk/`
    `{session, em, rows: [{inscription_element, cc, tp, exam}]}`

    Un brouillon par case : la saisie se rejoue à volonté, elle remplace au lieu
    d'empiler. Une case vidée SUPPRIME le brouillon — c'est ainsi que
    l'enseignant revient sur une erreur, et cela doit rester possible tant que
    la direction n'a pas exporté.
    """
    permission_classes = [IsEnseignant]

    def post(self, request):
        prof = getattr(request.user, 'prof_profile', None)
        if prof is None:
            return Response({'detail': 'Profil enseignant introuvable.'},
                            status=status.HTTP_403_FORBIDDEN)

        session_id = request.data.get('session')
        em_id      = request.data.get('em')
        lignes     = request.data.get('rows') or []
        if not session_id or not em_id:
            return Response({'detail': 'La session et l’élément de module sont '
                                       'obligatoires.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not peut_acceder_em(request.user, em_id):
            return Response({'detail': 'Cet élément de module est hors de votre '
                                       'périmètre.'},
                            status=status.HTTP_403_FORBIDDEN)

        try:
            session = SessionEvaluation.objects.get(pk=session_id)
            em      = EM.objects.get(pk=em_id)
        except (SessionEvaluation.DoesNotExist, EM.DoesNotExist):
            return Response({'detail': 'Session ou élément de module introuvable.'},
                            status=status.HTTP_404_NOT_FOUND)

        # Les instantanés d'étudiants, ramassés en une requête : sans eux,
        # l'export devrait rejoindre des tables miroir, et perdrait son sens le
        # jour où la synchronisation les remplace.
        references = [l.get('inscription_element') for l in lignes
                      if l.get('inscription_element')]
        etudiants = {}
        for inscription in (InscriptionElement.objects
                            .filter(id__in=references)
                            .select_related(
                                'inscription_ped__inscription_admin__etudiant')):
            etudiant = inscription.inscription_ped.inscription_admin.etudiant
            etudiants[inscription.id] = (
                etudiant.matricule,
                getattr(etudiant, 'nom_complet', None) or etudiant.nom,
            )

        crees = modifies = supprimes = 0
        for ligne in lignes:
            reference = ligne.get('inscription_element')
            if not reference or reference not in etudiants:
                continue
            matricule, nom = etudiants[reference]
            for champ, type_note in TYPES:
                cle = dict(prof_id=prof.pk, inscription_element_id=reference,
                           session_id=int(session_id), type_note=type_note)
                valeur = _note(ligne.get(champ))
                if valeur is None:
                    nombre, _ = SaisieNoteEnLigne.objects.filter(**cle).delete()
                    supprimes += nombre
                    continue
                _, cree = SaisieNoteEnLigne.objects.update_or_create(
                    **cle,
                    defaults=dict(
                        valeur=valeur, statut='brouillon',
                        prof_nom=getattr(prof, 'nom', '') or '',
                        user_id=request.user.pk,
                        em_id=int(em_id),
                        institution_id=getattr(session, 'institution_id', None),
                        etudiant_matricule=matricule or '',
                        etudiant_nom=nom or '',
                        em_code=getattr(em, 'code_em', '') or '',
                        em_intitule=getattr(em, 'intitule', '') or '',
                        session_code=getattr(session, 'code', '') or '',
                    ),
                )
                crees += int(cree)
                modifies += int(not cree)

        return Response({'created': crees, 'updated': modifies,
                         'deleted': supprimes})


class SaisieListView(APIView):
    """`GET /api/v1/saisie-en-ligne/` — les brouillons, pour la direction."""
    permission_classes = [IsAdminOrIT]

    def get(self, request):
        from .serializers import SaisieNoteEnLigneSerializer

        brouillons = SaisieNoteEnLigne.objects.all()
        for filtre in ('session_id', 'em_id', 'prof_id', 'statut'):
            valeur = request.query_params.get(filtre)
            if valeur:
                brouillons = brouillons.filter(**{filtre: valeur})
        # Borne haute assumée : cet écran sert à surveiller une campagne, pas à
        # tout déverser. Au-delà, on filtre par session ou par enseignant.
        return Response(SaisieNoteEnLigneSerializer(brouillons[:2000],
                                                    many=True).data)


class SaisieExportView(APIView):
    """
    `GET /api/v1/saisie-en-ligne/export/?session=&em=&prof_id=` — le classeur.

    Marque au passage les lignes comme exportées : c'est ce qui permet à la
    direction de savoir ce qu'elle a déjà repris en local, et à l'enseignant de
    voir que son brouillon est parti.
    """
    permission_classes = [IsAdminOrIT]

    def get(self, request):
        from openpyxl import Workbook

        brouillons = SaisieNoteEnLigne.objects.all().order_by(
            'em_code', 'etudiant_matricule', 'type_note')
        for filtre in ('session_id', 'em_id', 'prof_id'):
            valeur = request.query_params.get(filtre)
            if valeur:
                brouillons = brouillons.filter(**{filtre: valeur})
        lignes = list(brouillons)

        classeur = Workbook()
        feuille = classeur.active
        feuille.title = 'Saisie en ligne'
        feuille.append(['Matricule', 'Étudiant', 'Code EM', 'Élément de module',
                        'Session', 'Type', 'Note', 'Enseignant', 'Saisie le'])
        for ligne in lignes:
            feuille.append([
                ligne.etudiant_matricule, ligne.etudiant_nom,
                ligne.em_code, ligne.em_intitule, ligne.session_code,
                ligne.type_note,
                float(ligne.valeur) if ligne.valeur is not None else '',
                ligne.prof_nom,
                ligne.date_modification.strftime('%Y-%m-%d %H:%M')
                if ligne.date_modification else '',
            ])

        a_marquer = [l.id for l in lignes if l.statut != 'exporte']
        if a_marquer:
            SaisieNoteEnLigne.objects.filter(id__in=a_marquer).update(
                statut='exporte', exporte_le=timezone.now())

        tampon = BytesIO()
        classeur.save(tampon)
        reponse = HttpResponse(
            tampon.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.'
                         'spreadsheetml.sheet')
        reponse['Content-Disposition'] = 'attachment; filename="saisie-en-ligne.xlsx"'
        return reponse
