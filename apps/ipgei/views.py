"""
API IPGEI — /api/v1/ipgei/

Les ViewSets restent minces : ils valident, délèguent à `services.py` et
renvoient. Le périmètre est celui de l'institution principale (IPGEI), via
`InstitutionScopedMixin` du socle.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.response import Response
from rest_framework.viewsets import (GenericViewSet, ModelViewSet,
                                     ReadOnlyModelViewSet)
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin, UpdateModelMixin

from apps.prof.models import Prof
from apps.salle.models import Salle
from core.mixins import InstitutionScopedMixin, SelectAllMixin
from core.pagination import NoPagination, StandardPagination
from core.permissions import RBACPermission

from . import edt_pdf, services
from .filters import ClasseFilter, InscriptionIPGEIFilter
from .models import (
    AbsenceSeance, Classe, Deliberation, EmploiArchive, Evaluation, GrilleType,
    HistoriqueClasse,
    InscriptionIPGEI, LigneDeliberation, Matiere, Note, ParametresIPGEI,
    PermutationEtudiant, PermutationProf, SeanceReelle, SeanceType, SemaineIPGEI,
    SemestreIPGEI, SousGroupeTP, StatutWorkflow,
)
from .serializers import (
    AbsenceSeanceSerializer, ClasseSelectSerializer, ClasseSerializer,
    DeliberationSerializer, DupliquerGrilleSerializer, EditionLotSerializer,
    EmploiArchiveSerializer, EvaluationSerializer, GrilleTypeListSerializer, GrilleTypeSerializer,
    HistoriqueClasseSerializer, InscriptionCompleteSerializer,
    InscriptionIPGEISerializer,
    LigneDeliberationListSerializer, LigneDeliberationSerializer,
    MatiereSelectSerializer, MatiereSerializer, NoteSerializer,
    ParametresIPGEISerializer, PermutationEtudiantSerializer,
    PermutationProfSerializer, SaisieAbsencesSerializer,
    SaisieCollectiveSerializer, SeanceReelleSerializer, SeanceTypeSerializer,
    SemaineIPGEISerializer, SemestreIPGEISerializer, SousGroupeTPSerializer,
    VersionArchiveSerializer,
)


def _nom_fichier(libelle: str) -> str:
    """Portion de nom de fichier sûre : accents et ponctuation retirés."""
    import re
    import unicodedata

    sans_accent = (
        unicodedata.normalize('NFKD', libelle or '')
        .encode('ascii', 'ignore').decode()
    )
    return re.sub(r'_+', '_', re.sub(r'[^\w]+', '_', sans_accent)).strip('_') or 'edt'


def _institution_principale():
    from apps.parametres.models import Institution
    return Institution.objects.filter(est_principale=True).first()


def _utilisateur(request):
    user = getattr(request, 'user', None)
    return user if getattr(user, 'is_authenticated', False) else None


class _ErreurMetierMixin:
    """Transforme les ValidationError Django des services en 400 DRF lisibles."""
    def _executer(self, fonction, *args, **kwargs):
        try:
            return fonction(*args, **kwargs)
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, 'messages', [str(exc)]))


# ═════════════════════════════════════════════════════════════════════════════
# Paramètres
# ═════════════════════════════════════════════════════════════════════════════
class ParametresIPGEIViewSet(ListModelMixin, RetrieveModelMixin, UpdateModelMixin, GenericViewSet):
    """
    Réglages du cursus (seuil, plafond de rattrapage, droit de redoublement).
    GET /api/v1/ipgei/parametres/courant/ → paramètres de l'institution principale.
    """
    serializer_class   = ParametresIPGEISerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_parametres'
    pagination_class   = NoPagination
    queryset           = ParametresIPGEI.objects.select_related('institution')

    @action(detail=False, methods=['get', 'patch'], url_path='courant')
    def courant(self, request):
        institution = _institution_principale()
        if institution is None:
            raise ValidationError("Aucune institution principale configurée.")
        params = ParametresIPGEI.pour(institution)
        if request.method == 'PATCH':
            serializer = self.get_serializer(params, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)
        return Response(self.get_serializer(params).data)


# ═════════════════════════════════════════════════════════════════════════════
# Calendrier
# ═════════════════════════════════════════════════════════════════════════════
class SemestreIPGEIViewSet(_ErreurMetierMixin, InstitutionScopedMixin,
                           SelectAllMixin, ModelViewSet):
    """Semestres S1..S4 d'une année + génération des semaines."""
    serializer_class   = SemestreIPGEISerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_parametres'
    queryset           = SemestreIPGEI.objects.select_related('institution')
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['code', 'annee_universitaire', 'est_cloture']
    ordering_fields    = ['annee_universitaire', 'code', 'date_debut']
    ordering           = ['-annee_universitaire', 'code']
    pagination_class   = StandardPagination

    @action(detail=True, methods=['post'], url_path='generer-semaines')
    def generer_semaines(self, request, pk=None):
        """
        Ajoute des semaines à la suite de celles déjà générées.

        `nb_semaines` : combien en ajouter (défaut : celui du semestre).
        `remplacer=true` repart de zéro — seules les semaines sans séance sont
        supprimées, on ne détruit jamais un emploi du temps déjà posé.
        """
        semestre  = self.get_object()
        remplacer = bool(request.data.get('remplacer', False))
        nb        = request.data.get('nb_semaines')
        try:
            nb = int(nb) if nb not in (None, '') else None
        except (TypeError, ValueError):
            raise ValidationError({'nb_semaines': 'Nombre de semaines invalide.'})
        if nb is not None and not 1 <= nb <= 40:
            raise ValidationError({'nb_semaines': 'Attendu entre 1 et 40.'})

        creees = self._executer(
            services.generer_semaines, semestre, nb=nb, remplacer=remplacer,
        )
        return Response({'semaines_creees': creees, 'total': semestre.semaines.count()})

    @action(detail=True, methods=['get'], url_path='semaines')
    def semaines(self, request, pk=None):
        """
        Semaines du semestre.

        `?classe=` restreint l'état de cohérence à cette classe. Sans lui,
        l'état porte sur tout le niveau — ce qui ferait dire à une classe sans
        emploi du temps que son suivi est « à jour », alors qu'il décrit celui
        de sa voisine.
        """
        semestre = self.get_object()
        classe = Classe.objects.filter(pk=request.query_params.get('classe')).first()
        return Response(SemaineIPGEISerializer(
            semestre.semaines.all(), many=True,
            context={'classe_coherence': classe},
        ).data)

    @action(detail=True, methods=['post'], url_path='cloturer')
    def cloturer(self, request, pk=None):
        """Clôture le semestre : plus aucune saisie de note n'est acceptée."""
        semestre = self.get_object()
        semestre.est_cloture = not semestre.est_cloture
        semestre.save(update_fields=['est_cloture'])
        return Response(self.get_serializer(semestre).data)


class SemaineIPGEIViewSet(SelectAllMixin, ModelViewSet):
    """Semaines pédagogiques — qualification (cours / vacances / examens)."""
    serializer_class   = SemaineIPGEISerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_parametres'
    queryset           = SemaineIPGEI.objects.select_related('semestre')
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['semestre', 'type_semaine', 'numero']
    ordering           = ['semestre', 'date_debut']
    pagination_class   = NoPagination


# ═════════════════════════════════════════════════════════════════════════════
# Structure
# ═════════════════════════════════════════════════════════════════════════════
class ClasseViewSet(InstitutionScopedMixin, SelectAllMixin, ModelViewSet):
    """Classes MPSI/MP d'une année universitaire."""
    serializer_class   = ClasseSerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_classes'
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class    = ClasseFilter
    # `libelle` et `niveau` sont desormais portes par le parent `Departement` :
    # `groupe` pour le suffixe de classe, `niveau__niveau` pour le code.
    search_fields      = ['groupe', 'niveau__niveau', 'nom']
    ordering_fields    = ['annee_universitaire', 'niveau__niveau', 'groupe']
    ordering           = ['-annee_universitaire', 'niveau__niveau', 'groupe']
    pagination_class   = StandardPagination
    queryset           = (
        Classe.objects
        .select_related('institution', 'professeur_principal')
        .prefetch_related('sous_groupes__matieres')
        # Nom distinct de la @property `effectif` du modele : Django refuse
        # d'ecraser un attribut de classe par une annotation.
        .annotate(effectif_annote=Count('inscriptions', filter=Q(inscriptions__actif=True), distinct=True))
    )

    @action(detail=False, methods=['get'], url_path='select', pagination_class=NoPagination)
    def select(self, request):
        """Liste légère pour les listes déroulantes."""
        qs = self.filter_queryset(self.get_queryset()).filter(actif=True)
        return Response(ClasseSelectSerializer(qs, many=True).data)

    @action(detail=True, methods=['get'], url_path='etudiants', pagination_class=NoPagination)
    def etudiants(self, request, pk=None):
        """Effectif de la classe (inscriptions actives)."""
        classe = self.get_object()
        qs = (
            classe.inscriptions.filter(actif=True)
            .select_related('etudiant', 'classe', 'sous_groupe')
        )
        return Response(InscriptionIPGEISerializer(qs, many=True).data)


