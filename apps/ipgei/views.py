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
from django.utils import timezone as _tz
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.viewsets import (GenericViewSet, ModelViewSet,
                                     ReadOnlyModelViewSet)
from rest_framework.mixins import (CreateModelMixin, DestroyModelMixin,
                                   ListModelMixin, RetrieveModelMixin,
                                   UpdateModelMixin)

from apps.prof.models import Prof
from apps.salle.models import Salle
from core.mixins import InstitutionScopedMixin, SelectAllMixin
from core.pagination import NoPagination, StandardPagination
from rest_framework.permissions import BasePermission

from core.permissions import RBACPermission

from . import detail_notes, edt_pdf, pv, services
from .filters import ClasseFilter, InscriptionIPGEIFilter
from .models import (
    AbsenceSeance, AnonymatIPGEI, Classe, Deliberation, EmploiArchive, Evaluation, GrilleType,
    HistoriqueClasse,
    InscriptionIPGEI, LigneDeliberation, Matiere, MembreJuryIPGEI, NiveauIPGEI,
    Note,
    ParametresIPGEI,
    PermutationEtudiant, PermutationProf, SeanceReelle, SeanceType, SemaineIPGEI,
    SemestreIPGEI, SessionEvaluationIPGEI, SignataireIPGEI, SousGroupeTP,
    StatutWorkflow,
)
from .serializers import (
    AbsenceSeanceSerializer, ClasseSelectSerializer, ClasseSerializer,
    DeliberationSerializer, DupliquerGrilleSerializer, EditionLotSerializer,
    EmploiArchiveSerializer, EvaluationSerializer, GrilleTypeListSerializer, GrilleTypeSerializer,
    HistoriqueClasseSerializer, InscriptionCompleteSerializer,
    InscriptionIPGEISerializer,
    LigneDeliberationListSerializer, LigneDeliberationSerializer,
    MatiereSelectSerializer, MatiereSerializer, MembreJuryIPGEISerializer,
    NiveauIPGEISerializer,
    NoteSerializer,
    ParametresIPGEISerializer, PermutationEtudiantSerializer,
    PermutationProfSerializer, SaisieAbsencesSerializer,
    SaisieAnonymeLotSerializer,
    SaisieAnonymeSerializer,
    SaisieCollectiveSerializer, SeanceReelleSerializer, SeanceTypeSerializer,
    SemaineIPGEISerializer, SemestreIPGEISerializer,
    SessionEvaluationIPGEISerializer, SignataireIPGEISerializer, SousGroupeTPSerializer,
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


def _annee_session(request) -> str:
    """Année universitaire du contexte de l'utilisateur, ou chaîne vide."""
    from apps.authentication.models import UserContexte

    utilisateur = _utilisateur(request)
    if utilisateur is None:
        return ''
    contexte = UserContexte.objects.filter(user=utilisateur).first()
    return contexte.annee_universitaire if contexte else ''


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

    @action(detail=False, methods=['post'], url_path='creer-annee')
    def creer_annee(self, request):
        """
        Ouvre une année d'un coup : ses quatre semestres, datés par convention.

        Les créer un par un laissait des années à moitié ouvertes, et un inscrit
        n'y recevait que la moitié de ses matières. Les semestres déjà présents
        sont laissés tels quels — dates comprises.
        """
        annee = request.data.get('annee_universitaire')
        if not annee:
            raise ValidationError({'annee_universitaire': 'Année requise.'})

        crees = self._executer(services.creer_semestres_annee,
                               annee, _institution_principale())
        return Response({
            'annee_universitaire': annee,
            'semestres_crees':     len(crees),
            'codes':               [s.code for s in crees],
        }, status=status.HTTP_201_CREATED if crees else status.HTTP_200_OK)

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

    @action(detail=False, methods=['get'], url_path='contexte-frais',
            pagination_class=NoPagination)
    def contexte_frais(self, request):
        """
        Ce qu'il faut savoir pour tarifer une inscription IPGEI.

        La grille du socle est indexée par type de diplôme, notion qui n'existe
        pas en prépa : les classes en portent une valeur unique, héritée de leur
        filière. L'écran ne doit donc pas la faire choisir — sinon un tarif
        enregistré sous un autre type resterait introuvable, et les inscriptions
        continueraient à se créer à zéro sans que rien ne l'explique.

        C'est le serveur qui la donne, plutôt qu'un choix figé dans le front :
        elle suit la filière et changerait avec elle.
        """
        classe = Classe.objects.select_related('filiere').filter(
            filiere__isnull=False).first()
        return Response({
            'type_diplome': getattr(getattr(classe, 'filiere', None), 'type_diplome', ''),
            'niveaux': [
                {'niveau': 1, 'libelle': '1re année — MPSI'},
                {'niveau': 2, 'libelle': '2e année — MP'},
            ],
        })

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

    @action(detail=False, methods=['post'], url_path='repartir')
    def repartir(self, request):
        """
        Déplace plusieurs inscrits d'une classe vers une autre, en une fois.

        À la rentrée, on ne traite pas des demandes individuelles : on constitue
        des groupes. Le circuit de la permutation — formulaire signé, accord des
        deux élèves — vaut pour un changement demandé en cours d'année, pas pour
        une répartition d'arrivants.

        Corps : `{mouvements: [{inscription, classe}, …], motif}`. Tout passe ou
        rien : une répartition à moitié appliquée laisserait deux classes
        fausses.
        """
        mouvements = request.data.get('mouvements') or []
        return Response(self._executer(
            services.repartir_inscriptions, mouvements,
            utilisateur=_utilisateur(request),
            motif=request.data.get('motif') or 'Répartition entre classes',
        ))

    @action(detail=False, methods=['post'], url_path='recalculer-frais')
    def recalculer_frais(self, request):
        """
        Repose le tarif de la grille sur les inscriptions d'une année.

        Le montant est figé à la création — c'est ce qui garde un reçu exact
        quand la grille change en cours d'année. Mais renseignée APRÈS coup,
        elle laissait les dossiers à zéro sans que rien ne les rattrape : le
        cas d'un passage en année supérieure ouvert avant que la scolarité
        n'ait fixé les tarifs.

        Les inscriptions déjà payées sont laissées telles quelles.
        """
        annee = request.data.get('annee_universitaire')
        if not annee:
            raise ValidationError({'annee_universitaire': 'Année requise.'})
        return Response(self._executer(
            services.recalculer_frais, annee,
            institution=_institution_principale(),
            classe_id=request.data.get('classe'),
        ))
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

    @action(detail=True, methods=['post'], url_path='affecter')
    def affecter(self, request, pk=None):
        """
        Sort un inscrit de la classe d'attente pour le poser dans une classe.

        Passe par le même chemin qu'un changement de classe : le mouvement est
        journalisé, et le département de l'étudiant suit — sans quoi il
        resterait absent des listes d'appel.
        """
        inscription = self.get_object()
        classe = Classe.objects.filter(pk=request.data.get('classe')).first()
        if classe is None:
            raise ValidationError({'classe': 'Classe introuvable.'})

        self._executer(services.affecter_a_une_classe, inscription, classe,
                       _utilisateur(request))
        inscription.refresh_from_db()
        return Response(self.get_serializer(inscription).data)

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

        if donnees.get('classe'):
            classe = Classe.objects.filter(pk=donnees['classe']).first()
            if classe is None:
                raise ValidationError({'classe': 'Classe introuvable.'})
        else:
            # Inscription en attente : le niveau suffit. L'étudiant reçoit dès
            # maintenant sa maquette et ses frais — ce qui manque est
            # seulement le groupe où il suivra les cours.
            institution = _institution_principale()
            annee = donnees.get('annee_universitaire') or _annee_session(request)
            if not annee:
                raise ValidationError(
                    {'annee_universitaire': "Précisez l'année universitaire."})
            classe = services.classe_attente(donnees['niveau'], annee, institution)

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

            # Redoubler se contrôle AVANT d'écrire : une fois l'inscription
            # créée, ses notes et ses absences existent, et la défaire coûte
            # plus que de la refuser.
            statut, nb_redoublements = services.controler_reinscription(etudiant, classe)

            inscription = InscriptionIPGEI.objects.create(
                etudiant=etudiant,
                classe=classe,
                sous_groupe_id=sous_groupe_id or None,
                numero_ordre=donnees.get('numero_ordre'),
                annee_universitaire=classe.annee_universitaire,
                statut=statut,
                nb_redoublements=nb_redoublements,
                montant_frais=services.frais_pour(classe),
            )

            # Inscription pédagogique, dans la même transaction : un étudiant
            # inscrit sans ses matières n'apparaîtrait ni dans les grilles de
            # notes ni dans les délibérations, et rien ne le signalerait.
            matieres = services.inscrire_aux_matieres(inscription)

        donnees_reponse = InscriptionIPGEISerializer(inscription).data
        donnees_reponse['matieres_inscrites'] = matieres
        return Response(donnees_reponse, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='depuis-preinscription')
    def depuis_preinscription(self, request):
        """
        Transforme une pré-inscription acceptée en inscription IPGEI.

        La conversion du socle produit une inscription administrative LMD :
        sans classe de prépa, sans frais IPGEI et sans maquette. Le dossier
        paraîtrait traité alors que l'étudiant n'existerait dans aucune grille
        de notes.

        Celle-ci emprunte le chemin de l'inscription unitaire — contrôle du
        redoublement, tarif figé, matières rattachées — et reprend l'identité
        du dossier plutôt que de la faire ressaisir : le candidat l'a déjà
        renseignée, la retaper n'ajoute que des fautes.
        """
        from apps.absence.models import Etudiant
        from apps.inscriptions.models import Preinscription

        dossier = Preinscription.objects.filter(
            pk=request.data.get('preinscription')).first()
        if dossier is None:
            raise ValidationError({'preinscription': 'Dossier introuvable.'})
        if dossier.statut != 'acceptee':
            raise ValidationError(
                {'preinscription': 'Seul un dossier accepté peut être converti '
                                   f'(statut actuel : {dossier.get_statut_display()}).'}
            )
        classe = Classe.objects.filter(pk=request.data.get('classe')).first()
        if classe is None:
            raise ValidationError({'classe': 'Classe requise et valide.'})

        # Le matricule vient du bac : c'est l'identifiant que le candidat porte
        # déjà, et celui sous lequel le référentiel officiel le connaît.
        matricule = (request.data.get('matricule')
                     or dossier.cni or str(dossier.numero_dossier)[:20])

        with transaction.atomic():
            etudiant = Etudiant.objects.filter(matricule=matricule).first()
            if etudiant is None:
                nom_complet = f'{dossier.nom_fr} {dossier.prenom_fr}'.strip()
                etudiant = Etudiant.objects.create(
                    matricule=matricule, nom=nom_complet, nom_fr=nom_complet,
                    nom_ar=dossier.nom_ar or '', prenom_ar=dossier.prenom_ar or '',
                    prenom_fr=dossier.prenom_fr or '',
                    genre=dossier.genre or 'M',
                    date_naissance=dossier.date_naissance,
                    lieu_naissance_fr=dossier.lieu_naissance or '',
                    cni=dossier.cni or None, telephone=dossier.telephone or '',
                    email=dossier.email or '',
                    serie_bac=dossier.serie_bac or '',
                    moyenne_bac=dossier.bac_moyenne,
                    statut='actif',
                )

            if InscriptionIPGEI.objects.filter(
                etudiant=etudiant, annee_universitaire=classe.annee_universitaire,
            ).exists():
                raise ValidationError(
                    f'{etudiant} est déjà inscrit pour {classe.annee_universitaire}.')

            statut, nb = services.controler_reinscription(etudiant, classe)
            inscription = InscriptionIPGEI.objects.create(
                etudiant=etudiant, classe=classe,
                annee_universitaire=classe.annee_universitaire,
                statut=statut, nb_redoublements=nb,
                montant_frais=services.frais_pour(classe),
            )
            matieres = services.inscrire_aux_matieres(inscription)

            # Le dossier cesse d'apparaître comme à traiter. Le statut terminal
            # du socle s'appelle « inscrite » ; chercher « convertie », qui n'est
            # pas dans le référentiel, laissait le dossier « accepté » et son
            # bouton « Inscrire » indéfiniment.
            dossier.statut = 'inscrite'
            dossier.save(update_fields=['statut'])

        donnees = InscriptionIPGEISerializer(inscription).data
        donnees['matieres_inscrites'] = matieres
        return Response(donnees, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='importer-mesrs',
            parser_classes=[MultiPartParser, FormParser])
    def importer_mesrs(self, request):
        """
        Inscription en lot depuis le fichier officiel.

        L'import du socle ne convient pas : il crée des inscriptions
        administratives LMD, sans classe de prépa, sans rattachement aux
        matières et sans frais. Les dossiers produits seraient inexploitables —
        et le resteraient sans que rien ne le signale.

        Celui-ci passe par le même chemin que l'inscription unitaire : contrôle
        du redoublement, tarif figé, maquette rattachée. Une ligne qui échoue
        n'interrompt pas les autres, mais son motif est rendu : un import
        silencieusement partiel est pire qu'un import refusé.

        Colonnes attendues : NNI et NOMFR au minimum ; NUMBAC, SERIE, MOYENNE,
        DATENAIS, LIEUNAIS et SEXE sont reprises si présentes.
        """
        import datetime as _dt

        from apps.absence.models import Etudiant

        fichier = request.FILES.get('fichier')
        if fichier is None:
            raise ValidationError({'fichier': 'Fichier requis.'})
        classe = Classe.objects.filter(pk=request.data.get('classe')).first()
        if classe is None:
            raise ValidationError({'classe': 'Classe requise et valide.'})

        try:
            import openpyxl
            classeur = openpyxl.load_workbook(fichier, read_only=True, data_only=True)
            feuille  = classeur.active
        except Exception as exc:
            raise ValidationError({'fichier': f'Fichier Excel invalide : {exc}'})

        if feuille.max_row and feuille.max_row > 5000:
            raise ValidationError({'fichier': 'Trop de lignes (maximum 5000).'})

        entetes = {}
        premiere = next(feuille.iter_rows(min_row=1, max_row=1, values_only=True), None)
        for indice, cellule in enumerate(premiere or []):
            if cellule is not None:
                entetes[str(cellule).strip().upper()] = indice

        manquantes = [c for c in ('NNI', 'NOMFR') if c not in entetes]
        if manquantes:
            raise ValidationError(
                {'fichier': f"Colonnes manquantes : {', '.join(manquantes)}"})

        def colonne(ligne, nom):
            i = entetes.get(nom)
            if i is None or i >= len(ligne) or ligne[i] is None:
                return ''
            return str(ligne[i]).strip()

        def date_de(valeur):
            if not valeur:
                return None
            for forme in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
                try:
                    return _dt.datetime.strptime(valeur, forme).date()
                except ValueError:
                    continue
            return None

        crees, ignores, erreurs = 0, 0, []
        montant = services.frais_pour(classe)

        for numero, ligne in enumerate(
            feuille.iter_rows(min_row=2, values_only=True), start=2,
        ):
            nni = colonne(ligne, 'NNI')
            nom = colonne(ligne, 'NOMFR')
            if not (nni or nom):
                continue
            matricule = colonne(ligne, 'NUMBAC') or nni
            if not matricule:
                erreurs.append({'ligne': numero, 'motif': 'Ni NNI ni numéro de bac.'})
                continue

            try:
                with transaction.atomic():
                    etudiant = Etudiant.objects.filter(matricule=matricule).first()
                    if etudiant is None:
                        etudiant = Etudiant.objects.create(
                            matricule=matricule, nom=nom, nom_fr=nom, statut='actif',
                            cni=nni or None,
                            date_naissance=date_de(colonne(ligne, 'DATENAIS')),
                            lieu_naissance_fr=colonne(ligne, 'LIEUNAIS'),
                            genre='F' if colonne(ligne, 'SEXE').upper().startswith('F') else 'M',
                            nbac=colonne(ligne, 'NUMBAC') or None,
                            serie_bac=colonne(ligne, 'SERIE'),
                        )

                    if InscriptionIPGEI.objects.filter(
                        etudiant=etudiant,
                        annee_universitaire=classe.annee_universitaire,
                    ).exists():
                        ignores += 1
                        continue

                    statut, nb = services.controler_reinscription(etudiant, classe)
                    inscription = InscriptionIPGEI.objects.create(
                        etudiant=etudiant, classe=classe,
                        annee_universitaire=classe.annee_universitaire,
                        statut=statut, nb_redoublements=nb, montant_frais=montant,
                    )
                    services.inscrire_aux_matieres(inscription)
                    crees += 1
            except Exception as exc:
                motif = getattr(exc, 'messages', None) or [str(exc)]
                erreurs.append({'ligne': numero, 'etudiant': nom, 'motif': motif[0][:200]})

        return Response({
            'inscriptions_creees': crees,
            'deja_inscrits':       ignores,
            'erreurs':             erreurs,
            'montant_applique':    str(montant),
        })

    @action(detail=False, methods=['get'], url_path='maquette', pagination_class=NoPagination)
    def maquette(self, request):
        """
        État de l'inscription pédagogique d'une classe.

        Elle est automatique : il n'y a rien à y saisir. C'est précisément ce
        qui rend cette vue nécessaire — un automatisme dont on ne vérifie
        jamais le résultat finit par manquer quelqu'un sans que personne le
        sache, et cela ne se voit qu'en délibération.

        Pour chaque étudiant : ce que la maquette du niveau prévoit, ce qui lui
        est effectivement rattaché, et ce qui manque.
        """
        classe_id = request.query_params.get('classe')
        if not classe_id:
            raise ValidationError({'classe': 'Classe requise.'})
        classe = Classe.objects.filter(pk=classe_id).first()
        if classe is None:
            raise ValidationError({'classe': 'Classe introuvable.'})

        semestres = list(services.semestres_du_niveau(
            classe.niveau_code, classe.annee_universitaire, classe.institution))
        attendues = {}
        for semestre in semestres:
            for m in Matiere.objects.filter(
                institution=semestre.institution, code_semestre=semestre.code, actif=True,
            ):
                attendues[(semestre.pk, m.pk)] = f'{semestre.code} · {m.code}'

        lignes = []
        for inscription in (InscriptionIPGEI.objects
                            .filter(classe=classe, actif=True)
                            .select_related('etudiant')
                            .order_by('numero_ordre', 'etudiant__nom')):
            rattachees = set(
                inscription.notes.values_list('semestre_id', 'matiere_id'))
            manquantes = [libelle for cle, libelle in attendues.items()
                          if cle not in rattachees]
            lignes.append({
                'inscription':    inscription.pk,
                'etudiant':       str(inscription.etudiant),
                'attendues':      len(attendues),
                'rattachees':     len(attendues) - len(manquantes),
                'manquantes':     sorted(manquantes),
            })

        return Response({
            'classe':    classe.nom,
            'niveau':    classe.niveau_code,
            'semestres': [s.code for s in semestres],
            'attendues': len(attendues),
            'lignes':    lignes,
        })

    @action(detail=False, methods=['post'], url_path='rattacher-matieres')
    def rattacher_matieres(self, request):
        """
        Rattache ce qui manque, pour toute une classe.

        L'inscription pédagogique se fait seule à l'inscription et à l'ouverture
        d'un semestre ; ce bouton n'existe que pour les cas où elle a échoué —
        une matière créée pendant une panne, un import fait à la main.
        Idempotent : sans manque, il ne crée rien.
        """
        classe = Classe.objects.filter(pk=request.data.get('classe')).first()
        if classe is None:
            raise ValidationError({'classe': 'Classe requise et valide.'})

        creees = 0
        for inscription in InscriptionIPGEI.objects.filter(classe=classe, actif=True):
            creees += services.inscrire_aux_matieres(inscription)
        return Response({'notes_creees': creees})

    @action(detail=True, methods=['post'], url_path='payer')
    def payer(self, request, pk=None):
        """
        Enregistre le paiement des frais.

        Le montant reste celui figé à l'inscription : ressaisir une somme
        ouvrirait la porte à un écart silencieux avec la grille tarifaire.
        Seules la date et la référence du versement sont apportées ici.
        """
        inscription = self.get_object()
        if inscription.est_payee:
            raise ValidationError('Ces frais sont déjà enregistrés comme payés.')

        reference = (request.data.get('recu_paiement') or '').strip()
        if not reference:
            raise ValidationError(
                {'recu_paiement': 'La référence du versement est requise.'}
            )
        date_paiement = request.data.get('date_paiement') or _tz.now().date()

        inscription.est_payee     = True
        inscription.recu_paiement = reference
        inscription.date_paiement = date_paiement
        inscription.save(update_fields=['est_payee', 'recu_paiement', 'date_paiement'])
        return Response(InscriptionIPGEISerializer(inscription).data)

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
    filterset_fields   = ['code_semestre', 'has_tp', 'actif', 'niveau_ref']
    search_fields      = ['code', 'intitule']
    ordering_fields    = ['code_semestre', 'ordre', 'code', 'coefficient']
    ordering           = ['code_semestre', 'ordre', 'code']
    pagination_class   = StandardPagination

    @action(detail=False, methods=['get'], url_path='contexte-frais',
            pagination_class=NoPagination)
    def contexte_frais(self, request):
        """
        Ce qu'il faut savoir pour tarifer une inscription IPGEI.

        La grille du socle est indexée par type de diplôme, notion qui n'existe
        pas en prépa : les classes en portent une valeur unique, héritée de leur
        filière. L'écran ne doit donc pas la faire choisir — sinon un tarif
        enregistré sous un autre type resterait introuvable, et les inscriptions
        continueraient à se créer à zéro sans que rien ne l'explique.

        C'est le serveur qui la donne, plutôt qu'un choix figé dans le front :
        elle suit la filière et changerait avec elle.
        """
        classe = Classe.objects.select_related('filiere').filter(
            filiere__isnull=False).first()
        return Response({
            'type_diplome': getattr(getattr(classe, 'filiere', None), 'type_diplome', ''),
            'niveaux': [
                {'niveau': 1, 'libelle': '1re année — MPSI'},
                {'niveau': 2, 'libelle': '2e année — MP'},
            ],
        })

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
class NiveauIPGEIViewSet(_ErreurMetierMixin, InstitutionScopedMixin, ModelViewSet):
    """
    Référentiel des niveaux du cursus — MPSI, MP, et ceux qu'on ajoute.

    Rattaché au module `ipgei_parametres`, réservé à l'admin comme le reste du
    paramétrage : ce que ces lignes décrivent — droit au redoublement, décisions
    du jury, rang commandant le tarif — engage tout le cursus.
    """
    serializer_class   = NiveauIPGEISerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_parametres'
    queryset           = NiveauIPGEI.objects.select_related('institution')
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['rang', 'actif']
    ordering           = ['rang', 'ordre', 'code']
    pagination_class   = NoPagination

    def perform_destroy(self, instance):
        """
        Un niveau qui a servi ne se supprime pas : il se désactive.

        Effacer la ligne rendrait illisibles les inscriptions et délibérations
        qui la citent — elles portent le code du niveau, pas sa clé.
        """
        if instance.matieres.exists():
            raise ValidationError(
                f'{instance.code} porte {instance.matieres.count()} matière(s). '
                'Désactivez-le plutôt que de le supprimer.'
            )
        from apps.departement.models import Departement
        if Departement.objects.filter(
            classe_ipgei__isnull=False, niveau__niveau=instance.code,
        ).exists():
            raise ValidationError(
                f'Des classes existent en {instance.code}. Désactivez le niveau.'
            )
        instance.delete()

    def perform_create(self, serializer):
        """Le référentiel du socle doit connaître le niveau, sinon aucune classe
        ne pourra s'y rattacher."""
        from .socle import niveau_socle
        niveau = serializer.save()
        niveau_socle(niveau.code)


class SignataireIPGEIViewSet(_ErreurMetierMixin, InstitutionScopedMixin, ModelViewSet):
    """
    Qui signe les documents officiels — relevés, attestations.

    Le bloc de signature était figé dans les gabarits, au titre du chef de
    service de la scolarité. Quand il est en congé, un autre signe à la main
    sous une fonction qui n'est pas la sienne. Cette liste permet de désigner le
    signataire au moment de la génération ; la signature, elle, reste manuscrite.
    """
    serializer_class   = SignataireIPGEISerializer
    permission_classes = [RBACPermission]
    queryset           = SignataireIPGEI.objects.select_related('institution')
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['actif', 'par_defaut']
    ordering           = ['ordre', 'nom_fr']
    pagination_class   = NoPagination

    @property
    def required_module(self):
        """
        Lire relève des documents, écrire du paramétrage.

        L'agent qui édite un relevé doit voir la liste pour choisir qui signe —
        sans quoi le sélecteur serait vide pour lui. Décider QUI a le droit de
        signer est un autre acte, réservé au paramétrage.
        """
        return ('ipgei_documents' if self.action in ('list', 'retrieve')
                else 'ipgei_parametres')

    def perform_destroy(self, instance):
        """
        Un signataire qui a servi se désactive, il ne s'efface pas.

        Les documents déjà émis portent son nom recopié : les supprimer d'ici
        ne les réécrit pas, mais on garde la trace de qui était habilité.
        """
        instance.actif = False
        instance.par_defaut = False
        instance.save(update_fields=['actif', 'par_defaut'])


class SessionEvaluationIPGEIViewSet(_ErreurMetierMixin, ListModelMixin,
                                    RetrieveModelMixin, UpdateModelMixin, GenericViewSet):
    """
    Fenêtres de saisie des notes : une session normale et une session de
    rattrapage par semestre.

    Ni création ni suppression : les sessions existent avec le semestre. Le
    PATCH ne porte que sur les dates indicatives — les états passent par
    `ouvrir`, `cloturer` et `rouvrir`, qui datent l'acte et le journalisent.
    """
    serializer_class   = SessionEvaluationIPGEISerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_notes'
    queryset           = (SessionEvaluationIPGEI.objects
                          .select_related('semestre', 'cloturee_par'))
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['semestre', 'type_session', 'est_ouverte', 'est_close',
                          'semestre__annee_universitaire']
    ordering           = ['semestre__annee_universitaire', 'semestre__code', 'type_session']
    pagination_class   = NoPagination

    def list(self, request, *args, **kwargs):
        """
        Sessions de l'année demandée, créées au besoin.

        Une session absente de la liste serait indistinguable d'une session
        fermée : on matérialise donc les deux campagnes de chaque semestre
        avant de répondre.
        """
        annee = request.query_params.get('annee_universitaire') or \
            request.query_params.get('semestre__annee_universitaire')
        semestres = SemestreIPGEI.objects.all()
        if annee:
            semestres = semestres.filter(annee_universitaire=annee)
        if request.query_params.get('semestre'):
            semestres = semestres.filter(pk=request.query_params['semestre'])
        for semestre in semestres:
            services.sessions_du_semestre(semestre)
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        qs = super().get_queryset()
        annee = self.request.query_params.get('annee_universitaire')
        if annee:
            qs = qs.filter(semestre__annee_universitaire=annee)
        return qs

    @action(detail=True, methods=['post'], url_path='ouvrir')
    def ouvrir(self, request, pk=None):
        session = self._executer(services.ouvrir_session, self.get_object(),
                                 _utilisateur(request))
        return Response(self.get_serializer(session).data)

    @action(detail=True, methods=['post'], url_path='cloturer')
    def cloturer(self, request, pk=None):
        session = self._executer(services.cloturer_session, self.get_object(),
                                 _utilisateur(request))
        return Response(self.get_serializer(session).data)

    @action(detail=True, methods=['post'], url_path='rouvrir')
    def rouvrir(self, request, pk=None):
        """
        Réouverture d'une campagne close — réservée à un administrateur.

        Rouvrir, c'est rendre modifiables des notes déjà arrêtées : l'acte
        dépasse la saisie courante, il relève de la responsabilité de
        l'établissement.
        """
        utilisateur = _utilisateur(request)
        est_admin = bool(utilisateur and (
            utilisateur.is_superuser or getattr(utilisateur, 'role', '') == 'admin'
        ))
        if not est_admin:
            return Response(
                {'detail': 'Seul un administrateur peut réouvrir une session clôturée.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        session = self._executer(services.rouvrir_session, self.get_object(), utilisateur)
        return Response(self.get_serializer(session).data)

    # ── Anonymat des copies ──────────────────────────────────────────────────
    @action(detail=True, methods=['get', 'post'], url_path='anonymats')
    def anonymats(self, request, pk=None):
        """
        GET  → table de correspondance numéro ↔ étudiant (levée d'anonymat).
        POST → tirage au sort des numéros de la campagne.

        Une seule action pour les deux verbes : deux `@action` partageant un
        même `url_path` se masqueraient dans le routeur, la seconde renvoyant
        405 sur une URL pourtant déclarée.
        """
        if request.method == 'GET':
            return self._table_anonymats(pk)

        utilisateur = _utilisateur(request)
        force = str(request.data.get('force', '')).lower() in ('1', 'true', 'on')
        if force and not (utilisateur and (
            utilisateur.is_superuser or getattr(utilisateur, 'role', '') == 'admin'
        )):
            return Response(
                {'detail': 'Seul un administrateur peut forcer une régénération '
                           'alors que des notes existent.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        total = self._executer(
            services.generer_anonymats, self.get_object(),
            regenerer=str(request.data.get('regenerer', '')).lower() in ('1', 'true', 'on'),
            force=force, utilisateur=utilisateur,
        )
        return Response({'anonymats_generes': total})

    def _table_anonymats(self, pk):
        """
        Table de correspondance numéro ↔ étudiant : la levée d'anonymat.

        Elle reste derrière le module `ipgei_notes` et n'est jamais renvoyée par
        la saisie sous anonymat, qui n'a pas à révéler qui elle note.
        """
        lignes = (AnonymatIPGEI.objects
                  .filter(session=self.get_object())
                  .select_related('inscription__etudiant', 'inscription__classe')
                  .order_by('numero'))
        return Response([
            {
                'numero':             ligne.numero,
                'inscription':        ligne.inscription_id,
                'etudiant_nom':       ligne.inscription.etudiant.nom_display,
                'etudiant_matricule': ligne.inscription.etudiant.matricule,
                'classe':             ligne.inscription.classe.nom,
            }
            for ligne in lignes
        ])

    @action(detail=True, methods=['post'], url_path='plafond')
    def plafond(self, request, pk=None):
        """
        Fixe le plafond de rattrapage de cette session, puis recalcule.

        Le plafond est figé à la création de la session ; cette action permet de
        le poser sur une campagne déjà ouverte — par exemple quand la règle est
        arrêtée après coup. Refusée sur une session close : les notes qui en
        découlent sont arrêtées.
        """
        session = self.get_object()
        if session.type_session != SessionEvaluationIPGEI.TYPE_RATTRAPAGE:
            raise ValidationError('Le plafond ne concerne que la session de rattrapage.')
        if session.est_close:
            raise ValidationError('Session clôturée — réouvrez-la avant de changer le plafond.')

        brut = request.data.get('valeur')
        if brut in (None, ''):
            valeur = None
        else:
            try:
                valeur = Decimal(str(brut))
            except (ArithmeticError, TypeError, ValueError):
                raise ValidationError({'valeur': 'Plafond invalide.'})
            if not Decimal('0') <= valeur <= Decimal('20'):
                raise ValidationError({'valeur': 'Le plafond doit être compris entre 0 et 20.'})

        session.plafond_rattrapage = valeur
        session.save(update_fields=['plafond_rattrapage'])

        recalculees = self._executer(
            services.recalculer_notes,
            Note.objects.filter(semestre=session.semestre),
            plafond_rattrapage=valeur,
        )
        donnees = self.get_serializer(session).data
        donnees['notes_recalculees'] = recalculees
        return Response(donnees)


class EnseignantDeLaMatiereOuRBAC(BasePermission):
    """
    Saisie des notes : le droit sur le module, ou enseigner la matière.

    `ipgei_notes` est attribué à la scolarité et à la direction des études, pas
    au corps enseignant — qui est pourtant celui qui corrige. Plutôt que
    d'ouvrir le module à tous les enseignants, ce qui donnerait à chacun les
    notes de tous, l'accès se règle matière par matière sur la foi de l'emploi
    du temps.
    """

    def has_permission(self, request, view):
        if RBACPermission().has_permission(request, view):
            return True

        utilisateur = request.user
        if not (utilisateur and utilisateur.is_authenticated):
            return False
        prof = services.prof_de(utilisateur)
        if prof is None:
            return False

        # Consulter son propre périmètre ne révèle rien d'autre que lui-même.
        if view.action == 'mes_feuilles':
            return True

        source   = request.query_params if request.method == 'GET' else request.data
        semestre = SemestreIPGEI.objects.filter(pk=source.get('semestre')).first()
        if semestre is None:
            return False

        if view.action in ('saisie_anonyme', 'grille_anonyme', 'saisie_anonyme_lot'):
            # Sous anonymat la classe n'est pas transmise : c'est le principe.
            return services.enseigne_matiere(
                prof, matiere_id=source.get('matiere'), semestre=semestre)

        return services.enseigne(
            prof, classe_id=source.get('classe'), matiere_id=source.get('matiere'),
            semestre=semestre,
        )


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

    def get_permissions(self):
        # La feuille de notes d'une matière appartient d'abord à qui l'enseigne.
        if self.action in ('grille', 'saisie_collective', 'saisie_anonyme',
                           'grille_anonyme', 'saisie_anonyme_lot',
                           'mes_feuilles', 'fiche_collecte'):
            return [EnseignantDeLaMatiereOuRBAC()]
        return super().get_permissions()

    @action(detail=False, methods=['get'], url_path='fiche-collecte')
    def fiche_collecte(self, request):
        """
        Fiche de collecte : la feuille que l'enseignant emporte en salle.

        Liste des étudiants dans l'ORDRE de la grille de saisie, colonnes de
        note et d'observations vides, bloc de signature. C'est le pendant papier
        de la saisie : l'enseignant remplit, signe, la scolarité reporte.

        `matiere` omise, la fiche est éditée pour toutes les matières du
        semestre — un jeu complet pour la classe. Un enseignant, lui, doit la
        nommer : le contrôle d'accès se fait matière par matière, et sans elle
        il obtiendrait les feuilles de ses collègues.

        La CAMPAGNE n'est pas demandée : elle se déduit de l'épreuve. Une fiche
        de rattrapage relève de la seconde session, les autres de la normale —
        faire choisir les deux revenait à faire ressaisir la même chose, et
        laissait produire un DS de rattrapage qui n'existe pas.
        """
        from django.http import HttpResponse

        from . import collecte as collecte_notes

        semestre_id = request.query_params.get('semestre')
        classe_id   = request.query_params.get('classe')
        if not (semestre_id and classe_id):
            raise ValidationError('Paramètres requis : semestre, classe.')

        semestre = SemestreIPGEI.objects.filter(pk=semestre_id).first()
        if semestre is None:
            raise ValidationError({'semestre': 'Semestre introuvable.'})
        classe = Classe.objects.filter(pk=classe_id).first()
        if classe is None:
            raise ValidationError({'classe': 'Classe introuvable.'})

        type_note = (request.query_params.get('type_note') or 'EXAM').upper()
        anonymat  = request.query_params.get('anonymat') in ('1', 'true', 'on')
        # `sortie` et non `format` : DRF réserve `format` à la négociation de
        # contenu et répond 404 pour un suffixe qu'aucun renderer ne connaît.
        excel     = (request.query_params.get('sortie') or 'pdf').lower() == 'excel'

        matiere_id = request.query_params.get('matiere')
        if matiere_id:
            matiere = Matiere.objects.filter(pk=matiere_id).first()
            if matiere is None:
                raise ValidationError({'matiere': 'Matière introuvable.'})
            if matiere.code_semestre != semestre.code:
                raise ValidationError(
                    {'matiere': f'{matiere.code} relève du semestre '
                                f'{matiere.code_semestre}, pas de '
                                f'{semestre.code}.'})
            matieres = [matiere]
        else:
            matieres = collecte_notes.matieres_de(classe, semestre, type_note)
            if not matieres:
                raise ValidationError(
                    'Aucune matière du semestre ne correspond : vérifiez la '
                    'maquette du niveau de cette classe.')

        libelle = _nom_fichier(f'{classe.nom}_{semestre.code}_{type_note}')
        if excel:
            contenu = self._executer(
                collecte_notes.generer_excel, semestre, classe, matieres,
                type_note, anonymat)
            reponse = HttpResponse(
                contenu,
                content_type='application/vnd.openxmlformats-officedocument.'
                             'spreadsheetml.sheet')
            reponse['Content-Disposition'] = \
                f'attachment; filename="collecte_{libelle}.xlsx"'
        else:
            contenu = self._executer(
                collecte_notes.generer_pdf, semestre, classe, matieres,
                type_note, anonymat)
            reponse = HttpResponse(contenu, content_type='application/pdf')
            reponse['Content-Disposition'] = \
                f'attachment; filename="collecte_{libelle}.pdf"'
        return reponse

    @action(detail=False, methods=['get'], url_path='mes-feuilles')
    def mes_feuilles(self, request):
        """
        Feuilles de notes de l'enseignant connecté : ses couples classe × matière.

        Le périmètre vient de l'emploi du temps, seule source tenue à jour de
        qui enseigne quoi. Un compte sans fiche enseignant n'a pas de feuille —
        la scolarité passe par la grille complète.
        """
        prof = services.prof_de(request.user)
        if prof is None:
            return Response([])
        return Response(services.feuilles_enseignant(
            prof, annee_universitaire=request.query_params.get('annee_universitaire', ''),
        ))

    @action(detail=True, methods=['post'], url_path='recalculer')
    def recalculer(self, request, pk=None):
        note = self.get_object()
        plafond = services.plafond_effectif(note.semestre)
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

        # Les deux campagnes s'ouvrent séparément : la grille dit laquelle
        # accepte des notes, pour que l'écran grise les bonnes colonnes plutôt
        # que de laisser saisir puis rejeter à l'enregistrement.
        sessions = services.sessions_du_semestre(semestre)
        return Response({
            'matiere':   MatiereSerializer(matiere).data,
            'semestre':  SemestreIPGEISerializer(semestre).data,
            'nb_ds':     max(1, numeros['ds']),
            'nb_examens': max(1, numeros['exam']),
            'verrouillee': semestre.est_cloture,
            'sessions': SessionEvaluationIPGEISerializer(
                sorted(sessions.values(), key=lambda s: s.type_session), many=True,
            ).data,
            'saisie_normale':    sessions[SessionEvaluationIPGEI.TYPE_NORMALE].est_saisissable,
            'saisie_rattrapage': sessions[SessionEvaluationIPGEI.TYPE_RATTRAPAGE].est_saisissable,
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

        # Une grille peut porter les deux campagnes à la fois. Chacune est
        # contrôlée pour ce qu'elle contient réellement : envoyer des DS pendant
        # la seule campagne de rattrapage doit échouer, et l'inverse aussi.
        lignes = donnees['lignes']
        touche_normale = any(
            'note_tp' in ligne or ligne.get('evaluations') for ligne in lignes
        )
        touche_rattrapage = any('note_rattrapage' in ligne for ligne in lignes)
        if touche_normale:
            self._executer(services.controler_saisie, semestre, rattrapage=False)
        if touche_rattrapage:
            self._executer(services.controler_saisie, semestre, rattrapage=True)
        if not (touche_normale or touche_rattrapage) and semestre.est_cloture:
            raise ValidationError('Semestre clôturé — saisie impossible.')

        utilisateur = _utilisateur(request)
        plafond     = services.plafond_effectif(semestre)
        traitees    = 0

        with transaction.atomic():
            for ligne in lignes:
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

    @action(detail=False, methods=['post'], url_path='saisie-anonyme')
    def saisie_anonyme(self, request):
        """
        Saisit une note sous un numéro d'anonymat, sans révéler l'étudiant.

        Corps : `{semestre, matiere, numero_anonymat, type_evaluation, numero,
        valeur}`. La réponse ne renvoie ni nom ni matricule — l'afficher
        annulerait l'anonymat au moment même où il sert.
        """
        entree = SaisieAnonymeSerializer(data=request.data)
        entree.is_valid(raise_exception=True)
        donnees = entree.validated_data
        self._type_anonyme(donnees)

        matiere  = Matiere.objects.filter(pk=donnees['matiere']).first()
        semestre = SemestreIPGEI.objects.filter(pk=donnees['semestre']).first()
        if matiere is None or semestre is None:
            raise ValidationError('Matière ou semestre introuvable.')
        if matiere.code_semestre != semestre.code:
            raise ValidationError(
                f'La matière {matiere.code} relève du semestre {matiere.code_semestre}.'
            )

        # Les DS et examens relèvent de la campagne normale : l'anonymat ne
        # dispense pas de la fenêtre de saisie.
        session = self._executer(services.controler_saisie, semestre, rattrapage=False)
        inscription = self._executer(services.resoudre_anonymat,
                                     session, donnees['numero_anonymat'])

        with transaction.atomic():
            note, _ = Note.objects.get_or_create(
                inscription=inscription, matiere=matiere, semestre=semestre,
            )
            if note.verrouillee:
                raise ValidationError('Note verrouillée par une délibération validée.')

            Evaluation.objects.update_or_create(
                note=note, type_evaluation=donnees['type_evaluation'],
                numero=donnees['numero'],
                defaults={'valeur': donnees['valeur'], 'saisie_par': _utilisateur(request)},
            )
            self._executer(services.calculer_note, note,
                           plafond_rattrapage=services.plafond_effectif(semestre))

        return Response({
            'numero_anonymat': donnees['numero_anonymat'],
            'matiere':         matiere.code,
            'type_evaluation': donnees['type_evaluation'],
            'numero':          donnees['numero'],
            'valeur':          donnees['valeur'],
        })

    # ── Saisie sous anonymat, par paquet de copies ──────────────────────────
    def _type_anonyme(self, source):
        """
        Épreuve corrigée sous anonymat — l'examen, et lui seul.

        Un devoir surveillé se corrige au fil du semestre, souvent en classe et
        devant les élèves : l'anonymat n'y a pas de prise, et le proposer
        donnerait une garantie que rien ne tient. C'est l'examen, ramassé sur
        copie à en-tête détachable, qui s'anonymise.
        """
        type_evaluation = source.get('type_evaluation') or Evaluation.TYPE_EXAM
        if type_evaluation != Evaluation.TYPE_EXAM:
            raise ValidationError(
                {'type_evaluation': "Seul l'examen se corrige sous anonymat : "
                                    'un devoir surveillé se saisit sur la grille '
                                    'nominative.'})
        return type_evaluation

    def _contexte_anonyme(self, source):
        """
        Matière, semestre et campagne d'une saisie anonyme, contrôlés ensemble.

        La campagne est TOUJOURS la normale : un numéro de rattrapage ne
        désigne pas le même candidat qu'un numéro de session normale.
        """
        matiere  = Matiere.objects.filter(pk=source.get('matiere')).first()
        semestre = SemestreIPGEI.objects.filter(pk=source.get('semestre')).first()
        if matiere is None or semestre is None:
            raise ValidationError('Matière ou semestre introuvable.')
        if matiere.code_semestre != semestre.code:
            raise ValidationError(
                f'La matière {matiere.code} relève du semestre {matiere.code_semestre}.'
            )
        session = services.session_saisie(
            semestre, SessionEvaluationIPGEI.TYPE_NORMALE)
        return matiere, semestre, session

    @action(detail=False, methods=['get'], url_path='grille-anonyme')
    def grille_anonyme(self, request):
        """
        Feuille de correction : un numéro par ligne, la note en regard.

        Ne renvoie NI nom NI matricule — pas même l'identifiant de
        l'inscription : le correcteur qui lit cette grille ne doit pas pouvoir
        remonter à l'étudiant, sans quoi l'anonymat ne protège plus rien. C'est
        aussi pourquoi les lignes sont classées par numéro et non par étudiant.

        Les notes déjà saisies sont renvoyées : on reprend une correction
        interrompue sans risquer de la ressaisir par-dessus.
        """
        matiere, semestre, session = self._contexte_anonyme(request.query_params)
        type_evaluation = self._type_anonyme(request.query_params)
        try:
            numero = int(request.query_params.get('numero') or 1)
        except (TypeError, ValueError):
            raise ValidationError({'numero': "Numéro d'épreuve invalide."})

        anonymats = list(
            AnonymatIPGEI.objects.filter(session=session)
            .values_list('inscription_id', 'numero')
        )
        # Une passe pour les notes, une pour les évaluations : une requête par
        # copie coûterait autant d'allers-retours qu'il y a d'étudiants.
        notes = dict(
            Note.objects
            .filter(matiere=matiere, semestre=semestre,
                    inscription_id__in=[i for i, _ in anonymats])
            .values_list('inscription_id', 'id')
        )
        verrouillees = set(
            Note.objects.filter(pk__in=notes.values(), verrouillee=True)
            .values_list('id', flat=True)
        )
        valeurs = dict(
            Evaluation.objects
            .filter(note_id__in=notes.values(),
                    type_evaluation=type_evaluation, numero=numero)
            .values_list('note_id', 'valeur')
        )

        lignes = []
        for inscription_id, numero_anonymat in anonymats:
            note_id = notes.get(inscription_id)
            valeur = valeurs.get(note_id)
            lignes.append({
                'numero_anonymat': numero_anonymat,
                # Chaine et non nombre, comme partout ailleurs dans l'API :
                # DRF serialise ses DecimalField en chaines
                # (COERCE_DECIMAL_TO_STRING), mais ce dictionnaire est construit
                # a la main et le rendu JSON en aurait fait un flottant. L'ecran
                # traite la note comme une chaine, il tombait sur un nombre.
                'valeur':          None if valeur is None else str(valeur),
                'verrouillee':     note_id in verrouillees,
            })
        lignes.sort(key=lambda ligne: ligne['numero_anonymat'])

        return Response({
            'matiere':         MatiereSerializer(matiere).data,
            'semestre':        SemestreIPGEISerializer(semestre).data,
            'type_evaluation': type_evaluation,
            'numero':          numero,
            'saisissable':     session.est_saisissable,
            'lignes':          lignes,
        })

    @action(detail=False, methods=['post'], url_path='saisie-anonyme-lot')
    def saisie_anonyme_lot(self, request):
        """
        Enregistre toute une feuille de correction en une requête.

        Copie par copie, corriger une classe demandait autant d'allers-retours
        que d'étudiants, et une coupure au milieu laissait la moitié des notes
        entrées sans qu'on sache laquelle. Ici tout passe dans une transaction :
        la feuille est enregistrée entière, ou pas du tout.

        Une valeur nulle EFFACE l'épreuve — c'est ainsi qu'on retire une note
        saisie par erreur, comme sur la grille nominative.
        """
        entree = SaisieAnonymeLotSerializer(data=request.data)
        entree.is_valid(raise_exception=True)
        donnees = entree.validated_data

        matiere, semestre, session = self._contexte_anonyme(donnees)
        self._type_anonyme(donnees)
        self._executer(services.controler_saisie, semestre, rattrapage=False)

        utilisateur = _utilisateur(request)
        traitees = 0
        with transaction.atomic():
            for ligne in donnees['lignes']:
                inscription = self._executer(
                    services.resoudre_anonymat, session, ligne['numero_anonymat'])
                note, _ = Note.objects.get_or_create(
                    inscription=inscription, matiere=matiere, semestre=semestre)
                if note.verrouillee:
                    raise ValidationError(
                        f"Copie n° {ligne['numero_anonymat']} : note verrouillée "
                        'par une délibération validée.')

                if ligne.get('valeur') is None:
                    Evaluation.objects.filter(
                        note=note, type_evaluation=donnees['type_evaluation'],
                        numero=donnees['numero']).delete()
                else:
                    Evaluation.objects.update_or_create(
                        note=note, type_evaluation=donnees['type_evaluation'],
                        numero=donnees['numero'],
                        defaults={'valeur': ligne['valeur'],
                                  'saisie_par': utilisateur},
                    )
                self._executer(services.calculer_note, note,
                               plafond_rattrapage=services.plafond_effectif(semestre))
                traitees += 1

        # Aucun nom, aucun matricule en retour : la réponse ne doit pas
        # renseigner celui qui corrige.
        return Response({'copies_traitees': traitees})

    @action(detail=False, methods=['post'], url_path='recalculer-lot')
    def recalculer_lot(self, request):
        """Recalcule toutes les notes d'un semestre (et d'une classe si fournie)."""
        semestre_id = request.data.get('semestre')
        if not semestre_id:
            raise ValidationError({'semestre': 'Semestre requis.'})
        semestre = SemestreIPGEI.objects.filter(pk=semestre_id).first()
        if semestre is None:
            raise ValidationError({'semestre': 'Semestre introuvable.'})
        qs = Note.objects.filter(semestre=semestre)
        if request.data.get('classe'):
            qs = qs.filter(inscription__classe_id=request.data['classe'])
        # Le plafond figé sur la session, pas le paramétrage du jour : rejouer
        # un calcul doit redonner le même résultat.
        total = self._executer(services.recalculer_notes, qs,
                               plafond_rattrapage=services.plafond_effectif(semestre))
        return Response({'notes_recalculees': total})


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
        self._recalculer(evaluation.note)

    def perform_update(self, serializer):
        evaluation = serializer.save()
        self._recalculer(evaluation.note)

    def perform_destroy(self, instance):
        note = instance.note
        instance.delete()
        if not note.verrouillee:
            self._recalculer(note)

    @staticmethod
    def _recalculer(note):
        services.calculer_note(
            note, plafond_rattrapage=services.plafond_effectif(note.semestre),
        )


# ═════════════════════════════════════════════════════════════════════════════
# Délibération
# ═════════════════════════════════════════════════════════════════════════════
class DeliberationViewSet(_ErreurMetierMixin, InstitutionScopedMixin, ModelViewSet):
    """Jurys IPGEI : calcul des moyennes, décisions, validation."""
    serializer_class   = DeliberationSerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_deliberation'
    queryset           = Deliberation.objects.select_related(
        'institution', 'semestre', 'validee_par', 'classe')
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['niveau', 'classe', 'annee_universitaire', 'portee',
                          'statut', 'semestre']
    # La plus récente en tête : on ouvre cet écran pour le jury en cours, pas
    # pour celui de l'an dernier. L'année d'abord — un jury de 2026-2027 créé
    # avant un rattrapage de 2025-2026 reste devant lui —, puis la création.
    ordering           = ['-annee_universitaire', '-date_creation']
    pagination_class   = StandardPagination

    def perform_create(self, serializer):
        institution = serializer.validated_data.get('institution') or _institution_principale()
        params = ParametresIPGEI.pour(institution) if institution else None
        # Le seuil et le plafond sont recopiés des paramètres SI le client ne
        # les impose pas : la délibération devient alors autoportante. Le seuil
        # suit la PORTÉE — valider un semestre et prononcer un passage d'année
        # ne se jugent pas sur la même exigence.
        extra = {'institution': institution}
        if params is not None:
            if 'seuil_validation' not in serializer.validated_data:
                portee = serializer.validated_data.get('portee', Deliberation.PORTEE_ANNUELLE)
                extra['seuil_validation'] = params.seuil_pour(portee)
            if 'plafond_rattrapage' not in serializer.validated_data:
                extra['plafond_rattrapage'] = params.plafond_rattrapage
        serializer.save(**extra)

    #: Ce qu'une délibération validée ne peut plus laisser toucher. Le seuil et
    #: le plafond ont produit les décisions déjà notifiées ; les changer après
    #: coup ferait dire au PV signé autre chose que ce qu'il affirme.
    CHAMPS_FIGES = ('seuil_validation', 'plafond_rattrapage', 'niveau',
                    'classe', 'portee', 'semestre', 'annee_universitaire')

    def perform_update(self, serializer):
        instance = serializer.instance
        if instance.est_verrouillee:
            touches = [champ for champ in self.CHAMPS_FIGES
                       if champ in serializer.validated_data
                       and serializer.validated_data[champ] != getattr(instance, champ)]
            if touches:
                raise ValidationError(
                    'Délibération validée : ' + ', '.join(touches) +
                    ' ne peu' + ('vent' if len(touches) > 1 else 't') +
                    " plus changer. Dévalidez d'abord — les décisions déjà "
                    'émises seront alors remises en cause.')
        serializer.save()

    def perform_destroy(self, instance):
        if instance.est_verrouillee:
            raise ValidationError('Délibération validée — suppression interdite.')
        # Les inscriptions nées d'un passage pointent la ligne qui les a
        # décidées : effacer la délibération effacerait l'origine de dossiers
        # bien réels. La base le refuserait de toute façon (PROTECT) — mais
        # avec un message que personne ne comprend.
        issues = InscriptionIPGEI.objects.filter(
            issue_de__deliberation=instance).count()
        if issues:
            raise ValidationError(
                f'{issues} inscription(s) découlent de ce jury : la supprimer '
                "effacerait leur origine. Défaites d'abord ces inscriptions."
            )
        instance.delete()

    @action(detail=True, methods=['post'], url_path='calculer')
    def calculer(self, request, pk=None):
        """
        (Re)calcule le jury, éventuellement sous un AUTRE seuil.

        Le seuil est figé à la création pour qu'un recalcul redonne le même
        résultat. Mais le jury, en séance, veut voir ce que donnerait un seuil
        de 9,50 avant d'en décider : passer `seuil_validation` dans le corps le
        pose sur la délibération PUIS recalcule, en une fois. Deux requêtes
        laisseraient, entre les deux, une délibération dont le seuil ne
        correspond plus aux décisions affichées.
        """
        deliberation = self.get_object()

        seuil = request.data.get('seuil_validation')
        if seuil not in (None, ''):
            if deliberation.est_verrouillee:
                raise ValidationError(
                    {'seuil_validation': 'Délibération validée : le seuil qui a '
                                         'fondé les décisions ne peut plus changer.'})
            try:
                seuil = Decimal(str(seuil))
            except (ArithmeticError, TypeError, ValueError):
                raise ValidationError({'seuil_validation': 'Seuil invalide.'})
            if not (Decimal('0') <= seuil <= Decimal('20')):
                raise ValidationError(
                    {'seuil_validation': 'Le seuil se situe entre 0 et 20.'})
            deliberation.seuil_validation = seuil
            deliberation.save(update_fields=['seuil_validation'])

        total = self._executer(services.calculer_deliberation, deliberation)
        deliberation.refresh_from_db()
        return Response({
            'lignes': total,
            'deliberation': self.get_serializer(deliberation).data,
        })

    @action(detail=True, methods=['get', 'post'], url_path='passage')
    def passage(self, request, pk=None):
        """
        Ouvre l'année suivante aux étudiants que le jury a fait repartir.

        GET  → qui repart, vers quel niveau, et qui est déjà inscrit : de quoi
               décider avant d'agir.
        POST → crée les inscriptions dans la classe d'attente du niveau visé.
               `niveau` tranche quand plusieurs niveaux partagent le rang ;
               `annee` force l'année cible, celle qui suit par défaut.

        Deux verbes sur une même action : deux `@action` de même `url_path` se
        masqueraient dans le routeur, la seconde répondant 405.
        """
        deliberation = self.get_object()

        if request.method == 'GET':
            candidats = self._executer(services.candidats_passage, deliberation)
            cible = services.annee_suivante(deliberation.annee_universitaire)
            deja = set(
                InscriptionIPGEI.objects
                .filter(annee_universitaire=cible,
                        etudiant__in=[c['inscription'].etudiant_id for c in candidats])
                .values_list('etudiant_id', flat=True)
            )
            return Response({
                'annee_cible': cible,
                'validee':     deliberation.statut == Deliberation.STATUT_VALIDEE,
                'annuelle':    deliberation.portee == Deliberation.PORTEE_ANNUELLE,
                'candidats': [
                    {
                        'inscription':        c['inscription'].pk,
                        'etudiant_nom':       c['inscription'].etudiant.nom_display,
                        'etudiant_matricule': c['inscription'].etudiant.matricule,
                        'suite':              c['suite'],
                        'niveaux':            c['niveaux'],
                        'deja_inscrit':       c['inscription'].etudiant_id in deja,
                    }
                    for c in candidats
                ],
            })

        bilan = self._executer(
            services.passer_en_annee_suivante, deliberation,
            niveau_cible=request.data.get('niveau') or '',
            annee_cible=request.data.get('annee') or '',
            utilisateur=_utilisateur(request),
        )
        return Response(bilan)

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

    @action(detail=True, methods=['post'], url_path='devalider')
    def devalider(self, request, pk=None):
        """
        Retire la validation d'un jury — réservé à un administrateur.

        Dévalider, c'est remettre en cause des décisions déjà notifiées et
        rouvrir des notes arrêtées. L'acte dépasse la conduite d'une séance : il
        engage l'établissement, au même titre que la réouverture d'une campagne
        de saisie.
        """
        utilisateur = _utilisateur(request)
        est_admin = bool(utilisateur and (
            utilisateur.is_superuser or getattr(utilisateur, 'role', '') == 'admin'
        ))
        if not est_admin:
            return Response(
                {'detail': 'Seul un administrateur peut dévalider une délibération.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        deliberation = self._executer(services.devalider_deliberation,
                                      self.get_object(), utilisateur)
        return Response(self.get_serializer(deliberation).data)

    # ── Procès-verbal ────────────────────────────────────────────────────────
    def get_permissions(self):
        # Lire et signer le PV n'est pas gérer les jurys : un enseignant qui y
        # siège doit pouvoir les deux sans détenir le module.
        if self.action in ('signer', 'pv_pdf', 'pv_excel'):
            return [MembreDuJuryOuRBAC()]
        return super().get_permissions()

    @action(detail=True, methods=['post'], url_path='signer')
    def signer(self, request, pk=None):
        """Signature du PV par le membre de jury connecté."""
        membre = self._executer(services.signer_pv, self.get_object(), _utilisateur(request))
        return Response(MembreJuryIPGEISerializer(membre).data)

    @action(detail=True, methods=['get'], url_path='pv-pdf')
    def pv_pdf(self, request, pk=None):
        from django.http import HttpResponse

        deliberation = self.get_object()
        contenu = pv.generer_pdf(deliberation, base_url=request.build_absolute_uri('/'))
        reponse = HttpResponse(contenu, content_type='application/pdf')
        reponse['Content-Disposition'] = f'attachment; filename="{_nom_pv(deliberation)}.pdf"'
        return reponse

    @action(detail=True, methods=['get'], url_path='detail-notes')
    def detail_notes_pdf(self, request, pk=None):
        """
        Document de séance : un bloc par élève, détail matière par matière.

        Complète le PV sans le remplacer — le PV donne une ligne et une décision
        par élève, celui-ci donne les notes qui la fondent, avec la moyenne de
        la classe en regard.
        """
        from django.http import HttpResponse

        deliberation = self.get_object()
        # `tri` commande l'ordre des blocs : par classement pour délibérer du
        # haut vers le bas, par matricule pour retrouver un dossier qu'on
        # presente au jury, par moyenne pour examiner les cas limites.
        tri = request.query_params.get('tri') or detail_notes.TRI_RANG
        contenu = self._executer(detail_notes.generer_pdf, deliberation, tri)
        reponse = HttpResponse(contenu, content_type='application/pdf')
        reponse['Content-Disposition'] = (
            f'attachment; filename="detail-notes-{_nom_pv(deliberation)}-{tri}.pdf"')
        return reponse

    @action(detail=True, methods=['get'], url_path='pv-excel')
    def pv_excel(self, request, pk=None):
        from django.http import HttpResponse

        deliberation = self.get_object()
        contenu = pv.generer_excel(deliberation)
        reponse = HttpResponse(
            contenu,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        reponse['Content-Disposition'] = f'attachment; filename="{_nom_pv(deliberation)}.xlsx"'
        return reponse


class MembreDuJuryOuRBAC(BasePermission):
    """
    Accès au procès-verbal : le droit sur le module, ou le fait d'y siéger.

    Un enseignant désigné au jury ne détient pas `ipgei_deliberation` — il ne
    gère pas les jurys, il siège dans un seul. Sans cette ouverture il ne
    pourrait ni lire le PV ni y apposer sa signature, ce qui viderait la
    collégialité de son sens. L'appartenance est vérifiée sur la délibération
    demandée, et `services.signer_pv` refuse de toute façon toute signature
    apposée pour autrui.
    """

    def has_permission(self, request, view):
        if RBACPermission().has_permission(request, view):
            return True
        utilisateur = request.user
        if not (utilisateur and utilisateur.is_authenticated):
            return False
        try:
            deliberation_id = int(view.kwargs.get('pk'))
        except (TypeError, ValueError):
            return False
        return MembreJuryIPGEI.objects.filter(
            deliberation_id=deliberation_id, utilisateur=utilisateur,
        ).exists()


def _nom_pv(deliberation) -> str:
    """Nom de fichier du PV : lisible, sans accent ni espace."""
    cible = (deliberation.semestre.code if deliberation.semestre_id
             else deliberation.annee_universitaire)
    return _nom_fichier(f'PV_{deliberation.niveau}_{cible}_{deliberation.annee_universitaire}')


class MembreJuryIPGEIViewSet(_ErreurMetierMixin, ListModelMixin, RetrieveModelMixin,
                             CreateModelMixin, DestroyModelMixin, GenericViewSet):
    """
    Composition du jury d'une délibération.

    Pas de mise à jour : changer le rôle ou la personne d'un membre déjà inscrit
    reviendrait à réécrire qui a siégé. On retire et on réinscrit — et un membre
    qui a signé ne se retire pas.
    """
    serializer_class   = MembreJuryIPGEISerializer
    permission_classes = [RBACPermission]
    required_module    = 'ipgei_deliberation'
    queryset           = MembreJuryIPGEI.objects.select_related('utilisateur', 'deliberation')
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['deliberation', 'role']
    ordering           = ['deliberation', 'id']
    pagination_class   = NoPagination

    def create(self, request, *args, **kwargs):
        entree = self.get_serializer(data=request.data)
        entree.is_valid(raise_exception=True)
        membre = self._executer(
            services.ajouter_membre_jury,
            entree.validated_data['deliberation'],
            entree.validated_data['utilisateur'],
            entree.validated_data['role'],
        )
        return Response(self.get_serializer(membre).data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        self._executer(services.retirer_membre_jury, instance)


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
#: Rôles qui portent la décision sur les emplois du temps. L'« action directe »
#: leur était déjà réservée dans les faits ; on la nomme ici plutôt que de la
#: laisser dépendre d'une case à cocher que n'importe qui pouvait cocher.
ROLES_DECISION_EDT = ('admin', 'DG', 'DE', 'DA')


def _peut_trancher_seul(utilisateur) -> bool:
    """Cet utilisateur peut-il valider une permutation sans l'accord d'un tiers ?"""
    if utilisateur is None:
        return False
    return bool(getattr(utilisateur, 'is_superuser', False)) or         getattr(utilisateur, 'role', '') in ROLES_DECISION_EDT


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

    @action(detail=False, methods=['post'], url_path='permuter-maintenant')
    def permuter_maintenant(self, request):
        """
        Demande, valide et applique l'échange en une seule transaction.

        Le circuit A → accord de B → validation du directeur reste la règle. Ce
        raccourci ne le contourne que pour qui pouvait déjà trancher seul —
        c'est exactement ce que faisait « action directe », mais sans obliger à
        quitter la grille pour appliquer, en trois appels et deux écrans.

        Le droit est exigé **explicitement** : une action personnalisée n'est
        pas dans `ACTION_MAP` et retomberait sinon sur « voir », ce qui
        laisserait appliquer une permutation à qui n'a que la lecture.
        """
        utilisateur = _utilisateur(request)
        if not _peut_trancher_seul(utilisateur):
            raise PermissionDenied(
                "Vous pouvez demander la permutation, mais sa validation revient "
                "à la direction."
            )

        entree = self.get_serializer(data=request.data)
        entree.is_valid(raise_exception=True)

        with transaction.atomic():
            permutation = entree.save(
                demande_par=utilisateur, action_directe=True,
                statut=StatutWorkflow.VALIDEE, valide_par=utilisateur,
                date_validation=_tz.now(),
            )
            impactees = self._executer(
                services.appliquer_permutation_prof, permutation, utilisateur,
            )

        permutation.refresh_from_db()
        donnees = self.get_serializer(permutation).data
        donnees['seances_impactees'] = impactees if isinstance(impactees, int) else 0
        return Response(donnees, status=status.HTTP_201_CREATED)


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

    @action(detail=False, methods=['get'], url_path='formulaire')
    def formulaire(self, request):
        """
        Formulaire papier de demande, à télécharger, remplir et signer.

        Vierge : c'est l'étudiant qui le complète à la main. Le pré-remplir
        supposerait de savoir qui demande quoi avant qu'il l'ait demandé.
        """
        from datetime import date

        from django.http import HttpResponse

        from core.pdf_renderer import render_pdf_bytes
        from core.pdf_utils import get_institution_context

        # Même en-tête que le suivi : nom, groupe et logo viennent de la base.
        contexte = get_institution_context()
        contexte.update({
            'annee_universitaire': (request.query_params.get('annee')
                                    or _annee_session(request) or ''),
            'date_edition': date.today().strftime('%d/%m/%Y'),
        })
        contenu = render_pdf_bytes(
            'ipgei/formulaire_permutation.html',
            contexte,
            {
                'page-size': 'A4', 'orientation': 'Portrait',
                'margin-top': '12mm', 'margin-bottom': '12mm',
                'margin-left': '14mm', 'margin-right': '14mm',
                'encoding': 'UTF-8', 'enable-local-file-access': None,
            },
        )
        reponse = HttpResponse(contenu, content_type='application/pdf')
        reponse['Content-Disposition'] = (
            'attachment; filename="demande-changement-de-classe.pdf"')
        return reponse

    @action(detail=False, methods=['post'], url_path='appliquer-maintenant')
    def appliquer_maintenant(self, request):
        """
        Enregistre et applique le mouvement en une seule transaction.

        Le circuit demande → accord → validation n'a pas d'objet ici : la
        demande arrive sur un FORMULAIRE PAPIER rempli et signé, et c'est la
        direction des études qui tranche. Faire ressaisir dans l'application
        l'accord de chacun redemanderait par écran ce qui a déjà été signé —
        et, pour un échange, exigerait deux demandes là où il n'y a qu'une
        décision.

        Le droit est exigé explicitement : une action personnalisée n'est pas
        dans `ACTION_MAP` et retomberait sinon sur « voir ».
        """
        utilisateur = _utilisateur(request)
        if not _peut_trancher_seul(utilisateur):
            raise PermissionDenied(
                'Le mouvement est prononcé par la direction des études.')

        entree = self.get_serializer(data=request.data)
        entree.is_valid(raise_exception=True)

        with transaction.atomic():
            inscription = entree.validated_data['inscription']
            permutation = entree.save(
                classe_source=inscription.classe,
                demande_par=utilisateur, action_directe=True,
                statut=StatutWorkflow.VALIDEE, valide_par=utilisateur,
                date_validation=_tz.now(),
            )
            self._executer(services.appliquer_permutation_etudiant,
                           permutation, utilisateur)

        permutation.refresh_from_db()
        return Response(self.get_serializer(permutation).data,
                        status=status.HTTP_201_CREATED)

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

    def _signataire_id(self, request):
        """
        Signataire choisi à la génération, ou None pour celui par défaut.

        Sert aux documents que le chef de scolarité signe : quand il est absent,
        l'agent désigne à sa place qui signera, sans que le PDF sorte à un nom
        qui n'est pas celui de la main qui l'a signé.
        """
        return request.data.get('signataire') or None

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
            signataire_id=self._signataire_id(request),
        )
        return self._reponse_pdf(doc, pdf)

    @action(detail=False, methods=['post'], url_path='releve-annuel')
    def releve_annuel(self, request):
        from . import documents as docs_ipgei
        inscription = self._inscription(request)
        doc, pdf = self._executer(
            docs_ipgei.generer_releve_annuel, inscription,
            utilisateur=_utilisateur(request), base_url=self._base_url(),
            signataire_id=self._signataire_id(request),
        )
        return self._reponse_pdf(doc, pdf)

    @action(detail=False, methods=['post'], url_path='attestation-inscription')
    def attestation_inscription(self, request):
        from . import documents as docs_ipgei
        doc, pdf = self._executer(
            docs_ipgei.generer_attestation_inscription, self._inscription(request),
            utilisateur=_utilisateur(request), base_url=self._base_url(),
            signataire_id=self._signataire_id(request),
        )
        return self._reponse_pdf(doc, pdf)

    @action(detail=False, methods=['post'], url_path='recu-paiement')
    def recu_paiement(self, request):
        from . import documents as docs_ipgei
        doc, pdf = self._executer(
            docs_ipgei.generer_recu_paiement, self._inscription(request),
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
            signataire_id=self._signataire_id(request),
        )
        return self._reponse_pdf(doc, pdf)

    def _jury_et_autorises(self, request, classe):
        """
        Le jury de cette classe, et les seuls étudiants qu'il autorise à concourir.

        Le jury est celui que l'agent a désigné, ou — le cas courant, puisqu'une
        délibération porte sur une classe — le dernier jury ANNUEL validé de la
        classe. Le déduire évite de faire choisir deux fois la même chose.
        """
        identifiant = request.data.get('deliberation')
        if identifiant:
            deliberation = Deliberation.objects.filter(pk=identifiant).first()
            if deliberation is None:
                raise ValidationError({'deliberation': 'Délibération introuvable.'})
        else:
            deliberation = (
                Deliberation.objects
                .filter(classe=classe, portee=Deliberation.PORTEE_ANNUELLE,
                        statut=Deliberation.STATUT_VALIDEE)
                .order_by('-date_validation', '-id').first()
            )
        if deliberation is None:
            raise ValidationError(
                f"{classe.nom} n'a pas de jury annuel validé : l'autorisation de "
                'concourir atteste une décision, elle ne peut la précéder.'
            )

        autorisees = set(
            deliberation.lignes
            .filter(decision=LigneDeliberation.DECISION_CNIM)
            .values_list('inscription_id', flat=True)
        )
        retenues = [i for i in
                    InscriptionIPGEI.objects
                    .filter(classe=classe, actif=True)
                    .select_related('etudiant', 'classe__institution')
                    .order_by('numero_ordre', 'etudiant__nom')
                    if i.pk in autorisees]
        if not retenues:
            # Dire ce que le jury A prononcé : sur une classe de première année,
            # « aucun autorisé » est la règle et non un incident, et l'agent doit
            # comprendre qu'il n'y a rien à corriger.
            from collections import Counter
            comptes = Counter(
                l.get_decision_display() or 'sans décision'
                for l in deliberation.lignes.all()
            )
            detail = ', '.join(f'{n} × « {d} »' for d, n in comptes.most_common(3))
            raise ValidationError(
                f"Aucun étudiant de {classe.nom} n'est autorisé à concourir. "
                f'Le jury a prononcé {detail}. '
                "L'autorisation CNIM est une décision de DEUXIÈME année : une "
                'classe de première année ne peut pas en produire.'
            )
        return deliberation, retenues

    @action(detail=False, methods=['post'], url_path='generer-lot')
    def generer_lot(self, request):
        """
        Édite un document pour toute une classe, fusionné en un seul PDF.

        Corps : `{type_document, classe, semestre}` — le semestre n'étant exigé
        que par le relevé de semestre.

        Un fichier par élève obligerait à ouvrir quarante pièces puis à les
        imprimer une par une. Chaque élève garde pourtant SON document au
        registre : numéro de série, empreinte et QR de vérification. La fusion
        ne concerne que le papier.

        Un élève en échec est reporté dans les en-têtes de la réponse et
        n'interrompt pas le lot : sur une promotion, un seul cas bloquant
        empêcherait d'imprimer les trente-neuf autres.
        """
        import json

        from django.http import HttpResponse

        from . import documents as docs_ipgei

        type_document = request.data.get('type_document') or ''
        classe = Classe.objects.filter(pk=request.data.get('classe')).first()
        if classe is None:
            raise ValidationError({'classe': 'Classe requise.'})

        semestre = None
        if request.data.get('semestre'):
            semestre = SemestreIPGEI.objects.filter(
                pk=request.data['semestre']).first()
            if semestre is None:
                raise ValidationError({'semestre': 'Semestre introuvable.'})

        inscriptions = list(
            InscriptionIPGEI.objects
            .filter(classe=classe, actif=True)
            .select_related('etudiant', 'classe__institution')
            .order_by('numero_ordre', 'etudiant__nom')
        )
        if not inscriptions:
            raise ValidationError(f"{classe.nom} n'a aucun inscrit actif.")

        # L'autorisation de concourir découle d'un jury, et ne concerne QUE les
        # étudiants qu'il a autorisés. On restreint donc la fournée à ceux-là :
        # partir de la classe entière ferait sortir un refus par redoublant, et
        # un lot de quarante afficherait trente-deux lignes d'échec pour huit
        # pièces — le bilan dirait l'inverse de ce qui s'est passé.
        deliberation = None
        if type_document == docs_ipgei.TYPE_CNIM:
            deliberation, inscriptions = self._jury_et_autorises(request, classe)

        contenu, edites, total, erreurs = self._executer(
            docs_ipgei.generer_lot, inscriptions, type_document,
            semestre=semestre, deliberation=deliberation,
            utilisateur=_utilisateur(request), base_url=self._base_url(),
            # Un seul signataire pour toute la fournée : quarante pièces d'un
            # même tirage se signent de la même main.
            signataire_id=self._signataire_id(request),
        )

        libelle = _nom_fichier(
            f'{type_document}_{classe.nom}'
            + (f'_{semestre.code}' if semestre else ''))
        reponse = HttpResponse(contenu, content_type='application/pdf')
        reponse['Content-Disposition'] = f'attachment; filename="{libelle}.pdf"'
        reponse['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        # Le bilan voyage dans les en-têtes : le corps est le PDF lui-même, et
        # l'écran doit pouvoir dire « 39 sur 40 » sans seconde requête.
        reponse['X-Edites']  = str(edites)
        reponse['X-Total']   = str(total)
        reponse['X-Erreurs'] = json.dumps(erreurs, ensure_ascii=False)
        reponse['Access-Control-Expose-Headers'] = 'X-Edites, X-Total, X-Erreurs'
        return reponse

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

        # Répartition par niveau, lue au référentiel plutôt qu'écrite en dur :
        # MPSI et MP étaient les deux seuls possibles, ils ne le sont plus.
        # `niveau` est une clé étrangère depuis que `Classe` hérite de
        # `Departement` — la filtrer par un code exige de traverser la relation,
        # faute de quoi la requête compare un texte à un identifiant.
        par_niveau = []
        for fiche in NiveauIPGEI.objects.filter(actif=True).order_by('rang', 'ordre', 'code'):
            par_niveau.append({
                'code':      fiche.code,
                'libelle':   fiche.libelle or f'{fiche.code} — {fiche.libelle_rang}',
                'classes':   classes.filter(niveau__niveau=fiche.code).count(),
                'effectifs': inscriptions.filter(classe__niveau__niveau=fiche.code).count(),
            })

        return Response({
            'annee_universitaire': annee or '',
            'classes': {
                'total': classes.count(),
            },
            'effectifs': {
                'total': inscriptions.count(),
            },
            'par_niveau': par_niveau,
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
        """
        Années universitaires proposées par le sélecteur global.

        UNION de deux ensembles, et c'est voulu :

          · celles DÉCLARÉES au registre du socle (`parametres.Year`, l'écran
            Paramètres → Années), pour qu'une année ouverte par la scolarité
            soit sélectionnable avant même qu'une classe y existe ;
          · celles qui portent DÉJÀ des classes IPGEI, pour qu'aucune année
            peuplée ne devienne inatteignable si elle manque au registre.

        Ne lire que le registre ferait disparaître des écrans une année où des
        étudiants sont inscrits ; ne lire que les classes laisse le registre et
        IPGEI diverger sans que rien ne le dise. Les deux sources restent
        alignées par `socle.annee_socle()`, appelé dès qu'IPGEI ouvre une année.

        `?saisissables=1` ne renvoie que celles où une note peut encore être
        écrite — un semestre au moins non clôturé, et l'année non close au
        registre. Proposer un choix qui ne mène qu'à un refus n'est pas un choix.
        """
        from apps.parametres.models import Year

        annees = sorted(
            set(Classe.objects.values_list('annee_universitaire', flat=True))
            | set(Year.objects.values_list('annee', flat=True)),
            reverse=True,
        )
        if str(request.query_params.get('saisissables', '')).lower() in ('1', 'true', 'on'):
            # Deux verrous, indépendants : l'année close au registre du socle
            # (décision d'établissement) et le semestre clôturé au calendrier
            # IPGEI. N'en regarder qu'un laissait proposer une année fermée.
            closes = set(
                Year.objects.filter(est_cloturee=True).values_list('annee', flat=True)
            )
            ouvertes = set(
                SemestreIPGEI.objects.filter(est_cloture=False)
                .values_list('annee_universitaire', flat=True)
            )
            annees = [a for a in annees if a in ouvertes and a not in closes]
        return Response(annees)
