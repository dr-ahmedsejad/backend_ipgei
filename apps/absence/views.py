# import logging
# from django.db.models import Count, Q
# from rest_framework import viewsets, generics, status
# from rest_framework.decorators import action
# from rest_framework.response import Response
# from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
# from django_filters.rest_framework import DjangoFilterBackend
# from rest_framework.filters import SearchFilter
# from core.permissions import RBACPermission
# from core.mixins import AuditMixin, SelectAllMixin
# from core.pagination import StandardPagination
# from .models import Etudiant, Presence, SeuilAbsence
# from .serializers import (
#     EtudiantSerializer, PresenceSerializer,
#     PresenceBulkSerializer, SeuilAbsenceSerializer,
# )
#
# logger = logging.getLogger('siga')
#
#
# class EtudiantViewSet(AuditMixin, SelectAllMixin, viewsets.ModelViewSet):
#     queryset = Etudiant.objects.select_related('departement').all()
#     serializer_class = EtudiantSerializer
#     permission_classes = [RBACPermission]
#     required_module = 'absences'
#     parser_classes = [MultiPartParser, FormParser, JSONParser]
#     filter_backends = [DjangoFilterBackend, SearchFilter]
#     filterset_fields = ['departement', 'genre']
#     search_fields = ['nom', 'matricule']
#     pagination_class = StandardPagination
#
#     @action(detail=False, methods=['post'], url_path='importer', parser_classes=[MultiPartParser])
#     def importer(self, request):
#         """Import CSV/Excel d'étudiants pour un département."""
#         departement_id = request.data.get('departement_id')
#         fichier = request.FILES.get('fichier')
#         if not departement_id or not fichier:
#             return Response({'error': 'departement_id et fichier requis.'}, status=400)
#         try:
#             import openpyxl
#             wb = openpyxl.load_workbook(fichier)
#             ws = wb.active
#             created = 0
#             for row in ws.iter_rows(min_row=2, values_only=True):
#                 if not row[0]:
#                     continue
#                 obj, created_flag = Etudiant.objects.update_or_create(
#                     matricule=str(row[0]).strip(),
#                     defaults={
#                         'nom': str(row[1]).strip() if len(row) > 1 else '',
#                         'departement_id': departement_id,
#                         'genre': str(row[2]).strip() if len(row) > 2 else 'M',
#                     }
#                 )
#                 if created_flag:
#                     created += 1
#             return Response({'imported': created}, status=status.HTTP_201_CREATED)
#         except Exception as e:
#             logger.exception('Import étudiants échoué')
#             return Response({'error': str(e)}, status=500)
#
#
# class PresenceViewSet(AuditMixin, viewsets.ModelViewSet):
#     queryset = Presence.objects.select_related('suivi', 'etudiant').all()
#     serializer_class = PresenceSerializer
#     permission_classes = [RBACPermission]
#     required_module = 'absences'
#     parser_classes = [MultiPartParser, FormParser, JSONParser]
#     filter_backends = [DjangoFilterBackend]
#
#     # Correction : Ajout des filtres liés au modèle Suivi pour une recherche précise
#     filterset_fields = [
#         'suivi',
#         'etudiant',
#         'statut',
#         'suivi__jour',
#         'suivi__numero_semaine',
#         'suivi__annee_universitaire'
#     ]
#     pagination_class = StandardPagination
#
#     @action(detail=False, methods=['post'], url_path='bulk')
#     def bulk_update(self, request):
#         """Mise à jour en masse des présences pour une séance."""
#         s = PresenceBulkSerializer(data=request.data)
#         s.is_valid(raise_exception=True)
#         d = s.validated_data
#         updated = 0
#         for item in d['presences']:
#             Presence.objects.update_or_create(
#                 suivi_id=d['suivi_id'],
#                 etudiant_id=item.get('etudiant_id'),
#                 defaults={
#                     'statut': item.get('statut', 0),
#                     'commentaire': item.get('commentaire', ''),
#                 }
#             )
#             updated += 1
#         return Response({'updated': updated})
#
#     @action(detail=False, methods=['get'], url_path='rapport')
#     def rapport(self, request):
#         """Rapport des absences par département."""
#         annee = request.query_params.get('annee_universitaire')
#         dept_id = request.query_params.get('departement')
#         if not annee:
#             return Response({'error': 'annee_universitaire requis.'}, status=400)
#         qs = Presence.objects.filter(suivi__annee_universitaire=annee, statut__in=[1, 2])
#         if dept_id:
#             qs = qs.filter(etudiant__departement_id=dept_id)
#         data = qs.values(
#             'etudiant__matricule', 'etudiant__nom', 'etudiant__departement__nom'
#         ).annotate(
#             total_absences=Count('id'),
#             absences_justifiees=Count('id', filter=Q(statut=3)),
#             absences_injustifiees=Count('id', filter=Q(statut=1)),
#         ).order_by('-total_absences')
#         return Response(list(data))
#
#     @action(detail=False, methods=['get'], url_path='par-etudiant')
#     def par_etudiant(self, request):
#         etudiant_id = request.query_params.get('etudiant')
#         if not etudiant_id:
#             return Response({'error': 'etudiant requis.'}, status=400)
#         qs = Presence.objects.filter(etudiant_id=etudiant_id).select_related('suivi__em', 'suivi__departement')
#         return Response(PresenceSerializer(qs, many=True).data)
#
#
# class SeuilAbsenceView(generics.RetrieveUpdateAPIView):
#     queryset = SeuilAbsence.objects.all()
#     serializer_class = SeuilAbsenceSerializer
#     permission_classes = [RBACPermission]
#     required_module = 'absences'
#
#     def get_object(self):
#         obj, _ = SeuilAbsence.objects.get_or_create(pk=1)
#         return obj

import logging
import openpyxl
import pdfkit
from django.conf import settings
from django.db.models import Count, Q
from django.template.loader import render_to_string
from django.http import HttpResponse
from rest_framework import viewsets, generics, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django_filters.rest_framework import DjangoFilterBackend
from django_filters import rest_framework as df_filters
from rest_framework.filters import SearchFilter, OrderingFilter