class SousGroupeTPViewSet(ModelViewSet):
    """Sous-groupes de TP (fixes sur le semestre)."""
    serializer_class   = SousGroupeTPSerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_classes'
    queryset           = SousGroupeTP.objects.select_related('classe').prefetch_related('matieres')
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['classe']
    ordering           = ['classe', 'groupe']
    pagination_class   = NoPagination


class InscriptionIPGEIViewSet(_ErreurMetierMixin, SelectAllMixin, ModelViewSet):
    """Rattachement des étudiants aux classes."""
    serializer_class   = InscriptionIPGEISerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_inscriptions'
    queryset           = (
        InscriptionIPGEI.objects
        .select_related('etudiant', 'classe', 'sous_groupe')
    )
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class    = InscriptionIPGEIFilter
    search_fields      = ['etudiant__nom', 'etudiant__nom_fr', 'etudiant__matricule']
    ordering_fields    = ['numero_ordre', 'etudiant__nom', 'date_inscription']
    ordering           = ['classe', 'numero_ordre', 'etudiant__nom']
    pagination_class   = StandardPagination

    @action(detail=False, methods=['post'], url_path='nouvelle')
    def nouvelle(self, request):
        """
        Inscription complète : crée au besoin la fiche étudiant, puis la
        rattache à sa classe — le tout dans une seule transaction.

        C'est le point d'entrée normal d'une rentrée : sans lui, il faudrait
        créer l'étudiant dans le référentiel du socle avant de pouvoir
        l'inscrire, et un échec entre les deux laisserait une fiche orpheline
        dont le matricule serait déjà consommé.
        """
        from apps.absence.models import Etudiant

        entree = InscriptionCompleteSerializer(data=request.data)
        entree.is_valid(raise_exception=True)
        donnees = entree.validated_data

        classe = Classe.objects.filter(pk=donnees['classe']).first()
        if classe is None:
            raise ValidationError({'classe': 'Classe introuvable.'})

        sous_groupe_id = donnees.get('sous_groupe')
        if sous_groupe_id and not SousGroupeTP.objects.filter(
            pk=sous_groupe_id, classe=classe,
        ).exists():
            raise ValidationError(
                {'sous_groupe': "Ce sous-groupe n'appartient pas à la classe choisie."}
            )

        with transaction.atomic():
            if donnees.get('etudiant'):
                etudiant = Etudiant.objects.filter(pk=donnees['etudiant']).first()
                if etudiant is None:
                    raise ValidationError({'etudiant': 'Étudiant introuvable.'})
            else:
                identite = dict(donnees['nouvel_etudiant'])
                # `nom_fr` alimente `nom_display`, utilisé par les relevés et la
                # page publique de vérification : on le renseigne dès la création.
                etudiant = Etudiant.objects.create(
                    nom_fr=identite['nom'], statut='actif', **identite,
                )

            if InscriptionIPGEI.objects.filter(
                etudiant=etudiant, annee_universitaire=classe.annee_universitaire,
            ).exists():
                raise ValidationError(
                    f'{etudiant.nom_display} est déjà inscrit pour '
                    f'{classe.annee_universitaire}.'
                )

            inscription = InscriptionIPGEI.objects.create(
                etudiant=etudiant,
                classe=classe,
                sous_groupe_id=sous_groupe_id or None,
                numero_ordre=donnees.get('numero_ordre'),
                annee_universitaire=classe.annee_universitaire,
            )

        return Response(
            InscriptionIPGEISerializer(inscription).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['post'], url_path='initialiser-notes')
    def initialiser_notes(self, request, pk=None):
        """Crée les lignes de note manquantes pour un semestre donné."""
        inscription = self.get_object()
        semestre_id = request.data.get('semestre')
        if not semestre_id:
            raise ValidationError({'semestre': 'Semestre requis.'})
        semestre = SemestreIPGEI.objects.filter(pk=semestre_id).first()
        if semestre is None:
            raise ValidationError({'semestre': 'Semestre introuvable.'})
        creees = services.initialiser_notes(inscription, semestre)
        return Response({'notes_creees': creees})

    @action(detail=True, methods=['get'], url_path='releve')
    def releve(self, request, pk=None):
        """Relevé de semestre ou relevé annuel (données brutes, JSON)."""
        inscription = self.get_object()
        semestre_id = request.query_params.get('semestre')
        if semestre_id:
            semestre = SemestreIPGEI.objects.filter(pk=semestre_id).first()
            if semestre is None:
                raise ValidationError({'semestre': 'Semestre introuvable.'})
            data = services.releve_semestre(inscription, semestre)
            return Response(_serialiser_releve_semestre(data))
        data = services.releve_annuel(inscription, inscription.annee_universitaire)
        return Response(_serialiser_releve_annuel(data))

    @action(detail=True, methods=['get'], url_path='historique-classes', pagination_class=NoPagination)
    def historique_classes(self, request, pk=None):
        inscription = self.get_object()
        qs = inscription.historique_classes.select_related('classe_avant', 'classe_apres', 'decide_par')
        return Response(HistoriqueClasseSerializer(qs, many=True).data)

    @action(detail=True, methods=['get'], url_path='absences')
    def absences(self, request, pk=None):
        """Bilan d'assiduité, éventuellement borné à un semestre."""
        inscription = self.get_object()
        semestre = None
        if request.query_params.get('semestre'):
            semestre = SemestreIPGEI.objects.filter(pk=request.query_params['semestre']).first()
        detail = (
            AbsenceSeance.objects.filter(inscription=inscription)
            .select_related('seance__matiere', 'seance__creneau')
            .order_by('-seance__date')
        )
        if semestre is not None:
            detail = detail.filter(seance__semaine__semestre=semestre)
        return Response({
            'bilan':  services.bilan_absences(inscription, semestre),
            'detail': AbsenceSeanceSerializer(detail[:200], many=True).data,
        })


def _serialiser_releve_semestre(data: dict) -> dict:
    return {
        'etudiant': {
            'id':        data['inscription'].etudiant_id,
            'nom':       data['inscription'].etudiant.nom_display,
            'matricule': data['inscription'].etudiant.matricule,
        },
        'classe':   data['inscription'].classe.nom,
        'semestre': data['semestre'].code,
        'annee_universitaire': data['semestre'].annee_universitaire,
        'notes':    NoteSerializer(data['notes'], many=True).data,
        'moyenne':  data['moyenne'],
        'total_coefficients': data['total_coefficients'],
        'seuil':    data['seuil'],
        'valide':   data['valide'],
        'absences': data['absences'],
    }


def _serialiser_releve_annuel(data: dict) -> dict:
    return {
        'etudiant': {
            'id':        data['inscription'].etudiant_id,
            'nom':       data['inscription'].etudiant.nom_display,
            'matricule': data['inscription'].etudiant.matricule,
        },
        'classe':   data['inscription'].classe.nom,
        'annee_universitaire': data['annee_universitaire'],
        'semestres': [
            {
                'code':    s['code'],
                'moyenne': s['moyenne'],
                'notes':   NoteSerializer(s['notes'], many=True).data,
            }
            for s in data['semestres']
        ],
        'moyenne':  data['moyenne'],
        'total_coefficients': data['total_coefficients'],
        'seuil':    data['seuil'],
        'valide':   data['valide'],
        'absences': data['absences'],
    }


# ═════════════════════════════════════════════════════════════════════════════
# Matières
# ═════════════════════════════════════════════════════════════════════════════
class MatiereViewSet(InstitutionScopedMixin, SelectAllMixin, ModelViewSet):
    """Matières du cursus : coefficient, volume horaire, pondération DS/TP/examen."""
    serializer_class   = MatiereSerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_matieres'
    queryset           = Matiere.objects.select_related('institution')
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields   = ['code_semestre', 'has_tp', 'actif']
    search_fields      = ['code', 'intitule']
    ordering_fields    = ['code_semestre', 'ordre', 'code', 'coefficient']
    ordering           = ['code_semestre', 'ordre', 'code']
    pagination_class   = StandardPagination

    @action(detail=False, methods=['get'], url_path='select', pagination_class=NoPagination)
    def select(self, request):
        qs = self.filter_queryset(self.get_queryset()).filter(actif=True)
        return Response(MatiereSelectSerializer(qs, many=True).data)

    @action(detail=True, methods=['post'], url_path='reinitialiser-ponderation')
    def reinitialiser_ponderation(self, request, pk=None):
        """Remet la pondération du cadrage (30/70 sans TP, 20/10/70 avec TP)."""
        from .models import PCT_DEFAUT_AVEC_TP, PCT_DEFAUT_SANS_TP
        matiere = self.get_object()
        defauts = PCT_DEFAUT_AVEC_TP if matiere.has_tp else PCT_DEFAUT_SANS_TP
        for champ, valeur in defauts.items():
            setattr(matiere, champ, valeur)
        matiere.save(update_fields=list(defauts))
        return Response(self.get_serializer(matiere).data)


# ═════════════════════════════════════════════════════════════════════════════
# Notes
# ═════════════════════════════════════════════════════════════════════════════
class NoteViewSet(_ErreurMetierMixin, ModelViewSet):
    """
    Notes par (étudiant × matière × semestre), avec DS et examens en nombre libre.
    """
    serializer_class   = NoteSerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_notes'
    queryset           = (
        Note.objects
        .select_related('inscription__etudiant', 'inscription__classe', 'matiere', 'semestre')
        .prefetch_related('evaluations')
    )
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields   = ['inscription', 'matiere', 'semestre', 'inscription__classe', 'verrouillee']
    search_fields      = ['inscription__etudiant__nom', 'inscription__etudiant__matricule']
    ordering           = ['inscription', 'matiere__ordre']
    pagination_class   = StandardPagination

    @action(detail=True, methods=['post'], url_path='recalculer')
    def recalculer(self, request, pk=None):
        note = self.get_object()
        plafond = _plafond_courant()
        self._executer(services.calculer_note, note, plafond_rattrapage=plafond)
        note.refresh_from_db()
        return Response(self.get_serializer(note).data)

    @action(detail=False, methods=['get'], url_path='grille')
    def grille(self, request):
        """
        Grille de saisie : une classe × une matière × un semestre.
        Crée à la volée les lignes manquantes pour que la grille soit complète
        dès le premier affichage.
        """
        classe_id   = request.query_params.get('classe')
        matiere_id  = request.query_params.get('matiere')
        semestre_id = request.query_params.get('semestre')
        if not (classe_id and matiere_id and semestre_id):
            raise ValidationError('Paramètres requis : classe, matiere, semestre.')

        matiere  = Matiere.objects.filter(pk=matiere_id).first()
        semestre = SemestreIPGEI.objects.filter(pk=semestre_id).first()
        if matiere is None or semestre is None:
            raise ValidationError('Matière ou semestre introuvable.')
        if matiere.code_semestre != semestre.code:
            raise ValidationError(
                f'La matière {matiere.code} relève du semestre {matiere.code_semestre}.'
            )

        inscriptions = (
            InscriptionIPGEI.objects
            .filter(classe_id=classe_id, actif=True)
            .select_related('etudiant')
            .order_by('numero_ordre', 'etudiant__nom')
        )
        with transaction.atomic():
            existantes = set(
                Note.objects.filter(inscription__in=inscriptions, matiere=matiere, semestre=semestre)
                .values_list('inscription_id', flat=True)
            )
            manquantes = [
                Note(inscription=i, matiere=matiere, semestre=semestre)
                for i in inscriptions if i.id not in existantes
            ]
            if manquantes and not semestre.est_cloture:
                Note.objects.bulk_create(manquantes)

        notes = (
            Note.objects
            .filter(inscription__in=inscriptions, matiere=matiere, semestre=semestre)
            .select_related('inscription__etudiant', 'matiere', 'semestre')
            .prefetch_related('evaluations')
            .order_by('inscription__numero_ordre', 'inscription__etudiant__nom')
        )
        # Nombre de colonnes DS / examens = le max déjà saisi dans la classe,
        # pour que la grille s'élargisse d'elle-même quand on ajoute un DS.
        numeros = {'ds': 0, 'exam': 0}
        for n in notes:
            for e in n.evaluations.all():
                numeros[e.type_evaluation] = max(numeros[e.type_evaluation], e.numero)

        return Response({
            'matiere':   MatiereSerializer(matiere).data,
            'semestre':  SemestreIPGEISerializer(semestre).data,
            'nb_ds':     max(1, numeros['ds']),
            'nb_examens': max(1, numeros['exam']),
            'verrouillee': semestre.est_cloture,
            'notes':     NoteSerializer(notes, many=True).data,
        })

    @action(detail=False, methods=['post'], url_path='saisie-collective')
    def saisie_collective(self, request):
        """
        Enregistre toute une grille en une requête, puis recalcule les lignes.
        Une valeur `null` supprime l'évaluation correspondante — c'est ainsi
        qu'on efface un DS saisi par erreur.
        """
        entree = SaisieCollectiveSerializer(data=request.data)
        entree.is_valid(raise_exception=True)
        donnees = entree.validated_data

        matiere  = Matiere.objects.filter(pk=donnees['matiere']).first()
        semestre = SemestreIPGEI.objects.filter(pk=donnees['semestre']).first()
        if matiere is None or semestre is None:
            raise ValidationError('Matière ou semestre introuvable.')
        if semestre.est_cloture:
            raise ValidationError('Semestre clôturé — saisie impossible.')

        utilisateur = _utilisateur(request)
        plafond     = _plafond_courant()
        traitees    = 0

        with transaction.atomic():
            for ligne in donnees['lignes']:
                note, _ = Note.objects.get_or_create(
                    inscription_id=ligne['inscription'], matiere=matiere, semestre=semestre,
                )
                if note.verrouillee:
                    continue

                if 'note_tp' in ligne:
                    note.note_tp = ligne['note_tp'] if matiere.has_tp else None
                if 'note_rattrapage' in ligne:
                    note.note_rattrapage = ligne['note_rattrapage']
                note.save(update_fields=['note_tp', 'note_rattrapage', 'date_modification'])

                for ev in ligne.get('evaluations', []):
                    cle = {
                        'note': note,
                        'type_evaluation': ev['type_evaluation'],
                        'numero': ev['numero'],
                    }
                    if ev['valeur'] is None:
                        Evaluation.objects.filter(**cle).delete()
                    else:
                        Evaluation.objects.update_or_create(
                            **cle, defaults={'valeur': ev['valeur'], 'saisie_par': utilisateur},
                        )

                self._executer(services.calculer_note, note, plafond_rattrapage=plafond)
                traitees += 1

        return Response({'lignes_traitees': traitees})

    @action(detail=False, methods=['post'], url_path='recalculer-lot')
    def recalculer_lot(self, request):
        """Recalcule toutes les notes d'un semestre (et d'une classe si fournie)."""
        semestre_id = request.data.get('semestre')
        if not semestre_id:
            raise ValidationError({'semestre': 'Semestre requis.'})
        qs = Note.objects.filter(semestre_id=semestre_id)
        if request.data.get('classe'):
            qs = qs.filter(inscription__classe_id=request.data['classe'])
        total = self._executer(services.recalculer_notes, qs, plafond_rattrapage=_plafond_courant())
        return Response({'notes_recalculees': total})


def _plafond_courant():
    institution = _institution_principale()
    if institution is None:
        return None
    return ParametresIPGEI.pour(institution).plafond_rattrapage


class EvaluationViewSet(ModelViewSet):
    """DS et examens individuels — utile pour corriger une note isolée."""
    serializer_class   = EvaluationSerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_notes'
    queryset           = Evaluation.objects.select_related('note__matiere')
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['note', 'type_evaluation']
    pagination_class   = NoPagination

    def perform_create(self, serializer):
        evaluation = serializer.save(saisie_par=_utilisateur(self.request))
        services.calculer_note(evaluation.note, plafond_rattrapage=_plafond_courant())

    def perform_update(self, serializer):
        evaluation = serializer.save()
        services.calculer_note(evaluation.note, plafond_rattrapage=_plafond_courant())

    def perform_destroy(self, instance):
        note = instance.note
        instance.delete()
        if not note.verrouillee:
            services.calculer_note(note, plafond_rattrapage=_plafond_courant())