from core.permissions import RBACPermission
from core.mixins import AuditMixin, SelectAllMixin
from core.pagination import StandardPagination

from .models import Etudiant, Presence, SeuilAbsence


class EtudiantFilter(df_filters.FilterSet):
    """FilterSet explicite — évite les ChoiceFilter stricts générés automatiquement.

    Deux familles de filtres, sémantiques différentes :

    1. Snapshot courant (FK directes sur Etudiant) :
       - filiere     : Etudiant.filiere (état actuel, mutation à chaque progression)
       - departement : Etudiant.departement (groupe actuel)
       - genre, statut

    2. Via la chaîne d'inscription administrative — utile pour retrouver les
       étudiants d'une filière+niveau **pour une année donnée**, même si leur
       `Etudiant.filiere/departement` a changé suite à une progression :
       - inscrit_filiere : InscriptionAdministrative.filiere
       - inscrit_niveau  : InscriptionAdministrative.niveau (1, 2, 3...)
       - inscrit_annee   : InscriptionAdministrative.annee_univ.annee ('2024-2025')

    L'utilisation des filtres `inscrit_*` requiert un .distinct() côté ViewSet
    car un étudiant peut avoir plusieurs InscriptionAdministrative (une par année).
    """
    genre    = df_filters.CharFilter(field_name='genre',    lookup_expr='exact')
    statut   = df_filters.CharFilter(field_name='statut',   lookup_expr='exact')
    filiere  = df_filters.NumberFilter(field_name='filiere', lookup_expr='exact')
    departement = df_filters.NumberFilter(field_name='departement', lookup_expr='exact')

    # Note : les 3 filtres `inscrit_*` ci-dessous sont déclarés pour la doc/tests
    # mais l'application réelle se fait dans EtudiantViewSet.get_queryset() via
    # un .filter() unique — sinon Django génère 3 INNER JOIN distincts qui
    # peuvent matcher des lignes d'inscription différentes (faux positifs).
    inscrit_filiere = df_filters.CharFilter(method='_noop_marker')
    inscrit_niveau  = df_filters.CharFilter(method='_noop_marker')
    inscrit_annee   = df_filters.CharFilter(method='_noop_marker')

    def _noop_marker(self, queryset, name, value):
        # No-op — la jointure cohérente est appliquée par EtudiantViewSet.get_queryset().
        return queryset

    class Meta:
        model  = Etudiant
        fields = [
            'genre', 'statut', 'filiere', 'departement',
            'inscrit_filiere', 'inscrit_niveau', 'inscrit_annee',
        ]
from .serializers import (
    EtudiantSerializer, PresenceSerializer,
    PresenceBulkSerializer, SeuilAbsenceSerializer,
)

logger = logging.getLogger('siga')


def _check_abs_module(user, code, action='voir'):
    """Helper RBAC fin pour les @actions absences (rapport, justificatifs, import).
    Admin/superuser bypassent. Sinon raise PermissionDenied si pas autorise."""
    from core.permissions import _has_access
    from rest_framework.exceptions import PermissionDenied
    if user.role == 'admin' or user.is_superuser:
        return
    if not _has_access(user, code, action):
        raise PermissionDenied(f'Vous n\'avez pas le droit "{action}" sur "{code}".')