# ═════════════════════════════════════════════════════════════════════════════
# Délibération
# ═════════════════════════════════════════════════════════════════════════════
class DeliberationViewSet(_ErreurMetierMixin, InstitutionScopedMixin, ModelViewSet):
    """Jurys IPGEI : calcul des moyennes, décisions, validation."""
    serializer_class   = DeliberationSerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_deliberation'
    queryset           = Deliberation.objects.select_related('institution', 'semestre', 'validee_par')
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['niveau', 'annee_universitaire', 'portee', 'statut', 'semestre']
    ordering           = ['-annee_universitaire', 'niveau']
    pagination_class   = StandardPagination

    def perform_create(self, serializer):
        institution = serializer.validated_data.get('institution') or _institution_principale()
        params = ParametresIPGEI.pour(institution) if institution else None
        # Le seuil et le plafond sont recopiés des paramètres SI le client ne
        # les impose pas : la délibération devient alors autoportante.
        extra = {'institution': institution}
        if params is not None:
            if 'seuil_validation' not in serializer.validated_data:
                extra['seuil_validation'] = params.seuil_validation
            if 'plafond_rattrapage' not in serializer.validated_data:
                extra['plafond_rattrapage'] = params.plafond_rattrapage
        serializer.save(**extra)

    def perform_destroy(self, instance):
        if instance.est_verrouillee:
            raise ValidationError('Délibération validée — suppression interdite.')
        instance.delete()

    @action(detail=True, methods=['post'], url_path='calculer')
    def calculer(self, request, pk=None):
        deliberation = self.get_object()
        total = self._executer(services.calculer_deliberation, deliberation)
        deliberation.refresh_from_db()
        return Response({
            'lignes': total,
            'deliberation': self.get_serializer(deliberation).data,
        })

    @action(detail=True, methods=['post'], url_path='valider')
    def valider(self, request, pk=None):
        deliberation = self.get_object()
        self._executer(services.valider_deliberation, deliberation, _utilisateur(request))
        deliberation.refresh_from_db()
        return Response(self.get_serializer(deliberation).data)

    @action(detail=True, methods=['get'], url_path='lignes', pagination_class=NoPagination)
    def lignes(self, request, pk=None):
        deliberation = self.get_object()
        qs = (
            deliberation.lignes
            .select_related('inscription__etudiant', 'inscription__classe')
            .order_by('rang', '-moyenne_generale')
        )
        if request.query_params.get('classe'):
            qs = qs.filter(inscription__classe_id=request.query_params['classe'])
        return Response(LigneDeliberationListSerializer(qs, many=True).data)

    @action(detail=True, methods=['get'], url_path='statistiques')
    def statistiques(self, request, pk=None):
        """Répartition des décisions — en-tête du PV de jury."""
        deliberation = self.get_object()
        lignes = list(deliberation.lignes.all())
        moyennes = [Decimal(l.moyenne_generale) for l in lignes if l.moyenne_generale is not None]
        repartition = {}
        for ligne in lignes:
            cle = ligne.decision or 'sans_decision'
            repartition[cle] = repartition.get(cle, 0) + 1
        return Response({
            'effectif':     len(lignes),
            'notes_saisies': len(moyennes),
            'moyenne_promo': (sum(moyennes) / len(moyennes)).quantize(Decimal('0.01')) if moyennes else None,
            'meilleure':    max(moyennes) if moyennes else None,
            'plus_faible':  min(moyennes) if moyennes else None,
            'repartition':  repartition,
        })


class LigneDeliberationViewSet(ListModelMixin, RetrieveModelMixin, UpdateModelMixin, GenericViewSet):
    """Ajustement des décisions par le jury (motif obligatoire si dérogation)."""
    serializer_class   = LigneDeliberationSerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_deliberation'
    queryset           = (
        LigneDeliberation.objects
        .select_related('deliberation', 'inscription__etudiant', 'inscription__classe')
        .prefetch_related('details__matiere', 'details__semestre')
    )
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields   = ['deliberation', 'decision', 'inscription__classe']
    search_fields      = ['inscription__etudiant__nom', 'inscription__etudiant__matricule']
    ordering           = ['rang']
    pagination_class   = StandardPagination


# ═════════════════════════════════════════════════════════════════════════════
# EDT — grille type et séances réelles
# ═════════════════════════════════════════════════════════════════════════════
class GrilleTypeViewSet(_ErreurMetierMixin, ModelViewSet):
    """Grille type d'une classe (patron dupliqué sur les semaines)."""
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_edt'
    queryset           = (
        GrilleType.objects
        .select_related('classe')
        .prefetch_related('seances__matiere', 'seances__prof', 'seances__salle',
                          'seances__jour', 'seances__creneau', 'seances__sous_groupe')
    )
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['classe', 'type_semestre', 'actif']
    pagination_class   = NoPagination

    def get_serializer_class(self):
        return GrilleTypeSerializer if self.action in (
            'retrieve', 'create', 'update', 'partial_update', 'pour_classe',
        ) else GrilleTypeListSerializer

    @action(detail=False, methods=['get'], url_path='pour-classe')
    def pour_classe(self, request):
        """
        Grille d'une classe pour un type de semestre, créée à la volée si besoin.

        Choisir la classe suffit : il n'existe qu'une grille possible par
        (classe × type de semestre), donc demander en plus à l'utilisateur de la
        créer ne lui fait rien décider — c'est une étape sans information.
        """
        classe_id     = request.query_params.get('classe')
        type_semestre = request.query_params.get('type_semestre', 'I')
        if not classe_id:
            raise ValidationError({'classe': 'Classe requise.'})
        if type_semestre not in ('I', 'P'):
            raise ValidationError({'type_semestre': "Type de semestre attendu : 'I' ou 'P'."})

        classe = Classe.objects.filter(pk=classe_id).first()
        if classe is None:
            raise ValidationError({'classe': 'Classe introuvable.'})

        grille, _ = GrilleType.objects.get_or_create(
            classe=classe, type_semestre=type_semestre,
            defaults={'libelle': f'Grille {classe.nom} — semestre '
                                 f'{"impair" if type_semestre == "I" else "pair"}'},
        )
        return Response(GrilleTypeSerializer(grille).data)

    @action(detail=True, methods=['post'], url_path='dupliquer')
    def dupliquer(self, request, pk=None):
        """
        Duplique la grille sur tout le semestre, ou sur un lot de N semaines
        à partir d'une semaine donnée (plan §4.5).
        """
        grille = self.get_object()
        entree = DupliquerGrilleSerializer(data=request.data)
        entree.is_valid(raise_exception=True)
        donnees = entree.validated_data

        semestre = SemestreIPGEI.objects.filter(pk=donnees['semestre']).first()
        if semestre is None:
            raise ValidationError({'semestre': 'Semestre introuvable.'})
        if semestre.type_semestre != grille.type_semestre:
            raise ValidationError(
                f'La grille vise un semestre de type {grille.get_type_semestre_display()}, '
                f'or {semestre.code} est de type {semestre.type_semestre}.'
            )

        if donnees.get('semaine_debut'):
            depart = SemaineIPGEI.objects.filter(
                pk=donnees['semaine_debut'], semestre=semestre,
            ).first()
            if depart is None:
                raise ValidationError({'semaine_debut': 'Semaine introuvable dans ce semestre.'})
            semaines = services.semaines_du_lot(depart, donnees.get('nb_semaines') or 1)
        else:
            semaines = list(semestre.semaines.filter(type_semaine=SemaineIPGEI.TYPE_COURS))

        resultat = self._executer(
            services.dupliquer_grille, grille, semaines, ecraser=donnees['ecraser'],
        )
        resultat['semaines_traitees'] = len(semaines)
        return Response(resultat)


class SeanceTypeViewSet(ModelViewSet):
    """Cases de la grille type."""
    serializer_class   = SeanceTypeSerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_edt'
    queryset           = SeanceType.objects.select_related(
        'grille__classe', 'jour', 'creneau', 'matiere', 'prof', 'salle', 'sous_groupe',
    )
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['grille', 'jour', 'creneau', 'matiere', 'prof']
    pagination_class   = NoPagination


class SeanceReelleViewSet(_ErreurMetierMixin, ModelViewSet):
    """
    Séances effectives d'une semaine. Support de l'édition unitaire et de
    l'édition « en lot » sur N semaines.
    """
    serializer_class   = SeanceReelleSerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_edt'
    queryset           = SeanceReelle.objects.select_related(
        'classe', 'semaine__semestre', 'jour', 'creneau', 'matiere',
        'prof', 'prof_initial', 'salle', 'sous_groupe',
    ).annotate(nb_absents_annote=Count('absences', distinct=True))
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['classe', 'semaine', 'semaine__semestre', 'prof', 'matiere',
                          'salle', 'jour', 'annulee', 'sous_groupe']
    ordering           = ['semaine', 'jour_id', 'creneau__ordre']
    pagination_class   = NoPagination

    @action(detail=False, methods=['get'], url_path='semaine')
    def par_semaine(self, request):
        """EDT d'une classe pour une semaine — vue principale de l'écran EDT."""
        classe_id  = request.query_params.get('classe')
        semaine_id = request.query_params.get('semaine')
        if not (classe_id and semaine_id):
            raise ValidationError('Paramètres requis : classe, semaine.')
        qs = self.get_queryset().filter(classe_id=classe_id, semaine_id=semaine_id)
        return Response(self.get_serializer(qs, many=True).data)

    @action(detail=False, methods=['get'], url_path='occupation')
    def occupation(self, request):
        """
        Enseignants et salles déjà pris, sur les AUTRES classes.

        L'écran de saisie retirait déjà de la liste un enseignant occupé, mais
        il ne connaissait que la grille affichée : en passant d'une classe à
        l'autre il repartait de zéro, et le même professeur pouvait être placé
        deux fois sur le même créneau. La contrainte est de portée
        établissement, elle ne peut donc venir que du serveur.

        Le périmètre suit celui de l'écran : une semaine précise si l'on édite
        une semaine, sinon toutes les grilles types de la même année et du même
        type de semestre — deux grilles types qui se recouvrent produiraient le
        conflit à chaque semaine dupliquée.
        """
        classe_id = request.query_params.get('classe')
        if not classe_id:
            raise ValidationError({'classe': 'Classe requise.'})
        classe = Classe.objects.filter(pk=classe_id).first()
        if classe is None:
            raise ValidationError({'classe': 'Classe introuvable.'})

        semaine_id = request.query_params.get('semaine')
        if semaine_id:
            seances = (
                SeanceReelle.objects
                .filter(semaine_id=semaine_id, annulee=False)
                .exclude(classe_id=classe.pk)
                .select_related('classe', 'prof', 'salle', 'sous_groupe')
            )
            contexte = lambda s: s.classe.nom            # noqa: E731
        else:
            type_semestre = request.query_params.get('type_semestre', 'I')
            if type_semestre not in ('I', 'P'):
                raise ValidationError(
                    {'type_semestre': "Type de semestre attendu : 'I' ou 'P'."})
            seances = (
                SeanceType.objects
                .filter(grille__type_semestre=type_semestre,
                        grille__classe__annee_universitaire=classe.annee_universitaire)
                .exclude(grille__classe_id=classe.pk)
                .select_related('grille__classe', 'prof', 'salle', 'sous_groupe')
            )
            contexte = lambda s: s.grille.classe.nom     # noqa: E731

        # Le front a besoin de savoir OÙ la personne est prise : masquer un nom
        # sans pouvoir dire pourquoi rend l'écran incompréhensible.
        return Response([
            {
                'jour':    s.jour_id,
                'creneau': s.creneau_id,
                'prof':    s.prof_id,
                'salle':   s.salle_id,
                'classe':  contexte(s) + (f' ({s.sous_groupe.groupe})'
                                          if s.sous_groupe_id else ''),
            }
            for s in seances if s.prof_id or s.salle_id
        ])

    # ── Impression ───────────────────────────────────────────────────────────
    def _semaine_demandee(self, request):
        """Semaine du calendrier IPGEI passée en paramètre, ou 400."""
        semaine_id = request.query_params.get('semaine')
        if not semaine_id:
            raise ValidationError('Paramètre requis : semaine.')
        semaine = (
            SemaineIPGEI.objects.select_related('semestre')
            .filter(pk=semaine_id).first()
        )
        if semaine is None:
            raise ValidationError("Cette semaine n'existe pas.")
        return semaine

    @action(detail=False, methods=['get'], url_path='pdf-classe')
    def pdf_classe(self, request):
        """
        Emploi du temps d'une classe, au gabarit du socle.

        `sous_groupe` restreint au plan d'un groupe : les cours de la classe
        entière y sont conservés — un étudiant de G1 les suit — et seules les
        séances de l'autre groupe sont écartées. Une séance propre au groupe
        remplace celle de la classe sur la même case horaire.
        """
        classe = Classe.objects.filter(pk=request.query_params.get('classe')).first()
        if classe is None:
            raise ValidationError('Paramètre requis : classe.')
        semaine     = self._semaine_demandee(request)
        sous_groupe = request.query_params.get('sous_groupe') or ''

        seances = list(self.get_queryset().filter(classe=classe, semaine=semaine))
        libelle = classe.nom
        if sous_groupe:
            propres = [s for s in seances if str(s.sous_groupe_id) == sous_groupe]
            occupees = {(s.jour_id, s.creneau_id) for s in propres}
            seances = propres + [
                s for s in seances
                if s.sous_groupe_id is None and (s.jour_id, s.creneau_id) not in occupees
            ]
            groupe = SousGroupeTP.objects.filter(pk=sous_groupe).first()
            if groupe:
                libelle = f'{classe.nom} — sous-groupe {groupe.groupe}'

        creneaux, lignes = edt_pdf.construire_grille(seances, axe='classe')
        return edt_pdf.reponse_pdf(
            'ipgei/emploi_pdf.html',
            {
                'annee_universitaire': classe.annee_universitaire,
                'cible_libelle':       'Classe',
                'cible':               libelle,
                'creneaux':            creneaux,
                'rows':                lignes,
                **edt_pdf.contexte_semaine(semaine),
            },
            f'Emploi_{_nom_fichier(libelle)}_S{semaine.numero or 0}.pdf',
        )

    @action(detail=False, methods=['get'], url_path='pdf-prof')
    def pdf_prof(self, request):
        """Emploi du temps d'un enseignant, toutes classes confondues."""
        prof = Prof.objects.filter(pk=request.query_params.get('prof')).first()
        if prof is None:
            raise ValidationError('Paramètre requis : prof.')
        semaine = self._semaine_demandee(request)

        seances = list(self.get_queryset().filter(prof=prof, semaine=semaine))
        creneaux, lignes = edt_pdf.construire_grille(seances, axe='prof')
        return edt_pdf.reponse_pdf(
            'ipgei/emploi_pdf.html',
            {
                'annee_universitaire': semaine.semestre.annee_universitaire,
                'cible_libelle':       'Enseignant',
                'cible':               prof.nom,
                'creneaux':            creneaux,
                'rows':                lignes,
                **edt_pdf.contexte_semaine(semaine),
            },
            f'Emploi_{_nom_fichier(prof.nom)}_S{semaine.numero or 0}.pdf',
        )

    @action(detail=False, methods=['get'], url_path='pdf-salle')
    def pdf_salle(self, request):
        """Occupation d'une salle : les cases vides sont les créneaux libres."""
        salle = Salle.objects.filter(pk=request.query_params.get('salle')).first()
        if salle is None:
            raise ValidationError('Paramètre requis : salle.')
        semaine = self._semaine_demandee(request)

        seances = list(self.get_queryset().filter(salle=salle, semaine=semaine))
        # La salle est déjà le sujet du document : la répéter dans chaque case
        # consommerait de la place sans rien apprendre.
        creneaux, lignes = edt_pdf.construire_grille(seances, axe='salle')
        return edt_pdf.reponse_pdf(
            'ipgei/emploi_pdf.html',
            {
                'annee_universitaire': semaine.semestre.annee_universitaire,
                'cible_libelle':       'Salle',
                'cible':               salle.nom,
                'creneaux':            creneaux,
                'rows':                lignes,
                **edt_pdf.contexte_semaine(semaine),
            },
            f'Occupation_{_nom_fichier(salle.nom)}_S{semaine.numero or 0}.pdf',
        )

    @action(detail=True, methods=['post'], url_path='appliquer-lot')
    def appliquer_lot(self, request, pk=None):
        """
        Reporte une séance sur les N semaines suivantes, sur la même case
        (jour × créneau × sous-groupe).

        Là où la case est déjà occupée, elle est modifiée. Là où elle est
        **vide, la séance est créée** : une séance ajoutée à la main sur une
        semaine ne se reportait sinon nulle part, puisqu'il n'y avait rien à
        mettre à jour — le report semblait sans effet.

        `semaines_du_lot` ne retient que les semaines de COURS : aucune séance
        n'est posée sur des vacances ou des examens.
        """
        seance = self.get_object()
        entree = EditionLotSerializer(data=request.data)
        entree.is_valid(raise_exception=True)
        donnees = entree.validated_data

        semaines = services.semaines_du_lot(seance.semaine, donnees['nb_semaines'])
        champs = {
            cle: donnees[cle]
            for cle in ('prof', 'salle', 'matiere', 'annulee')
            if cle in donnees
        }
        if not champs:
            raise ValidationError('Aucune modification demandée.')

        maj = {}
        for cle, valeur in champs.items():
            maj[cle if cle == 'annulee' else f'{cle}_id'] = valeur

        case = dict(classe=seance.classe, jour=seance.jour, creneau=seance.creneau,
                    sous_groupe=seance.sous_groupe)

        # Sauvegardes unitaires plutot qu'un `QuerySet.update()` : ce dernier
        # n'emet aucun signal, donc ni journal d'audit ni horodatage
        # `modifiee_le`. Le report en lot echappait ainsi a toute tracabilite,
        # et la semaine ne se declarait pas divergente. Le volume est celui
        # d'un lot de semaines, jamais massif.
        modifiees = 0
        for cible in SeanceReelle.objects.filter(**case, semaine__in=semaines):
            for champ, valeur in maj.items():
                setattr(cible, champ, valeur)
            cible.save(update_fields=[*maj.keys(), 'modifiee_le'])
            modifiees += 1

        # Semaines du lot où la case restait vide : on y pose la séance.
        deja = set(
            SeanceReelle.objects.filter(**case, semaine__in=semaines)
            .values_list('semaine_id', flat=True)
        )
        creees = 0
        for semaine in semaines:
            if semaine.pk in deja:
                continue
            SeanceReelle.objects.create(
                **case, semaine=semaine,
                matiere_id=maj.get('matiere_id', seance.matiere_id),
                prof_id=maj.get('prof_id', seance.prof_id),
                salle_id=maj.get('salle_id', seance.salle_id),
                type_seance=seance.type_seance,
                annulee=maj.get('annulee', False),
                date=services._date_seance(semaine, seance.jour),
                origine=SeanceReelle.ORIGINE_GRILLE,
                seance_type=seance.seance_type,
            )
            creees += 1

        return Response({
            'seances_modifiees': modifiees,
            'seances_creees':    creees,
            'semaines_traitees': len(semaines),
        })

    @action(detail=True, methods=['get', 'post'], url_path='absences')
    def absences(self, request, pk=None):
        """
        Feuille d'appel de la séance.
        GET  → liste de la classe + absents déjà saisis.
        POST → enregistre la saisie par exception (n'envoyer que les absents).
        """
        seance = self.get_object()
        if request.method == 'POST':
            entree = SaisieAbsencesSerializer(data=request.data)
            entree.is_valid(raise_exception=True)
            resultat = self._executer(
                services.saisir_absences, seance,
                entree.validated_data['absents'], _utilisateur(request),
            )
            return Response(resultat)

        # Une séance de TP ne concerne que son sous-groupe : l'appel doit
        # refléter exactement qui devait être présent.
        inscriptions = InscriptionIPGEI.objects.filter(classe=seance.classe, actif=True)
        if seance.sous_groupe_id:
            inscriptions = inscriptions.filter(sous_groupe_id=seance.sous_groupe_id)
        inscriptions = inscriptions.select_related('etudiant').order_by(
            'numero_ordre', 'etudiant__nom',
        )
        return Response({
            'seance':  self.get_serializer(seance).data,
            'classe':  InscriptionIPGEISerializer(inscriptions, many=True).data,
            'absents': AbsenceSeanceSerializer(
                seance.absences.select_related('inscription__etudiant'), many=True,
            ).data,
        })