class EtudiantViewSet(AuditMixin, SelectAllMixin, viewsets.ModelViewSet):
    """
    Gestion des étudiants : CRUD, Recherche et Import Excel.
    """
    queryset = Etudiant.objects.select_related(
        'departement', 'departement__niveau', 'filiere',
    ).order_by('matricule')
    serializer_class   = EtudiantSerializer
    permission_classes = [RBACPermission]
    # NB : le module canonique pour l'Etudiant CRUD est 'scolarite_etudiants'.
    # Le @action 'importer' surdefinit explicitement vers 'sco_etudiants_import'
    # pour la sensibilite supplementaire (ecrasement masse).
    required_module    = 'scolarite_etudiants'
    parser_classes     = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        """
        Applique le filtre par chaîne InscriptionAdministrative en un seul .filter()
        pour garantir que les 3 conditions (filiere, niveau, annee) matchent la
        MÊME ligne d'inscription. Sinon Django génère des INNER JOIN distincts
        qui peuvent matcher 3 inscriptions différentes du même étudiant.
        """
        qs = super().get_queryset()

        # Diplômé ⇔ présent au registre des diplômes (source de vérité). On annote ici
        # pour que le serializer expose `statut_effectif='diplome'` sans requête N+1
        # (le champ Etudiant.statut n'est jamais basculé à 'diplome' à l'attribution).
        from django.db.models import Exists, OuterRef
        from apps.documents.models import RegistreDiplome
        qs = qs.annotate(
            _est_diplome=Exists(RegistreDiplome.objects.filter(etudiant=OuterRef('pk')))
        )

        ins_filtres = {}
        if (v := self.request.query_params.get('inscrit_filiere')):
            ins_filtres['inscriptions_admin__filiere_id'] = v
        if (v := self.request.query_params.get('inscrit_niveau')):
            ins_filtres['inscriptions_admin__niveau'] = v
        if (v := self.request.query_params.get('inscrit_annee')):
            ins_filtres['inscriptions_admin__annee_univ__annee'] = v

        if ins_filtres:
            qs = qs.filter(**ins_filtres).distinct()

        return qs
    filter_backends    = [DjangoFilterBackend, SearchFilter]
    filterset_class    = EtudiantFilter
    search_fields      = ['nom', 'matricule', 'nom_fr', 'prenom_fr', 'nom_ar', 'prenom_ar', 'email', 'cni', 'nbac']
    pagination_class   = StandardPagination

    # ── Comptes portail étudiant : audit + creation bulk ────────────────────
    @action(detail=True, methods=['get'], url_path='notes')
    def notes_etudiant(self, request, pk=None):
        """
        GET /api/v1/absences/etudiants/<id>/notes/
        Retourne tous les ResultatElement + ResultatSemestre de l'etudiant,
        organises par annee universitaire et semestre, avec les codes EM lisibles.
        """
        from apps.documents.services import calculer_resultat_semestre_consolide
        from apps.inscriptions.models import InscriptionPedagogique

        etu = self.get_object()

        # Relevé CONSOLIDÉ — STRICTEMENT identique au relevé officiel : pour chaque
        # semestre suivi on prend l'ANNÉE LA PLUS RÉCENTE comme vue, puis on délègue
        # le calcul (consolidation + compensation Art. 12-15) au helper PARTAGÉ
        # calculer_resultat_semestre_consolide — le même que le relevé PDF. Ainsi un EM
        # validé par compensation (VCI/VCS) compte ses crédits ici comme sur le relevé.
        ips = (
            InscriptionPedagogique.objects
            .filter(inscription_admin__etudiant=etu)
            .select_related('semestre', 'inscription_admin__annee_univ')
        )
        sem_latest = {}   # semestre_id -> (semestre_obj, year_obj)
        for ip in ips:
            sem = ip.semestre
            an  = ip.inscription_admin.annee_univ if ip.inscription_admin_id else None
            if not sem or not an:
                continue
            cur = sem_latest.get(sem.id)
            if cur is None or (an.annee or '') > (cur[1].annee or ''):
                sem_latest[sem.id] = (sem, an)

        elements  = []
        semestres = []
        for sem, annee in sem_latest.values():
            res = calculer_resultat_semestre_consolide(etu, sem, annee)
            if not res['modules']:
                continue
            for mod in res['modules']:
                for e in mod['elements']:
                    me = e['me']
                    # est_valide = décision APRÈS compensation (V / VCI / VCS), pas me>=10.
                    est_valide   = (e['decision'] == 'Validé')
                    # Affichage : me=None = note pas encore saisie (pas « éliminatoire »).
                    est_elim_aff = (me is not None and me < 6)
                    if me is None:
                        statut = ''
                    elif est_valide:
                        statut = 'V'
                    elif est_elim_aff:
                        statut = 'E'
                    else:
                        statut = 'NV'
                    elements.append({
                        'id':              None,
                        'em_code':         e['code'],
                        'em_intitule':     e['intitule_fr'],
                        'module_code':     mod['code'],
                        'module_intitule': mod['intitule_fr'],
                        'semestre_code':     sem.code_semestre,
                        'semestre_intitule': sem.semestre,
                        'session_type':    'consolide',
                        'annee':           annee.annee,          # année de la VUE (relevé)
                        'annee_source':    e.get('annee_source') or '',   # d'où vient la note
                        'is_acquis':       bool(e.get('is_acquis')),
                        'cc':              e['cc'],
                        'tp':              e['tp'],
                        'exam':            e['exam'],
                        'exam_rat':        e['exam_rat'],
                        'has_tp':          bool(e.get('has_tp')),
                        'note_finale':     str(me) if me is not None else None,
                        'est_valide':      est_valide,
                        'est_eliminatoire': est_elim_aff,
                        'code_statut':     statut,
                    })
            moy = res['moyenne_semestre']
            semestres.append({
                'semestre_code':     sem.code_semestre,
                'semestre_intitule': sem.semestre,
                'session_type':    'consolide',
                'annee':           annee.annee,
                'moyenne':         str(moy) if moy is not None else '0',
                'credits_valides': res['credits_valides'],
                'est_admis':       res['est_admis'],
                'code_statut':     'V' if res['est_admis'] else 'NV',
            })

        return Response({
            'etudiant': {
                'id': etu.id, 'matricule': etu.matricule,
                'nom': etu.nom_fr or etu.nom, 'prenom': etu.prenom_fr or '',
            },
            'elements':  elements,
            'semestres': semestres,
        })

    @action(detail=False, methods=['get'], url_path='comptes-status')
    def comptes_status(self, request):
        """
        GET /api/v1/absences/etudiants/comptes-status/
        Retourne uniquement les etudiants actifs SANS compte portail, classes par statut :
          - creable          : CNI + NBAC presents -> bouton "Creer compte"
          - sans_cni         : CNI manquant
          - sans_nbac        : NBAC manquant
          - username_pris    : un User avec username=CNI existe deja (cas rare, lien orphelin)
        """
        from django.contrib.auth import get_user_model
        User = get_user_model()

        qs = Etudiant.objects.filter(statut='actif', user__isnull=True).select_related('filiere', 'departement').order_by('matricule')
        items = []
        for e in qs:
            cni  = (e.cni or '').strip()
            nbac = (e.nbac or '').strip()
            if not cni:
                statut = 'sans_cni'
            elif not nbac:
                statut = 'sans_nbac'
            elif User.objects.filter(username=cni).exists():
                statut = 'username_pris'
            else:
                statut = 'creable'
            items.append({
                'id':         e.id,
                'matricule':  e.matricule,
                'nom':        e.nom_fr or e.nom,
                'prenom':     e.prenom_fr or '',
                'cni':        e.cni or '',
                'nbac':       e.nbac or '',
                'email':      e.email or '',
                'filiere':    e.filiere.intitule_fr if e.filiere_id else '',
                'statut_compte': statut,
                'login_propose': cni or '',
                'email_propose': f'{e.matricule}@isms.esp.mr' if e.matricule else '',
            })
        return Response({
            'count':   len(items),
            'results': items,
            'recap': {
                'total':         len(items),
                'creable':       sum(1 for x in items if x['statut_compte'] == 'creable'),
                'sans_cni':      sum(1 for x in items if x['statut_compte'] == 'sans_cni'),
                'sans_nbac':     sum(1 for x in items if x['statut_compte'] == 'sans_nbac'),
                'username_pris': sum(1 for x in items if x['statut_compte'] == 'username_pris'),
            },
        })

    @action(detail=False, methods=['post'], url_path='creer-comptes')
    def creer_comptes(self, request):
        """
        POST /api/v1/absences/etudiants/creer-comptes/
        Body : {
            "etudiant_ids": [1, 2, 3]   # optionnel : si vide -> tous les etudiants 'creable'
            "dry_run":      false       # optionnel
        }
        Reutilise la meme logique que la commande create_student_accounts :
            username = CNI, password = NBAC, email = {matricule}@isms.esp.mr,
            doit_changer_mdp = True. Au 1er login, username -> matricule (FirstLoginView).
        """
        from django.contrib.auth import get_user_model
        User = get_user_model()

        ids     = request.data.get('etudiant_ids') or []
        dry_run = bool(request.data.get('dry_run', False))

        qs = Etudiant.objects.filter(statut='actif', user__isnull=True)
        if ids:
            qs = qs.filter(id__in=ids)

        crees = []
        ignores = []
        for e in qs:
            cni  = (e.cni or '').strip()
            nbac = (e.nbac or '').strip()
            mat  = (e.matricule or '').strip()
            if not cni or not nbac:
                ignores.append({'matricule': e.matricule, 'raison': 'CNI ou NBAC manquant'})
                continue
            if User.objects.filter(username=cni).exists():
                ignores.append({'matricule': e.matricule, 'raison': f"username '{cni}' deja pris"})
                continue
            email = f'{mat}@isms.esp.mr'
            nom_complet = f"{e.prenom_fr or ''} {e.nom_fr or e.nom}".strip()
            if not dry_run:
                u = User.objects.create_user(
                    username=cni, password=nbac, email=email, name=nom_complet,
                    role='etudiant', doit_changer_mdp=True,
                )
                e.user = u
                e.save(update_fields=['user'])
            crees.append({
                'matricule': e.matricule, 'login': cni, 'mdp_initial': nbac, 'email': email,
            })

        return Response({
            'dry_run':       dry_run,
            'crees_count':   len(crees),
            'ignores_count': len(ignores),
            'crees':         crees,
            'ignores':       ignores,
        })

    @action(detail=False, methods=['get'], url_path='export')
    def export(self, request):
        """Exporte la liste des étudiants filtrée en Excel (.xlsx)."""
        import io
        qs = self.filter_queryset(self.get_queryset())
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Étudiants'
        headers = [
            'Matricule', 'Nom FR', 'Prénom FR', 'Nom AR', 'Prénom AR',
            'Genre', 'Date naissance', 'CNI', 'Téléphone', 'Email',
            'Filière', 'Département', 'Statut',
        ]
        ws.append(headers)
        for e in qs:
            ws.append([
                e.matricule,
                e.nom_fr or e.nom,
                e.prenom_fr,
                e.nom_ar,
                e.prenom_ar,
                e.get_genre_display(),
                str(e.date_naissance) if e.date_naissance else '',
                e.cni or '',
                e.telephone,
                e.email,
                e.filiere.intitule_fr if e.filiere_id else '',
                e.departement.nom if e.departement_id else '',
                e.get_statut_display(),
            ])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        from django.http import HttpResponse
        response = HttpResponse(
            buf.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = 'attachment; filename="etudiants.xlsx"'
        return response

    @action(detail=False, methods=['post'], url_path='importer', parser_classes=[MultiPartParser])
    def importer(self, request):
        """Import CSV/Excel d'étudiants pour un département."""
        # Gate granulaire : import = abs_import (action sensible)
        _check_abs_module(request.user, 'abs_import', action='modifier')
        departement_id = request.data.get('departement_id')
        fichier = request.FILES.get('fichier')

        if not departement_id or not fichier:
            return Response({'error': 'departement_id et fichier requis.'}, status=400)

        try:
            wb = openpyxl.load_workbook(fichier)
            ws = wb.active
            created = 0
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row or not row[0]:
                    continue
                obj, created_flag = Etudiant.objects.update_or_create(
                    matricule=str(row[0]).strip(),
                    defaults={
                        'nom': str(row[1]).strip() if len(row) > 1 else '',
                        'departement_id': departement_id,
                        'genre': str(row[2]).strip() if len(row) > 2 else 'M',
                    }
                )
                if created_flag:
                    created += 1
            return Response({'imported': created}, status=status.HTTP_201_CREATED)
        except Exception as e:
            logger.exception('Import étudiants échoué')
            return Response({'error': str(e)}, status=500)


class PresenceFilter(df_filters.FilterSet):
    """FilterSet avec alias annee_universitaire → suivi__annee_universitaire."""
    suivi               = df_filters.NumberFilter(field_name='suivi',                       lookup_expr='exact')
    # F-4 : permet de batcher plusieurs suivi en 1 seule requete
    # via ?suivi__in=1,2,3 (au lieu de N requetes paralleles ?suivi=X)
    suivi__in           = df_filters.BaseInFilter(field_name='suivi',                       lookup_expr='in')
    etudiant            = df_filters.NumberFilter(field_name='etudiant',                    lookup_expr='exact')
    statut              = df_filters.NumberFilter(field_name='statut',                      lookup_expr='exact')
    jour                = df_filters.CharFilter(field_name='suivi__jour_fk__jour',          lookup_expr='exact')
    numero_semaine      = df_filters.NumberFilter(field_name='suivi__numero_semaine',        lookup_expr='exact')
    annee_universitaire = df_filters.CharFilter(field_name='suivi__annee_universitaire',     lookup_expr='exact')
    type_semestre       = df_filters.CharFilter(field_name='suivi__type_semestre',           lookup_expr='exact')
    avec_justificatif   = df_filters.BooleanFilter(field_name='justificatif',                method='filter_justificatif')

    def filter_justificatif(self, queryset, name, value):
        if value:
            return queryset.exclude(justificatif='').exclude(justificatif__isnull=True)
        return queryset.filter(justificatif__isnull=True) | queryset.filter(justificatif='')

    class Meta:
        model  = Presence
        fields = ['suivi', 'etudiant', 'statut']


class PresenceViewSet(AuditMixin, viewsets.ModelViewSet):
    """
    Gestion des présences : Saisie de masse, Rapports et Justificatifs.
    """
    queryset = Presence.objects.select_related('suivi', 'etudiant').all()
    serializer_class = PresenceSerializer
    permission_classes = [RBACPermission]
    required_module = 'abs_saisie'
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filter_backends  = [DjangoFilterBackend, OrderingFilter]
    filterset_class  = PresenceFilter
    ordering_fields  = ['suivi__numero_semaine', 'suivi__date_suivie', 'etudiant__nom']
    ordering         = ['-suivi__numero_semaine']
    pagination_class = StandardPagination

    @action(detail=False, methods=['post'], url_path='bulk')
    def bulk_update(self, request):
        """Mise à jour en masse des présences pour une séance (Frontend Matrix)."""
        s = PresenceBulkSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        updated = 0

        for item in d['presences']:
            # On ne met à jour que statut et commentaire.
            # Le champ justificatif reste intact s'il existe déjà.
            Presence.objects.update_or_create(
                suivi_id=d['suivi_id'],
                etudiant_id=item.get('etudiant_id'),
                defaults={
                    'statut': item.get('statut', 0),
                    'commentaire': item.get('commentaire', ''),
                }
            )
            updated += 1
        return Response({'updated': updated}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='upload-justificatif', parser_classes=[MultiPartParser])
    def upload_justificatif(self, request):
        """
        Upload un fichier justificatif pour une absence précise.
        Passe automatiquement le statut à 3 (Justifiée).
        """
        # Gate granulaire : justificatifs (DA principalement)
        _check_abs_module(request.user, 'abs_justificatifs', action='modifier')
        etudiant_id = request.data.get('etudiant_id')
        suivi_id = request.data.get('suivi_id')
        fichier = request.FILES.get('justificatif')

        if not all([etudiant_id, suivi_id, fichier]):
            return Response({'error': 'etudiant_id, suivi_id et fichier (justificatif) requis.'}, status=400)

        from core.validators import validate_document
        from django.core.exceptions import ValidationError as DjangoValidationError
        try:
            validate_document(fichier)
        except DjangoValidationError as e:
            return Response({'error': e.messages}, status=400)

        try:
            presence, created = Presence.objects.update_or_create(
                etudiant_id=etudiant_id,
                suivi_id=suivi_id,
                defaults={
                    'justificatif': fichier,
                    'statut': 3,  # Statut "Justifiée"
                    'commentaire': request.data.get('commentaire', 'Justificatif fourni.')
                }
            )
            return Response({
                'message': 'Justificatif enregistré',
                'statut': presence.statut,
                'url': presence.justificatif.url
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.exception("Erreur upload justificatif")
            return Response({'error': str(e)}, status=500)

    @action(detail=False, methods=['get'], url_path='rapport')
    def rapport(self, request):
        """Rapport statistique des absences."""
        annee = request.query_params.get('annee_universitaire')
        dept_id = request.query_params.get('departement')

        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)

        date_debut = request.query_params.get('date_debut')
        date_fin   = request.query_params.get('date_fin')

        qs = Presence.objects.filter(suivi__annee_universitaire=annee, statut__in=[1, 2, 3])
        if dept_id:
            qs = qs.filter(etudiant__departement_id=dept_id)
        if date_debut:
            qs = qs.filter(suivi__date_suivie__gte=date_debut)
        if date_fin:
            qs = qs.filter(suivi__date_suivie__lte=date_fin)

        data = qs.values(
            'etudiant__matricule', 'etudiant__nom', 'etudiant__departement__nom'
        ).annotate(
            total_absences=Count('id'),
            absences_injustifiees=Count('id', filter=Q(statut=1)),
            sanctions=Count('id', filter=Q(statut=2)),
            absences_justifiees=Count('id', filter=Q(statut=3)),
        ).order_by('-absences_injustifiees')

        return Response(list(data))

    @action(detail=False, methods=['get'], url_path='rapport-par-jour')
    def rapport_par_jour(self, request):
        """Distribution des absences par jour de la semaine.

        Groupage via `suivi.jour_fk` (FK Jour). Permet au front de matcher
        chaque count avec un Jour.id du modele parametres.Jour.

        Filtres : annee_universitaire (requis), departement, date_debut, date_fin.
        Compte uniquement les statuts d'absence (1=non justifiee, 2=sanction, 3=justifiee).
        """
        annee = request.query_params.get('annee_universitaire')
        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)

        qs = Presence.objects.filter(suivi__annee_universitaire=annee, statut__in=[1, 2, 3])
        dept_id    = request.query_params.get('departement')
        date_debut = request.query_params.get('date_debut')
        date_fin   = request.query_params.get('date_fin')
        if dept_id:    qs = qs.filter(etudiant__departement_id=dept_id)
        if date_debut: qs = qs.filter(suivi__date_suivie__gte=date_debut)
        if date_fin:   qs = qs.filter(suivi__date_suivie__lte=date_fin)

        data = qs.values(
            'suivi__jour_fk_id', 'suivi__jour_fk__jour',
        ).annotate(
            n=Count('id'),
            n_injustifiees=Count('id', filter=Q(statut=1)),
            n_sanctions=Count('id', filter=Q(statut=2)),
            n_justifiees=Count('id', filter=Q(statut=3)),
        ).order_by('suivi__jour_fk_id')

        # Reformatage : enleve les NULL (suivis sans jour_fk)
        result = [
            {
                'jour_id':         d['suivi__jour_fk_id'],
                'jour':            d['suivi__jour_fk__jour'] or '(inconnu)',
                'total':           d['n'],
                'injustifiees':    d['n_injustifiees'],
                'sanctions':       d['n_sanctions'],
                'justifiees':      d['n_justifiees'],
            }
            for d in data if d['suivi__jour_fk_id'] is not None
        ]
        return Response(result)

    @action(detail=False, methods=['get'], url_path='rapport-par-creneau')
    def rapport_par_creneau(self, request):
        """Distribution des absences par creneau horaire actif.

        Groupage via `suivi.creneau_fk` (FK Creneau). Filtre `creneau.is_actif=True`.
        Filtres : annee_universitaire (requis), departement, date_debut, date_fin.
        Tri : par `creneau.ordre` puis `creneau.creneau`.
        """
        annee = request.query_params.get('annee_universitaire')
        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)

        qs = Presence.objects.filter(
            suivi__annee_universitaire=annee,
            statut__in=[1, 2, 3],
            suivi__creneau_fk__is_actif=True,
        )
        dept_id    = request.query_params.get('departement')
        date_debut = request.query_params.get('date_debut')
        date_fin   = request.query_params.get('date_fin')
        if dept_id:    qs = qs.filter(etudiant__departement_id=dept_id)
        if date_debut: qs = qs.filter(suivi__date_suivie__gte=date_debut)
        if date_fin:   qs = qs.filter(suivi__date_suivie__lte=date_fin)

        data = qs.values(
            'suivi__creneau_fk_id',
            'suivi__creneau_fk__creneau',
            'suivi__creneau_fk__ordre',
            'suivi__creneau_fk__type_creneau',
        ).annotate(
            n=Count('id'),
            n_injustifiees=Count('id', filter=Q(statut=1)),
            n_sanctions=Count('id', filter=Q(statut=2)),
            n_justifiees=Count('id', filter=Q(statut=3)),
        ).order_by('suivi__creneau_fk__ordre', 'suivi__creneau_fk__creneau')

        result = [
            {
                'creneau_id':   d['suivi__creneau_fk_id'],
                'creneau':      d['suivi__creneau_fk__creneau'] or '(inconnu)',
                'type_creneau': d['suivi__creneau_fk__type_creneau'],
                'total':        d['n'],
                'injustifiees': d['n_injustifiees'],
                'sanctions':    d['n_sanctions'],
                'justifiees':   d['n_justifiees'],
            }
            for d in data if d['suivi__creneau_fk_id'] is not None
        ]
        return Response(result)

    @action(detail=False, methods=['get'], url_path='rapport-par-creneau-genre')
    def rapport_par_creneau_genre(self, request):
        """Distribution des absences par creneau actif x genre (M/F).

        Permet d'identifier le creneau ou les filles/garcons s'absentent le plus.
        Filtres : annee_universitaire (requis), departement, date_debut, date_fin.
        Tri : par `creneau.ordre`.
        """
        annee = request.query_params.get('annee_universitaire')
        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)

        qs = Presence.objects.filter(
            suivi__annee_universitaire=annee,
            statut__in=[1, 2, 3],
            suivi__creneau_fk__is_actif=True,
        )
        dept_id    = request.query_params.get('departement')
        date_debut = request.query_params.get('date_debut')
        date_fin   = request.query_params.get('date_fin')
        if dept_id:    qs = qs.filter(etudiant__departement_id=dept_id)
        if date_debut: qs = qs.filter(suivi__date_suivie__gte=date_debut)
        if date_fin:   qs = qs.filter(suivi__date_suivie__lte=date_fin)

        data = qs.values(
            'suivi__creneau_fk_id',
            'suivi__creneau_fk__creneau',
            'suivi__creneau_fk__ordre',
            'etudiant__genre',
        ).annotate(
            n=Count('id'),
        ).order_by('suivi__creneau_fk__ordre', 'suivi__creneau_fk__creneau')

        # Pivot : 1 ligne par creneau, colonnes filles/garcons
        rows = {}
        for d in data:
            cid = d['suivi__creneau_fk_id']
            if cid is None:
                continue
            row = rows.setdefault(cid, {
                'creneau_id': cid,
                'creneau':    d['suivi__creneau_fk__creneau'] or '(inconnu)',
                'ordre':      d['suivi__creneau_fk__ordre'] or 0,
                'filles':     0,
                'garcons':    0,
            })
            if d['etudiant__genre'] == 'F':
                row['filles'] += d['n']
            else:
                row['garcons'] += d['n']

        result = sorted(rows.values(), key=lambda r: (r['ordre'], r['creneau']))
        return Response(result)

    @action(detail=False, methods=['get'], url_path='par-etudiant')
    def par_etudiant(self, request):
        """Historique des présences pour un étudiant — filtres dates + pagination."""
        etudiant_id = request.query_params.get('etudiant')
        if not etudiant_id:
            return Response({'error': 'etudiant requis.'}, status=400)

        qs = Presence.objects.filter(etudiant_id=etudiant_id).select_related(
            'suivi', 'suivi__prof', 'suivi__em',
            'suivi__departement', 'suivi__salle', 'suivi__creneau_fk',
        ).order_by('-suivi__numero_semaine', '-suivi__date_suivie')

        date_debut = request.query_params.get('date_debut')
        date_fin   = request.query_params.get('date_fin')
        if date_debut:
            qs = qs.filter(date_modification__date__gte=date_debut)
        if date_fin:
            qs = qs.filter(date_modification__date__lte=date_fin)

        # Pagination optionnelle (si page_size fourni)
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(PresenceSerializer(page, many=True).data)
        return Response(PresenceSerializer(qs, many=True).data)
    # ── Supprimer le justificatif d'une présence ─────────────────────────────
    @action(detail=True, methods=['delete'], url_path='supprimer-justificatif',
            permission_classes=[RBACPermission])
    def supprimer_justificatif(self, request, pk=None):
        """Supprime physiquement le fichier justificatif et vide le champ."""
        # Gate granulaire : justificatifs (DA principalement)
        _check_abs_module(request.user, 'abs_justificatifs', action='supprimer')
        presence = self.get_object()
        if presence.justificatif:
            presence.justificatif.delete(save=True)  # supprime le fichier + sauvegarde
        else:
            return Response({'message': 'Aucun justificatif à supprimer.'}, status=status.HTTP_200_OK)
        return Response({'message': 'Justificatif supprimé.'}, status=status.HTTP_200_OK)

    # ── Helper PDF ───────────────────────────────────────────────────────────
    @staticmethod
    def _get_institution_context() -> dict:
        """Delegue au helper centralise (core.pdf_utils) pour partager la meme
        source de verite que tous les autres PDFs. Si l'institution principale
        change en BD, tous les PDFs basculent automatiquement."""
        from core.pdf_utils import get_institution_context
        return get_institution_context()

    @staticmethod
    def _build_pdf_context():
        """Retourne (inst_context, seance_map) communs aux vues PDF."""
        from apps.parametres.models import Seance
        seance_map: dict[str, str] = {}
        for s in Seance.objects.values('id', 'type_seance'):
            seance_map[str(s['id'])]     = s['type_seance']
            seance_map[s['type_seance']] = s['type_seance']
        inst_ctx = PresenceViewSet._get_institution_context()
        return inst_ctx, seance_map

    @staticmethod
    def _resolve_absence(p, seance_map: dict) -> dict:
        """Convertit une Presence en dict avec type_seance et créneau résolus (FK uniquement post-Phase-5)."""
        suivi = p.suivi
        type_label = '—'
        if getattr(suivi, 'type_seance_fk_id', None) and suivi.type_seance_fk:
            type_label = suivi.type_seance_fk.type_seance or '—'
        creneau = '—'
        if getattr(suivi, 'creneau_fk_id', None) and suivi.creneau_fk:
            creneau = suivi.creneau_fk.creneau or '—'
        jour = '—'
        if getattr(suivi, 'jour_fk_id', None) and suivi.jour_fk:
            jour = suivi.jour_fk.jour or '—'
        return {
            'statut':      p.statut,
            'commentaire': p.commentaire or '',
            'date':        suivi.date_suivie,
            'creneau':     creneau,
            'jour':        jour,
            'em':          suivi.em.intitule if getattr(suivi, 'em_id', None) else '—',
            'type_seance': type_label,
            'professeur':  suivi.prof.nom if getattr(suivi, 'prof_id', None) else '—',
        }

    @staticmethod
    def _make_pdf(html: str) -> bytes:
        config = pdfkit.configuration(
            wkhtmltopdf=r'C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe'
        )
        options = {
            'page-size':        'A4',
            'orientation':      'Portrait',
            'margin-top':       '1.5cm',
            'margin-bottom':    '1.5cm',
            'margin-left':      '1.2cm',
            'margin-right':     '1.2cm',
            'encoding':         'UTF-8',
            'enable-local-file-access': '',
        }
        return pdfkit.from_string(html, False, configuration=config, options=options)

    # ── Fiches de présence PDF ────────────────────────────────────────────────
    @action(detail=False, methods=['get'], url_path='fiches-pdf',
            permission_classes=[RBACPermission])
    def fiches_pdf(self, request):
        """
        Génère un PDF de fiches de présence.
        Params : annee_universitaire, numero_semaine, departement (opt)
        """
        annee   = request.query_params.get('annee_universitaire')
        semaine = request.query_params.get('numero_semaine')
        dep_id  = request.query_params.get('departement', '')

        if not annee or not semaine:
            return HttpResponse('annee_universitaire et numero_semaine requis.', status=400)

        from apps.suivi.models import Suivie
        from apps.parametres.models import Seance
        from collections import defaultdict

        inst_ctx, seance_map = self._build_pdf_context()

        # Charger les séances de la semaine
        qs = (
            Suivie.objects
            .filter(annee_universitaire=annee, numero_semaine=int(semaine))
            .select_related('prof', 'em', 'salle', 'creneau_fk', 'departement', 'jour_fk', 'type_seance_fk')
            .order_by('jour_fk__jour', 'creneau_fk__creneau')
        )
        if dep_id:
            qs = qs.filter(departement_id=dep_id)

        # Déduplication et résolution des labels (Phase 5 : FK uniquement)
        jours_order = {'Lundi': 1, 'Mardi': 2, 'Mercredi': 3, 'Jeudi': 4, 'Vendredi': 5, 'Samedi': 6}
        def _jour(x):    return x.jour_fk.jour       if x.jour_fk_id    and x.jour_fk    else ''
        def _creneau(x): return x.creneau_fk.creneau if x.creneau_fk_id and x.creneau_fk else ''
        def _type(x):    return x.type_seance_fk.type_seance if x.type_seance_fk_id and x.type_seance_fk else ''

        seen = set()
        suivies_unique = []
        for s in sorted(qs, key=lambda x: (jours_order.get(_jour(x), 9), _creneau(x))):
            # Exclure les séances sans type de séance OU sans EM (lignes
            # incomplètes : Sport, Instruction militaire, lignes vides) :
            # pas de fiche de présence pour ces séances.
            if not _type(s) or not s.em_id:
                continue
            key = f"{_jour(s)}|{_creneau(s)}|{_type(s)}|{getattr(s, 'departement_id', '')}"
            if key in seen:
                continue
            seen.add(key)
            suivies_unique.append(s)

        # Charger les étudiants par département
        dep_ids = {s.departement_id for s in suivies_unique if s.departement_id}
        etudiants_by_dep: dict = defaultdict(list)
        for did in dep_ids:
            etudiants_by_dep[did] = list(
                Etudiant.objects.filter(departement_id=did).order_by('matricule')
                .values('matricule', 'nom', 'genre')
            )

        # Construire les fiches
        from apps.departement.models import Departement as DepModel
        dep_cache: dict = {}

        def get_dep_info(dep_id_val):
            if dep_id_val not in dep_cache:
                try:
                    d = DepModel.objects.select_related('niveau', 'filiere').get(pk=dep_id_val)
                    dep_cache[dep_id_val] = {
                        'nom':     d.nom,
                        'niveau':  d.niveau.niveau if d.niveau_id else '',
                        'filiere': d.filiere.intitule_fr if d.filiere_id else '',
                    }
                except Exception:
                    dep_cache[dep_id_val] = {'nom': str(dep_id_val), 'niveau': '', 'filiere': ''}
            return dep_cache[dep_id_val]

        fiches = []
        for s in suivies_unique:
            type_label = _type(s) or '—'
            raw_creneau = _creneau(s) or '—'
            creneau = raw_creneau.replace('-', ' à ') if raw_creneau and raw_creneau != '—' else '—'
            dep_info = get_dep_info(s.departement_id) if s.departement_id else {'nom': '—', 'niveau': '', 'filiere': ''}
            fiches.append({
                'dep_nom':      dep_info['nom'],
                'niveau':       dep_info['niveau'],
                'filiere':      dep_info['filiere'],
                'date_seance':  s.date_suivie,
                'jour':         _jour(s) or '—',
                'creneau':      creneau,
                'type_seance':  type_label,
                # DS / ER / EF = examen/surveillance → fiche signée par le
                # surveillant (pas le professeur) et sans « Objet du cours ».
                'is_surveillance': type_label in ('DS', 'ER', 'EF'),
                'numero_semaine': s.numero_semaine,
                'em_code':      s.em.code if s.em_id and hasattr(s.em, 'code') else '',
                'em_intitule':  s.em.intitule if s.em_id else '—',
                'prof_nom':     s.prof.nom if s.prof_id else '—',
                'salle_nom':    s.salle.nom if s.salle_id else '—',
                'etudiants':    etudiants_by_dep.get(s.departement_id, []),
            })

        html = render_to_string('absence/fiches_presence.html', {
            'fiches':             fiches,
            'annee_universitaire': annee,
            'numero_semaine':     semaine,
            **inst_ctx,
        })
        pdf      = self._make_pdf(html)
        filename = f"fiches-presence-S{semaine}-{annee}.pdf".replace(' ', '_')
        response = HttpResponse(pdf, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # ── Rapport HTML par département (une page par étudiant) ──────────────────
    @action(detail=False, methods=['get'], url_path='rapport-departement-pdf',
            permission_classes=[RBACPermission])
    def rapport_departement_pdf(self, request):
        """
        Retourne un HTML imprimable (1 page/étudiant) pour un département.
        Params : departement (id), annee_universitaire, date_debut (opt), date_fin (opt)
        """
        annee      = request.query_params.get('annee_universitaire')
        dep_id     = request.query_params.get('departement')
        date_debut = request.query_params.get('date_debut', '')
        date_fin   = request.query_params.get('date_fin', '')

        if not annee or not dep_id:
            return HttpResponse('annee_universitaire et departement requis.', status=400)

        try:
            from apps.parametres.models import Departement
            dep_nom = Departement.objects.get(pk=dep_id).nom
        except Exception:
            dep_nom = f'Département {dep_id}'

        etudiants = Etudiant.objects.filter(departement_id=dep_id).order_by('nom')

        presences_qs = (
            Presence.objects
            .filter(suivi__annee_universitaire=annee, etudiant__departement_id=dep_id, statut__in=[1, 2, 3])
            .select_related('suivi', 'suivi__prof', 'suivi__em', 'suivi__creneau_fk', 'etudiant')
            .order_by('etudiant__nom', 'suivi__date_suivie')
        )
        if date_debut:
            presences_qs = presences_qs.filter(suivi__date_suivie__gte=date_debut)
        if date_fin:
            presences_qs = presences_qs.filter(suivi__date_suivie__lte=date_fin)

        inst_ctx, seance_map = self._build_pdf_context()

        from collections import defaultdict
        by_etu = defaultdict(list)
        for p in presences_qs:
            by_etu[p.etudiant_id].append(p)

        etudiants_data = []
        for etu in etudiants:
            raw_list = by_etu.get(etu.id, [])
            if not raw_list:
                continue
            absences = [self._resolve_absence(p, seance_map) for p in raw_list]
            etudiants_data.append({
                'etudiant': etu,
                'absences': absences,
                'stats': {
                    'total_absences':          len(absences),
                    'absences_non_justifiees': sum(1 for a in absences if a['statut'] == 1),
                    'absences_justifiees':     sum(1 for a in absences if a['statut'] == 3),
                    'sanctions':               sum(1 for a in absences if a['statut'] == 2),
                },
            })

        html = render_to_string('absence/rapport_departement.html', {
            'etudiants_data':      etudiants_data,
            'departement_nom':     dep_nom,
            'annee_universitaire': annee,
            'date_debut':          date_debut,
            'date_fin':            date_fin,
            **inst_ctx,
        })
        pdf      = self._make_pdf(html)
        filename = f"rapport-absences-{dep_nom}-{annee}.pdf".replace(' ', '_')
        response = HttpResponse(pdf, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # ── Rapport HTML par étudiant ─────────────────────────────────────────────
    @action(detail=False, methods=['get'], url_path='rapport-etudiant-pdf',
            permission_classes=[RBACPermission])
    def rapport_etudiant_pdf(self, request):
        """
        Retourne un HTML imprimable pour un étudiant.
        Params : etudiant (id), date_debut (opt), date_fin (opt)
        """
        etudiant_id = request.query_params.get('etudiant')
        date_debut  = request.query_params.get('date_debut', '')
        date_fin    = request.query_params.get('date_fin', '')

        if not etudiant_id:
            return HttpResponse('etudiant requis.', status=400)

        try:
            etudiant = Etudiant.objects.select_related('departement').get(pk=etudiant_id)
        except Etudiant.DoesNotExist:
            return HttpResponse('Étudiant introuvable.', status=404)

        qs = (
            Presence.objects
            .filter(etudiant_id=etudiant_id, statut__in=[1, 2, 3])
            .select_related('suivi', 'suivi__prof', 'suivi__em', 'suivi__creneau_fk')
            .order_by('suivi__date_suivie')
        )
        if date_debut:
            qs = qs.filter(suivi__date_suivie__gte=date_debut)
        if date_fin:
            qs = qs.filter(suivi__date_suivie__lte=date_fin)

        inst_ctx, seance_map = self._build_pdf_context()
        absences_dicts = [self._resolve_absence(p, seance_map) for p in qs]

        stats = {
            'total_absences':          len(absences_dicts),
            'absences_non_justifiees': sum(1 for a in absences_dicts if a['statut'] == 1),
            'absences_justifiees':     sum(1 for a in absences_dicts if a['statut'] == 3),
            'sanctions':               sum(1 for a in absences_dicts if a['statut'] == 2),
        }
        annee = (
            Presence.objects.filter(etudiant_id=etudiant_id)
            .values_list('suivi__annee_universitaire', flat=True).first() or ''
        )

        html = render_to_string('absence/rapport_etudiant.html', {
            'etudiant':            etudiant,
            'absences':            absences_dicts,
            'stats':               stats,
            'annee_universitaire': annee,
            'date_debut':          date_debut,
            'date_fin':            date_fin,
            **inst_ctx,
        })
        pdf      = self._make_pdf(html)
        filename = f"rapport-absences-{etudiant.matricule}-{annee}.pdf".replace(' ', '_')
        response = HttpResponse(pdf, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


class SeuilAbsenceView(generics.RetrieveUpdateAPIView):
    """
    Réglage global du seuil d'alerte des absences.
    """
    queryset = SeuilAbsence.objects.all()
    serializer_class = SeuilAbsenceSerializer
    permission_classes = [RBACPermission]
    required_module = 'abs_saisie'

    def get_object(self):
        obj, _ = SeuilAbsence.objects.get_or_create(pk=1)
        return obj