class EmploiArchiveViewSet(ReadOnlyModelViewSet):
    """
    Emplois du temps figés à la génération du suivi — lecture seule.

    On ne modifie pas une archive : la seule écriture possible est la prise de
    vue, faite par `apps.ipgei.archive` au moment où le suivi est généré.
    """
    serializer_class   = EmploiArchiveSerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_edt'
    queryset           = EmploiArchive.objects.all()
    pagination_class   = NoPagination

    @action(detail=False, methods=['get'], url_path='versions')
    def versions(self, request):
        """
        Prises de vue disponibles pour une classe, de la plus récente à la plus
        ancienne.

        Une semaine régénérée en compte plusieurs : c'est précisément ce qu'on
        veut pouvoir comparer.
        """
        classe_id = request.query_params.get('classe')
        if not classe_id:
            raise ValidationError({'classe': 'Classe requise.'})

        qs = (
            EmploiArchive.objects
            .filter(classe_id=classe_id)
            .values('semaine_id', 'version', 'genere_le',
                    'semaine__numero', 'semaine__date_debut',
                    'semaine__date_fin', 'semaine__semestre__code')
            .annotate(nb=Count('id'))
            .order_by('-semaine__numero', '-version')
        )
        semestre = request.query_params.get('semestre')
        if semestre:
            qs = qs.filter(semaine__semestre_id=semestre)

        return Response(VersionArchiveSerializer([
            {
                'semaine':    l['semaine_id'],
                'numero':     l['semaine__numero'],
                'date_debut': l['semaine__date_debut'],
                'date_fin':   l['semaine__date_fin'],
                'semestre':   l['semaine__semestre__code'] or '',
                'version':    l['version'],
                'genere_le':  l['genere_le'],
                'nb_seances': l['nb'],
            }
            for l in qs
        ], many=True).data)

    @action(detail=False, methods=['get'], url_path='grille')
    def grille(self, request):
        """Séances d'une prise de vue, au format de l'emploi du temps vivant."""
        seances = self._prise_de_vue(request)
        return Response(self.get_serializer(seances, many=True).data)

    @action(detail=False, methods=['get'], url_path='pdf')
    def pdf(self, request):
        """La même grille, au même gabarit que l'emploi du temps courant."""
        seances = list(self._prise_de_vue(request))
        if not seances:
            raise ValidationError('Aucune archive pour cette semaine.')

        axe      = self._axe(request)
        premiere = seances[0]
        semaine  = SemaineIPGEI.objects.select_related('semestre').filter(
            pk=request.query_params.get('semaine')).first()
        creneaux, lignes = edt_pdf.construire_grille(seances, axe=axe)

        libelle, cible = {
            'prof':  ('Enseignant', premiere.prof_nom),
            'salle': ('Salle',      premiere.salle_nom),
        }.get(axe, ('Classe', premiere.classe_nom))

        return edt_pdf.reponse_pdf(
            'ipgei/emploi_pdf.html',
            {
                'annee_universitaire': (semaine.semestre.annee_universitaire
                                        if semaine and semaine.semestre_id else ''),
                'cible_libelle':       libelle,
                'cible':               cible,
                'creneaux':            creneaux,
                'rows':                lignes,
                'semaine':      semaine.numero if semaine else '',
                'debut':        edt_pdf._date_fr(semaine.date_debut) if semaine else '',
                'fin':          edt_pdf._date_fr(semaine.date_fin) if semaine else '',
                'semestre_nom': (f'Semestre {semaine.semestre.code}'
                                 if semaine and semaine.semestre_id else ''),
                # Un document d'archive doit dire qu'il en est un, et de quand.
                # Sans cette mention il se confondrait avec l'emploi du temps
                # courant, alors qu'ils diffèrent souvent — c'est la raison même
                # de sa conservation.
                'etat_semaine': (
                    f'Publié le {edt_pdf._date_fr(premiere.genere_le.date())}'
                    + (f' · version {premiere.version}'
                       if axe == 'classe' and premiere.version > 1 else '')
                ),
                'est_divergent': False,
            },
            f'Emploi_{_nom_fichier(cible)}'
            f'_S{semaine.numero if semaine else 0}.pdf',
        )

    def _prise_de_vue(self, request):
        """
        Séances publiées d'une semaine, selon l'axe demandé.

        `classe` donne la grille d'une classe — c'est là que la notion de
        version a un sens, puisque c'est classe par classe qu'on régénère.
        `prof` et `salle` traversent les classes : chacune y apporte sa
        dernière version, une classe régénérée ne devant pas ressortir deux
        fois dans l'emploi du temps d'un enseignant.
        """
        semaine_id = request.query_params.get('semaine')
        if not semaine_id:
            raise ValidationError('Paramètre requis : semaine.')

        classe_id = request.query_params.get('classe')
        prof_id   = request.query_params.get('prof')
        salle_id  = request.query_params.get('salle')
        if not (classe_id or prof_id or salle_id):
            raise ValidationError('Paramètre requis : classe, prof ou salle.')

        qs = EmploiArchive.objects.filter(semaine_id=semaine_id)
        if classe_id:
            qs = qs.filter(classe_id=classe_id)
            version = request.query_params.get('version')
            if version:
                return qs.filter(version=version).order_by('jour_ref', 'creneau_ordre')
        if prof_id:
            qs = qs.filter(prof_ref=prof_id)
        if salle_id:
            qs = qs.filter(salle_ref=salle_id)

        # Dernière version DE CHAQUE CLASSE. Un `Max` global retiendrait la
        # version 2 d'une classe régénérée et effacerait les classes restées
        # en version 1.
        dernieres = {
            l['classe_id']: l['v']
            for l in (EmploiArchive.objects
                      .filter(semaine_id=semaine_id)
                      .values('classe_id')
                      .annotate(v=Max('version')))
        }
        retenues = [
            a.pk for a in qs.only('id', 'classe_id', 'version')
            if a.version == dernieres.get(a.classe_id)
        ]
        return (EmploiArchive.objects.filter(pk__in=retenues)
                .order_by('jour_ref', 'creneau_ordre'))

    def _axe(self, request):
        """Axe de lecture, déduit du filtre demandé."""
        if request.query_params.get('prof'):
            return 'prof'
        if request.query_params.get('salle'):
            return 'salle'
        return 'classe'


class AbsenceSeanceViewSet(ModelViewSet):
    """Absences saisies (consultation, correction unitaire)."""
    serializer_class   = AbsenceSeanceSerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_absences'
    queryset           = AbsenceSeance.objects.select_related(
        'inscription__etudiant', 'seance__matiere', 'seance__creneau', 'seance__semaine',
    )
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields   = ['inscription', 'seance', 'statut', 'inscription__classe',
                          'seance__semaine__semestre']
    search_fields      = ['inscription__etudiant__nom', 'inscription__etudiant__matricule']
    ordering           = ['-seance__date']
    pagination_class   = StandardPagination

    def perform_create(self, serializer):
        serializer.save(saisie_par=_utilisateur(self.request))


# ═════════════════════════════════════════════════════════════════════════════
# Permutations
# ═════════════════════════════════════════════════════════════════════════════
class _WorkflowPermutationMixin(_ErreurMetierMixin):
    """
    Circuit commun aux deux permutations : demande → accord → validation
    directeur, avec raccourci « action directe » du directeur.
    """
    service_application = None

    def perform_create(self, serializer):
        utilisateur = _utilisateur(self.request)
        directe = bool(self.request.data.get('action_directe'))
        extra = {'demande_par': utilisateur}
        if directe:
            # Le directeur tranche sans passer par l'accord de la contrepartie :
            # la demande naît déjà validée, prête à être appliquée.
            from django.utils import timezone
            extra.update({
                'action_directe':  True,
                'statut':          StatutWorkflow.VALIDEE,
                'valide_par':      utilisateur,
                'date_validation': timezone.now(),
            })
        serializer.save(**extra)

    def _transition(self, instance, *, depuis, vers, champ_acteur, champ_date, request):
        from django.utils import timezone
        if instance.statut not in depuis:
            raise ValidationError(
                f'Transition impossible depuis le statut « {instance.get_statut_display()} ».'
            )
        instance.statut = vers
        setattr(instance, champ_acteur, _utilisateur(request))
        setattr(instance, champ_date, timezone.now())
        instance.save(update_fields=['statut', champ_acteur, champ_date])
        return instance

    @action(detail=True, methods=['post'], url_path='accorder')
    def accorder(self, request, pk=None):
        """Accord de la contrepartie (prof B, ou classe d'accueil)."""
        instance = self.get_object()
        self._transition(
            instance, depuis=[StatutWorkflow.DEMANDEE], vers=StatutWorkflow.ACCORDEE,
            champ_acteur='accord_par', champ_date='date_accord', request=request,
        )
        return Response(self.get_serializer(instance).data)

    @action(detail=True, methods=['post'], url_path='valider')
    def valider(self, request, pk=None):
        """Validation du directeur — dernière étape avant application."""
        instance = self.get_object()
        self._transition(
            instance, depuis=[StatutWorkflow.DEMANDEE, StatutWorkflow.ACCORDEE],
            vers=StatutWorkflow.VALIDEE,
            champ_acteur='valide_par', champ_date='date_validation', request=request,
        )
        return Response(self.get_serializer(instance).data)

    @action(detail=True, methods=['post'], url_path='refuser')
    def refuser(self, request, pk=None):
        instance = self.get_object()
        if instance.statut == StatutWorkflow.APPLIQUEE:
            raise ValidationError('Permutation déjà appliquée — refus impossible.')
        instance.statut      = StatutWorkflow.REFUSEE
        instance.motif_refus = request.data.get('motif_refus', '')
        instance.save(update_fields=['statut', 'motif_refus'])
        return Response(self.get_serializer(instance).data)

    @action(detail=True, methods=['post'], url_path='appliquer')
    def appliquer(self, request, pk=None):
        instance = self.get_object()
        resultat = self._executer(self.service_application, instance, _utilisateur(request))
        instance.refresh_from_db()
        data = self.get_serializer(instance).data
        if isinstance(resultat, int):
            data['seances_impactees'] = resultat
        return Response(data)


class PermutationProfViewSet(_WorkflowPermutationMixin, ModelViewSet):
    """
    Permutation d'enseignants : créneau conservé, échange prof + salle + matière,
    portée 1 ou N semaines, charge suivant le prof effectif.
    """
    serializer_class    = PermutationProfSerializer
    permission_classes  = [RBACPermission]
    required_module     = 'ipgei_permutations'
    service_application = staticmethod(services.appliquer_permutation_prof)
    queryset            = PermutationProf.objects.select_related(
        'seance_a__classe', 'seance_a__matiere', 'seance_a__prof', 'seance_a__creneau',
        'seance_a__jour', 'seance_a__semaine', 'seance_a__salle',
        'seance_b__classe', 'seance_b__matiere', 'seance_b__prof', 'seance_b__creneau',
        'seance_b__jour', 'seance_b__semaine', 'seance_b__salle',
        'demande_par', 'valide_par',
    )
    filter_backends     = [DjangoFilterBackend, OrderingFilter]
    filterset_fields    = ['statut', 'seance_a__classe', 'seance_a__prof', 'seance_b__prof']
    ordering            = ['-date_demande']
    pagination_class    = StandardPagination


class PermutationEtudiantViewSet(_WorkflowPermutationMixin, ModelViewSet):
    """
    Changement de classe d'un étudiant : re-rattachement de l'inscription,
    historique conservé, notes et absences intactes.
    """
    serializer_class    = PermutationEtudiantSerializer
    permission_classes  = [RBACPermission]
    required_module     = 'ipgei_permutations'
    service_application = staticmethod(services.appliquer_permutation_etudiant)
    queryset            = PermutationEtudiant.objects.select_related(
        'inscription__etudiant', 'classe_source', 'classe_cible',
        'sous_groupe_cible', 'demande_par', 'valide_par',
    )
    filter_backends     = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields    = ['statut', 'classe_source', 'classe_cible']
    search_fields       = ['inscription__etudiant__nom', 'inscription__etudiant__matricule']
    ordering            = ['-date_demande']
    pagination_class    = StandardPagination

    def perform_create(self, serializer):
        # La classe de départ est déduite de l'inscription : la faire saisir
        # ouvrirait la porte à une incohérence entre demande et réalité.
        inscription = serializer.validated_data['inscription']
        serializer.validated_data['classe_source'] = inscription.classe
        super().perform_create(serializer)


class HistoriqueClasseViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    """Journal des changements de classe (lecture seule)."""
    serializer_class   = HistoriqueClasseSerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_permutations'
    queryset           = HistoriqueClasse.objects.select_related(
        'inscription__etudiant', 'classe_avant', 'classe_apres', 'decide_par',
    )
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['inscription', 'classe_avant', 'classe_apres']
    ordering           = ['-date_effet']
    pagination_class   = StandardPagination


# ═════════════════════════════════════════════════════════════════════════════
# Documents officiels
# ═════════════════════════════════════════════════════════════════════════════
class DocumentIPGEIViewSet(_ErreurMetierMixin, ListModelMixin, RetrieveModelMixin, GenericViewSet):
    """
    Émission des documents officiels IPGEI (plan §5).

    Chaque génération produit un `DocumentOfficiel` numéroté, scellé par un
    SHA-256 et vérifiable publiquement via `/verifier/{token}` — le même
    dispositif que les documents du socle, sans code de vérification dédié.
    """
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_documents'
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields   = ['type_document', 'annee_universitaire', 'etudiant', 'est_valide']
    search_fields      = ['numero_serie', 'etudiant__nom', 'etudiant__matricule']
    ordering           = ['-date_generation']
    pagination_class   = StandardPagination

    def get_queryset(self):
        from apps.documents.models import DocumentOfficiel
        from .documents import TYPES_IPGEI
        return (
            DocumentOfficiel.objects
            .filter(type_document__in=TYPES_IPGEI)
            .select_related('etudiant', 'institution', 'genere_par')
        )

    def get_serializer_class(self):
        from .serializers_documents import DocumentIPGEISerializer
        return DocumentIPGEISerializer

    # ── Helpers ─────────────────────────────────────────────────────────────
    def _base_url(self):
        from django.conf import settings
        return getattr(settings, 'DOCUMENTS_BASE_URL', '').rstrip('/')

    def _inscription(self, request):
        identifiant = request.data.get('inscription')
        if not identifiant:
            raise ValidationError({'inscription': 'Inscription requise.'})
        inscription = (
            InscriptionIPGEI.objects
            .select_related('etudiant', 'classe__institution')
            .filter(pk=identifiant).first()
        )
        if inscription is None:
            raise ValidationError({'inscription': 'Inscription introuvable.'})
        return inscription

    def _deliberation(self, request):
        identifiant = request.data.get('deliberation')
        if not identifiant:
            raise ValidationError({'deliberation': 'Délibération requise.'})
        deliberation = Deliberation.objects.filter(pk=identifiant).first()
        if deliberation is None:
            raise ValidationError({'deliberation': 'Délibération introuvable.'})
        return deliberation

    def _reponse_pdf(self, doc, pdf: bytes):
        from django.http import HttpResponse
        reponse = HttpResponse(pdf, content_type='application/pdf')
        reponse['Content-Disposition'] = f'attachment; filename="{doc.numero_serie}.pdf"'
        # Un document officiel ne doit jamais etre servi depuis un cache
        # intermediaire : chaque tirage porte son propre numero.
        reponse['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        reponse['X-Numero-Serie'] = doc.numero_serie
        return reponse

    # ── Émission ────────────────────────────────────────────────────────────
    @action(detail=False, methods=['post'], url_path='releve-semestre')
    def releve_semestre(self, request):
        from . import documents as docs_ipgei
        inscription = self._inscription(request)
        semestre_id = request.data.get('semestre')
        semestre = SemestreIPGEI.objects.filter(pk=semestre_id).first() if semestre_id else None
        if semestre is None:
            raise ValidationError({'semestre': 'Semestre requis et valide.'})
        doc, pdf = self._executer(
            docs_ipgei.generer_releve_semestre, inscription, semestre,
            utilisateur=_utilisateur(request), base_url=self._base_url(),
        )
        return self._reponse_pdf(doc, pdf)

    @action(detail=False, methods=['post'], url_path='releve-annuel')
    def releve_annuel(self, request):
        from . import documents as docs_ipgei
        inscription = self._inscription(request)
        doc, pdf = self._executer(
            docs_ipgei.generer_releve_annuel, inscription,
            utilisateur=_utilisateur(request), base_url=self._base_url(),
        )
        return self._reponse_pdf(doc, pdf)

    @action(detail=False, methods=['post'], url_path='decision-deliberation')
    def decision_deliberation(self, request):
        from . import documents as docs_ipgei
        doc, pdf = self._executer(
            docs_ipgei.generer_decision,
            self._deliberation(request), self._inscription(request),
            utilisateur=_utilisateur(request), base_url=self._base_url(),
        )
        return self._reponse_pdf(doc, pdf)

    @action(detail=False, methods=['post'], url_path='attestation-cnim')
    def attestation_cnim(self, request):
        from . import documents as docs_ipgei
        doc, pdf = self._executer(
            docs_ipgei.generer_attestation_cnim,
            self._deliberation(request), self._inscription(request),
            utilisateur=_utilisateur(request), base_url=self._base_url(),
        )
        return self._reponse_pdf(doc, pdf)

    @action(detail=False, methods=['post'], url_path='decisions-classe')
    def decisions_classe(self, request):
        """
        Émet en une passe les décisions de toute une classe.
        Renvoie le bilan plutôt qu'un PDF : les fichiers sont téléchargeables
        ensuite depuis la liste des documents.
        """
        from . import documents as docs_ipgei
        deliberation = self._deliberation(request)
        classe_id = request.data.get('classe')
        lignes = deliberation.lignes.select_related('inscription__classe__institution',
                                                    'inscription__etudiant')
        if classe_id:
            lignes = lignes.filter(inscription__classe_id=classe_id)

        emis, erreurs = [], []
        for ligne in lignes:
            try:
                doc, _ = docs_ipgei.generer_decision(
                    deliberation, ligne.inscription,
                    utilisateur=_utilisateur(request), base_url=self._base_url(),
                )
                emis.append(doc.numero_serie)
            except Exception as exc:            # noqa: BLE001 — on veut le bilan complet
                erreurs.append({
                    'etudiant': ligne.inscription.etudiant.matricule,
                    'erreur':   str(exc),
                })
        return Response({'emis': len(emis), 'numeros': emis, 'erreurs': erreurs})

    @action(detail=True, methods=['get'], url_path='telecharger')
    def telecharger(self, request, pk=None):
        """Re-télécharge le PDF déjà produit, sans réémettre de numéro."""
        from django.http import FileResponse
        doc = self.get_object()
        if not doc.fichier_pdf:
            raise ValidationError('Aucun PDF stocké pour ce document.')
        reponse = FileResponse(
            doc.fichier_pdf.open('rb'), content_type='application/pdf',
            as_attachment=True, filename=f'{doc.numero_serie}.pdf',
        )
        reponse['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return reponse


# ═════════════════════════════════════════════════════════════════════════════
# Tableau de bord
# ═════════════════════════════════════════════════════════════════════════════
class TableauBordIPGEIViewSet(GenericViewSet):
    """Compteurs d'accueil du module IPGEI."""
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_classes'
    queryset           = Classe.objects.none()

    @action(detail=False, methods=['get'], url_path='resume')
    def resume(self, request):
        annee = request.query_params.get('annee')
        classes = Classe.objects.filter(actif=True)
        inscriptions = InscriptionIPGEI.objects.filter(actif=True)
        if annee:
            classes = classes.filter(annee_universitaire=annee)
            inscriptions = inscriptions.filter(annee_universitaire=annee)

        return Response({
            'annee_universitaire': annee or '',
            'classes': {
                'total': classes.count(),
                'mpsi':  classes.filter(niveau='MPSI').count(),
                'mp':    classes.filter(niveau='MP').count(),
            },
            'effectifs': {
                'total': inscriptions.count(),
                'mpsi':  inscriptions.filter(classe__niveau__niveau='MPSI').count(),
                'mp':    inscriptions.filter(classe__niveau__niveau='MP').count(),
            },
            'matieres':      Matiere.objects.filter(actif=True).count(),
            'semestres':     SemestreIPGEI.objects.filter(**({'annee_universitaire': annee} if annee else {})).count(),
            'deliberations': Deliberation.objects.filter(**({'annee_universitaire': annee} if annee else {})).count(),
            'permutations_en_attente': (
                PermutationProf.objects.filter(
                    statut__in=[StatutWorkflow.DEMANDEE, StatutWorkflow.ACCORDEE],
                ).count()
                + PermutationEtudiant.objects.filter(
                    statut__in=[StatutWorkflow.DEMANDEE, StatutWorkflow.ACCORDEE],
                ).count()
            ),
        })

    @action(detail=False, methods=['get'], url_path='annees', pagination_class=NoPagination)
    def annees(self, request):
        """Années universitaires ayant au moins une classe — sélecteur global."""
        annees = list(
            Classe.objects.values_list('annee_universitaire', flat=True)
            .distinct().order_by('-annee_universitaire')
        )
        return Response(annees)
