import uuid

from django.db.models import Count
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.throttling import AnonRateThrottle
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from core.permissions import RBACPermission
from core.mixins import InstitutionScopedMixin
from .utils import creer_inscriptions_pedagogiques as _creer_inscriptions_pedagogiques_util
from .models import (
    Preinscription, InscriptionAdministrative,
    InscriptionPedagogique, InscriptionElement, Derogation,
    GrilleFrais, CandidatBac,
)
from .serializers import (
    PreinscriptionSerializer, PreinscriptionPublicSerializer,
    PreinscriptionCreateSerializer,
    InscriptionAdministrativeSerializer, InscriptionPedagogiqueSerializer,
    InscriptionElementSerializer, DerogationSerializer,
    GrilleFraisSerializer, CandidatBacSerializer,
)


def _creer_inscriptions_pedagogiques(inscription_admin, user):
    """Délègue à inscriptions.utils.creer_inscriptions_pedagogiques."""
    _creer_inscriptions_pedagogiques_util(inscription_admin, user)


def _generer_matricule(annee_obj=None, institution_obj=None, format='auto'):
    """
    Génère un matricule unique selon le format :
        {annee_bac_2digits}{code_etablissement}{seq:0Nd}

    Args:
        annee_obj : Year | None. None → Year active, sinon la plus récente.
        institution_obj : Institution | None. None → Institution principale.
            Refus si > 1 institution principale (ambiguïté multi-institution).
        format : 'auto' | 'historic5' | 'current6'
            - 'historic5' : N=2 → 5 chiffres total (ex `23603` pour 2023-2024)
            - 'current6'  : N=3 → 6 chiffres total (ex `255001` pour 2025-2026)
            - 'auto'      : N=3 par défaut

    La séquence est scopée par institution pour éviter les collisions cross-institution
    en mode multi-institution futur.
    """
    from apps.parametres.models import Year, Institution
    from apps.absence.models import Etudiant

    # Année de bac
    if annee_obj is None:
        annee_obj = (
            Year.objects.filter(est_active=True).order_by('-annee').first()
            or Year.objects.order_by('-annee').first()
        )
    annee_str   = annee_obj.annee if annee_obj else str(__import__('datetime').date.today().year)
    annee_debut = annee_str.split('-')[0].strip() if '-' in annee_str else annee_str
    annee_bac   = annee_debut[-2:]  # ex : '2025' → '25'

    # Institution
    if institution_obj is None:
        principales = list(Institution.objects.filter(est_principale=True))
        if len(principales) > 1:
            raise ValueError(
                f"{len(principales)} institutions principales — passer institution_obj explicitement."
            )
        institution_obj = principales[0] if principales else None
    code_inst = (institution_obj.code_etablissement or '') if institution_obj else ''

    # Largeur de séquence
    seq_width = {'historic5': 2, 'current6': 3, 'auto': 3}.get(format, 3)

    # Séquence anti-collision — scopée par institution (départements de cette institution)
    if institution_obj:
        seq = Etudiant.objects.filter(departement__institution=institution_obj).count() + 1
    else:
        seq = Etudiant.objects.count() + 1

    matricule = f'{annee_bac}{code_inst}{seq:0{seq_width}d}'
    while Etudiant.objects.filter(matricule=matricule).exists():
        seq += 1
        matricule = f'{annee_bac}{code_inst}{seq:0{seq_width}d}'

    return matricule


def _creer_compte_etudiant(etudiant):
    """
    Crée un compte CustomUser pour un étudiant qui n'en a pas encore.
    Username initial = CNI, mot de passe = numéro de bac, doit_changer_mdp = True.
    Idempotent : no-op si un user est déjà lié.
    """
    if etudiant.user_id is not None:
        return  # Compte déjà existant

    cni  = (etudiant.cni or '').strip()
    nbac = (etudiant.nbac or '').strip()
    if not cni or not nbac:
        return  # Données manquantes — pas de compte automatique

    from django.contrib.auth import get_user_model
    User = get_user_model()

    # Éviter les doublons de username
    if User.objects.filter(username=cni).exists():
        # Lier le compte existant si c'est déjà un étudiant sans profil
        existing = User.objects.get(username=cni)
        if existing.role == 'etudiant' and not hasattr(existing, 'etudiant_profile'):
            etudiant.user = existing
            etudiant.save(update_fields=['user'])
        return

    nom_complet = f"{etudiant.prenom_fr or ''} {etudiant.nom_fr or etudiant.nom}".strip()
    email       = f"{etudiant.matricule}@isms.esp.mr"

    new_user = User.objects.create_user(
        username         = cni,
        password         = nbac,
        email            = email,
        name             = nom_complet,
        role             = 'etudiant',
        doit_changer_mdp = True,
    )
    etudiant.user = new_user
    etudiant.save(update_fields=['user'])


class PreinscriptionViewSet(InstitutionScopedMixin, viewsets.ModelViewSet):
    """
    Gestion des pré-inscriptions.
    POST /api/v1/inscriptions/preinscriptions/                      → soumission publique (AllowAny)
    GET  /api/v1/inscriptions/preinscriptions/suivi/?numero_dossier → suivi public (AllowAny)
    GET  /api/v1/inscriptions/preinscriptions/{numero_dossier}/     → détail (authentifié)
    """
    queryset         = Preinscription.objects.select_related('annee_univ', 'filiere', 'examinee_par').all()
    serializer_class = PreinscriptionSerializer
    required_module  = 'insc_administrative'
    parser_classes   = [MultiPartParser, FormParser, JSONParser]
    filter_backends  = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['statut', 'filiere', 'annee_univ']
    search_fields    = ['nom_fr', 'prenom_fr', 'cni', 'email']
    lookup_field          = 'numero_dossier'
    lookup_value_regex    = r'[0-9a-f-]+'

    def get_permissions(self):
        if self.action in ('create', 'suivi'):
            return [AllowAny()]
        return [RBACPermission()]

    def get_throttle_classes(self):
        if self.action in ('create', 'suivi'):
            return [AnonRateThrottle]
        return super().get_throttle_classes()

    def get_serializer_class(self):
        if self.action == 'create':
            return PreinscriptionCreateSerializer
        return PreinscriptionSerializer

    def perform_create(self, serializer):
        serializer.save(statut='soumise')

    @action(detail=False, methods=['get'], url_path='suivi')
    def suivi(self, request):
        """Suivi public : GET ?numero_dossier=<uuid>"""
        numero = request.query_params.get('numero_dossier')
        if not numero:
            return Response({'detail': 'Paramètre numero_dossier requis.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            preinscription = Preinscription.objects.get(numero_dossier=numero)
        except (Preinscription.DoesNotExist, ValueError):
            return Response({'detail': 'Dossier introuvable.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(PreinscriptionPublicSerializer(preinscription).data)

    def _set_statut(self, request, statut, motif_rejet=''):
        """Utilitaire commun pour les transitions de statut."""
        from django.utils import timezone
        instance = self.get_object()
        old_statut = instance.statut
        instance.statut       = statut
        instance.motif_rejet  = motif_rejet
        instance.examinee_par = request.user
        instance.date_examen  = timezone.now()
        instance.save(update_fields=['statut', 'motif_rejet', 'examinee_par', 'date_examen'])
        # Preinscription n'est PAS dans TRACKED_MODELS (core/signals.py) → audit explicite.
        try:
            from core.audit_helpers import write_audit
            write_audit(
                action='UPDATE', model_name='Preinscription', object_id=str(instance.pk),
                changes={'statut': {'old': old_statut, 'new': statut},
                         'motif_rejet': motif_rejet or None},
                label=f'Préinscription #{instance.pk} → {statut}', keep_forever=True,
            )
        except Exception:
            import logging
            logging.getLogger('siga').warning('Audit statut préinscription échoué', exc_info=True)
        return Response(PreinscriptionSerializer(instance).data)

    @action(detail=True, methods=['patch'], url_path='examiner')
    def examiner(self, request, numero_dossier=None):
        statut = request.data.get('statut')
        if statut not in ('acceptee', 'rejetee', 'en_examen'):
            return Response({'detail': 'Statut invalide.'}, status=status.HTTP_400_BAD_REQUEST)
        return self._set_statut(request, statut, request.data.get('motif_rejet', ''))

    @action(detail=True, methods=['post'], url_path='accepter')
    def accepter(self, request, numero_dossier=None):
        return self._set_statut(request, 'acceptee')

    @action(detail=True, methods=['post'], url_path='rejeter')
    def rejeter(self, request, numero_dossier=None):
        motif = request.data.get('motif', '').strip()
        if not motif:
            return Response({'detail': 'Le motif de rejet est obligatoire.'}, status=status.HTTP_400_BAD_REQUEST)
        return self._set_statut(request, 'rejetee', motif)

    @action(detail=True, methods=['post'], url_path='convertir')
    def convertir(self, request, numero_dossier=None):
        """Convertit la pré-inscription en inscription administrative."""
        from apps.parametres.models import Year
        instance = self.get_object()
        if instance.statut != 'acceptee':
            return Response({'detail': 'Seuls les dossiers acceptés peuvent être convertis.'}, status=status.HTTP_400_BAD_REQUEST)
        if not instance.filiere:
            return Response({'detail': 'La filière est requise pour la conversion.'}, status=status.HTTP_400_BAD_REQUEST)

        from apps.absence.models import Etudiant
        import uuid as _uuid
        from apps.departement.models import Departement
        from apps.parametres.models import Niveau

        annee     = instance.annee_univ or Year.objects.order_by('-annee').first()
        matricule = _generer_matricule()

        departement = Departement.objects.filter(filiere=instance.filiere).first()
        if not departement:
            departement = Departement.objects.filter(nom__icontains='scolarite').first()
        if not departement:
            niveau_defaut = Niveau.objects.first()
            if not niveau_defaut:
                niveau_defaut = Niveau.objects.create(niveau='L1')
            departement, _ = Departement.objects.get_or_create(
                nom='Scolarité (import)',
                defaults={'code': 'SCO', 'description': 'Créé automatiquement.', 'niveau': niveau_defaut},
            )

        etudiant, _ = Etudiant.objects.get_or_create(
            matricule=matricule,
            defaults={
                'nom':         instance.nom_fr,
                'nom_fr':      instance.nom_fr,
                'nom_ar':      instance.nom_ar or '',
                'prenom_fr':   instance.prenom_fr,
                'prenom_ar':   instance.prenom_ar or '',
                'email':       instance.email or '',
                'departement': departement,
            },
        )

        from apps.parametres.models import Institution
        principale = Institution.objects.filter(est_principale=True).first()
        numero = f'INS-{annee.annee if annee else "2025"}-{str(_uuid.uuid4())[:6].upper()}'
        insc = InscriptionAdministrative.objects.create(
            etudiant=etudiant, annee_univ=annee, filiere=instance.filiere,
            niveau=1, institution=principale,
            numero_inscription=numero, statut='en_cours', validee_par=request.user,
        )
        instance.statut = 'inscrite'
        instance.save(update_fields=['statut'])
        return Response(InscriptionAdministrativeSerializer(insc).data, status=status.HTTP_201_CREATED)


# ── Inscription administrative ───────────────────────────────────────────────────

class InscriptionAdministrativeViewSet(InstitutionScopedMixin, viewsets.ModelViewSet):
    queryset = InscriptionAdministrative.objects.select_related(
        'etudiant', 'annee_univ', 'filiere', 'validee_par',
    ).all()
    serializer_class   = InscriptionAdministrativeSerializer
    permission_classes = [RBACPermission]
    required_module    = 'insc_administrative'
    filter_backends    = [DjangoFilterBackend, SearchFilter]
    filterset_fields   = ['statut', 'filiere', 'annee_univ', 'est_payee', 'niveau', 'etudiant']
    search_fields      = ['etudiant__matricule', 'etudiant__nom', 'numero_inscription']

    def perform_create(self, serializer):
        numero_genere = f"INS-{uuid.uuid4().hex[:8].upper()}"
        instance = serializer.save(validee_par=self.request.user, numero_inscription=numero_genere)
        # Créer automatiquement le compte étudiant si l'étudiant n'en a pas encore
        _creer_compte_etudiant(instance.etudiant)

    # ── Section 3.4 institution_V1 — Changer un matricule ──────────────────────
    @action(detail=False, methods=['patch'],
            url_path=r'etudiant/(?P<etudiant_id>\d+)/changer-matricule')
    def changer_matricule(self, request, etudiant_id=None):
        """
        PATCH /api/v1/inscriptions/admin/etudiant/<id>/changer-matricule/
        Body: {"nouveau_matricule": "23512", "motif": "min 10 caractères"}

        Réservé admin. Garde-fous :
        - refuse si nouveau == ancien
        - refuse si unicité violée
        - refuse (409) si Progression existe avec matricule différent (incohérence)
        - sinon : update Etudiant + propagation Progression + AuditLog
        """
        from apps.absence.models import Etudiant
        from .models import Progression
        from django.db import transaction
        try:
            from core.models import AuditLog
        except ImportError:
            AuditLog = None

        user = request.user
        if not (user and user.is_authenticated and (
            getattr(user, 'role', '') == 'admin' or user.is_superuser
        )):
            return Response(
                {'detail': 'Reservé administrateur.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        nouveau = (request.data.get('nouveau_matricule') or '').strip()
        motif = (request.data.get('motif') or '').strip()

        if not nouveau:
            return Response({'detail': 'nouveau_matricule requis.'}, status=400)
        if len(motif) < 10:
            return Response({'detail': 'motif requis (min 10 caracteres).'}, status=400)

        try:
            etudiant = Etudiant.objects.get(pk=etudiant_id)
        except Etudiant.DoesNotExist:
            return Response({'detail': 'Etudiant introuvable.'}, status=404)

        ancien = etudiant.matricule
        if ancien == nouveau:
            return Response({'detail': 'Aucun changement.'}, status=400)

        # Unicité
        if Etudiant.objects.filter(matricule=nouveau).exclude(pk=etudiant_id).exists():
            return Response(
                {'detail': f'Matricule {nouveau} deja attribue a un autre etudiant.'},
                status=409,
            )

        # Garde-fou Progression : refuse si snapshot ≠ ancien (incohérence historique)
        progs = Progression.objects.filter(etudiant=etudiant)
        progs_inconsistantes = progs.exclude(matricule=ancien)
        if progs_inconsistantes.exists():
            return Response(
                {
                    'detail': "Progressions historiques avec matricule different — incoherence a resoudre manuellement.",
                    'progressions_ids': list(progs_inconsistantes.values_list('id', flat=True)),
                },
                status=409,
            )

        with transaction.atomic():
            etudiant.matricule = nouveau
            etudiant.save(update_fields=['matricule'])
            nb_propages = progs.update(matricule=nouveau)

            if AuditLog is not None:
                try:
                    AuditLog.objects.create(
                        user=request.user, action='UPDATE',
                        model_name='Etudiant', object_id=str(etudiant.pk),
                        changes={'matricule': {'old': ancien, 'new': nouveau, 'motif': motif}},
                        ip_address=(request.META.get('REMOTE_ADDR') or '')[:45],
                        user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:500],
                    )
                except Exception:
                    pass  # AuditLog ne doit pas faire échouer la transaction

        return Response({
            'matricule_ancien':  ancien,
            'matricule_nouveau': nouveau,
            'progressions_maj':  nb_propages,
            'motif':             motif,
        })



    @action(detail=True, methods=['post'], url_path='payer')
    def payer(self, request, pk=None):
        """
        Enregistre le paiement des frais d'inscription.

        Le montant et le numéro de reçu ne sont PLUS saisis par l'agent :
          - montant  → lu dans la grille tarifaire (institution, année, type_diplome, niveau)
          - reçu     → généré automatiquement (RC-AAAA-NNNNN, thread-safe)
        """
        from django.utils import timezone
        from apps.documents.models import NumeroSerieConfig

        insc = self.get_object()
        if insc.est_payee:
            return Response({'detail': 'Cette inscription est déjà payée.'}, status=status.HTTP_400_BAD_REQUEST)

        montant = GrilleFrais.montant_pour(insc)
        if montant is None:
            return Response(
                {'detail': "Aucun tarif défini pour ce diplôme, ce niveau et cette année. "
                           "Renseignez la grille tarifaire avant d'encaisser."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        config, _ = NumeroSerieConfig.objects.get_or_create(
            institution=insc.institution,
            type_document='recu_inscription',
            defaults={'prefixe': 'REC', 'nb_chiffres': 6},
        )
        recu = config.generer_prochain(avec_annee=False)

        insc.est_payee     = True
        insc.recu_paiement = recu
        insc.montant_frais = montant
        insc.date_paiement = timezone.now().date()
        insc.save(update_fields=['est_payee', 'recu_paiement', 'montant_frais', 'date_paiement'])
        return Response(InscriptionAdministrativeSerializer(insc).data)

    @action(detail=True, methods=['get'], url_path='recu')
    def recu(self, request, pk=None):
        """Reçu de paiement des frais d'inscription en PDF (demi-A4 210×148 mm)."""
        from django.http import HttpResponse
        from .recu_service import generer_recu_pdf

        insc = self.get_object()
        if not insc.est_payee or not insc.recu_paiement:
            return Response(
                {'detail': "Aucun paiement enregistré pour cette inscription."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        agent = (request.user.get_full_name() or '').strip() or request.user.get_username()
        pdf = generer_recu_pdf(insc, agent=agent)
        matricule = getattr(insc.etudiant, 'matricule', '') or ''
        nom_fichier = f'{insc.recu_paiement}_{matricule}.pdf' if matricule else f'{insc.recu_paiement}.pdf'
        resp = HttpResponse(pdf, content_type='application/pdf')
        resp['Content-Disposition'] = f'inline; filename="{nom_fichier}"'
        return resp

    @action(detail=False, methods=['post'], url_path='generer-progression')
    def generer_progression(self, request):
        """
        POST /api/v1/inscriptions/admin/generer-progression/
        Body : { "pv_id": <int> }

        À partir d'un PV clos, génère les InscriptionAdministrative pour N+1 :
          - admis/rachat → niveau+1
          - ajourné      → même niveau, dettes
          - exclus       → Etudiant.statut='exclu'
        """
        from apps.evaluations.models import PVDeliberation
        from apps.inscriptions.services.progression import ProgressionService

        pv_id = request.data.get('pv_id')
        if not pv_id:
            return Response({'detail': 'pv_id est requis.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            pv = PVDeliberation.objects.get(pk=pv_id)
        except PVDeliberation.DoesNotExist:
            return Response({'detail': 'PV introuvable.'}, status=status.HTTP_404_NOT_FOUND)

        if not pv.est_clos:
            return Response(
                {'detail': 'Le PV doit être clos avant de générer la progression.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            svc   = ProgressionService(pv)
            stats = svc.generer()
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(stats, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='importer-mers',
            parser_classes=[MultiPartParser, FormParser])
    def importer_mers(self, request):
        """
        Import en masse depuis un fichier Excel MERS.
        POST /api/v1/inscriptions/admin/importer-mers/
        Params (multipart) : fichier, filiere (int), niveau (int), departement (int)
        """
        return self._import_excel(request, format_matricule='current6')

    @action(detail=False, methods=['post'], url_path='importer-historique',
            parser_classes=[MultiPartParser, FormParser])
    def importer_historique(self, request):
        """
        Section 3.3 institution_V1 — Import d'une cohorte historique.

        POST /api/v1/inscriptions/admin/importer-historique/
        Params (multipart) :
          - fichier     : Excel
          - filiere     : int
          - niveau      : int
          - departement : int
          - annee       : int (Year.id) — obligatoire (vs importer-mers qui prend l'active)
          - institution : int (Institution.id, optionnel — défaut principale)

        Colonnes Excel :
          - Obligatoires : NNI, NOMFR
          - Optionnelles : MATRICULE (explicite ; sinon généré au format historic5)
        """
        return self._import_excel(request, format_matricule='historic5', mode='historique')

    def _import_excel(self, request, format_matricule='current6', mode='mers'):
        import openpyxl
        from decimal import Decimal, InvalidOperation
        from django.db import transaction
        from apps.absence.models import Etudiant
        from apps.scolarite.models import Filiere
        from apps.departement.models import Departement
        from apps.parametres.models import Year, Institution

        fichier    = request.FILES.get('fichier')
        filiere_id = request.data.get('filiere')
        niveau_raw = request.data.get('niveau')
        dept_id    = request.data.get('departement')
        annee_id   = request.data.get('annee')
        inst_id    = request.data.get('institution')

        if not fichier:
            return Response({'detail': 'Le fichier est requis.'}, status=status.HTTP_400_BAD_REQUEST)
        if fichier.size > 5 * 1024 * 1024:
            return Response({'detail': 'Fichier trop volumineux (max 5 Mo).'}, status=status.HTTP_400_BAD_REQUEST)
        if not filiere_id or not niveau_raw or not dept_id:
            return Response({'detail': 'filiere, niveau et departement sont requis.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            filiere     = Filiere.objects.get(pk=filiere_id)
            departement = Departement.objects.get(pk=dept_id)
            niveau_int  = int(niveau_raw)
        except (Filiere.DoesNotExist, Departement.DoesNotExist):
            return Response({'detail': 'Filière ou département introuvable.'}, status=status.HTTP_400_BAD_REQUEST)

        # Résoudre Year
        if mode == 'historique':
            if not annee_id:
                return Response({'detail': 'Le param "annee" est obligatoire en mode historique.'}, status=400)
            try:
                annee = Year.objects.get(pk=annee_id)
            except Year.DoesNotExist:
                return Response({'detail': f'Year #{annee_id} introuvable.'}, status=400)
        else:
            annee = (
                Year.objects.filter(est_active=True).order_by('-annee').first()
                or Year.objects.order_by('-annee').first()
            )
        annee_label = annee.annee if annee else '2025-2026'

        # Résoudre Institution
        if inst_id:
            try:
                institution = Institution.objects.get(pk=inst_id)
            except Institution.DoesNotExist:
                return Response({'detail': f'Institution #{inst_id} introuvable.'}, status=400)
        else:
            principales = list(Institution.objects.filter(est_principale=True))
            if len(principales) != 1:
                return Response(
                    {'detail': f'{len(principales)} institutions principales — passer le param "institution".'},
                    status=400,
                )
            institution = principales[0]

        # Garde-fous institution (Section 3.3 du plan)
        if filiere.institution_id is not None and filiere.institution_id != institution.id:
            return Response(
                {'detail': f'Filière {filiere.code} appartient à institution #{filiere.institution_id} ≠ {institution.id}.'},
                status=400,
            )
        if departement.institution_id is not None and departement.institution_id != institution.id:
            return Response(
                {'detail': f'Département #{departement.id} appartient à institution #{departement.institution_id} ≠ {institution.id}.'},
                status=400,
            )
        if mode == 'historique' and departement.annee_universitaire != annee.annee:
            return Response(
                {'detail': f'Département {departement.id} (annee={departement.annee_universitaire}) incompatible avec {annee.annee}.'},
                status=400,
            )

        try:
            wb = openpyxl.load_workbook(fichier, read_only=True, data_only=True)
            ws = wb.active
        except Exception as e:
            return Response({'detail': f'Fichier Excel invalide : {e}'}, status=status.HTTP_400_BAD_REQUEST)

        if ws.max_row and ws.max_row > 5000:
            return Response({'detail': 'Trop de lignes (max 5000).'}, status=status.HTTP_400_BAD_REQUEST)

        headers  = {}
        first_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if first_row:
            for idx, cell in enumerate(first_row):
                if cell is not None:
                    headers[str(cell).strip().upper()] = idx

        missing = [c for c in ('NNI', 'NOMFR') if c not in headers]
        if missing:
            return Response(
                {'detail': f"Colonnes manquantes : {', '.join(missing)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        def get_col(row, name, raw=False):
            idx = headers.get(name)
            if idx is None or idx >= len(row):
                return None
            v = row[idx]
            if v is None:
                return None
            return v if raw else str(v).strip()

        def parse_date(val):
            if not val:
                return None
            import datetime as _dt
            # openpyxl retourne les cellules date comme datetime ou date directement
            if isinstance(val, _dt.datetime):
                return val.date()
            if isinstance(val, _dt.date):
                return val
            for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y', '%Y-%m-%d %H:%M:%S'):
                try:
                    return _dt.datetime.strptime(str(val).strip(), fmt).date()
                except ValueError:
                    continue
            return None

        def parse_decimal(val):
            if not val:
                return None
            try:
                return Decimal(str(val).replace(',', '.'))
            except InvalidOperation:
                return None

        created_count = 0
        updated_count = 0
        errors        = []

        for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            nni = get_col(row, 'NNI')
            if not nni:
                continue

            matricule_excel = get_col(row, 'MATRICULE') or ''
            insc_admin_created = None
            try:
                with transaction.atomic():
                    defaults = {
                        'nom':               get_col(row, 'NOMFR') or '',
                        'nom_fr':            get_col(row, 'NOMFR') or '',
                        'nom_ar':            get_col(row, 'NOMAR') or '',
                        'lieu_naissance_fr': get_col(row, 'LIEUNFR') or '',
                        'lieu_naissance_ar': get_col(row, 'LIEUNAR') or '',
                        'nationalite_fr':    get_col(row, 'NATIOFR') or 'Mauritanienne',
                        'nationalite_ar':    get_col(row, 'NATIOAR') or 'موريتانية',
                        'genre':             (get_col(row, 'GENRE') or 'M')[0].upper(),
                        'date_naissance':    parse_date(get_col(row, 'DATN', raw=True)),
                        'nbac':              get_col(row, 'NBAC'),
                        'serie_bac':         get_col(row, 'SERIE') or '',
                        'moyenne_bac':       parse_decimal(get_col(row, 'MOYG')),
                        'departement':       departement,
                        'filiere':           filiere,
                    }

                    # Lookup priorité : matricule Excel d'abord, puis CNI
                    etudiant = None
                    if matricule_excel:
                        etudiant = Etudiant.objects.filter(matricule=matricule_excel).first()
                    if etudiant is None:
                        etudiant = Etudiant.objects.filter(cni=nni).first()

                    if etudiant:
                        # Update progressif (ne pas écraser le matricule existant)
                        for k, v in defaults.items():
                            setattr(etudiant, k, v)
                        etudiant.cni = nni
                        etudiant.save()
                        is_new = False
                    else:
                        matricule_final = matricule_excel or _generer_matricule(
                            annee_obj=annee, institution_obj=institution,
                            format=format_matricule,
                        )
                        etudiant = Etudiant.objects.create(
                            cni=nni, matricule=matricule_final, **defaults,
                        )
                        is_new = True

                    if is_new:
                        created_count += 1
                    else:
                        updated_count += 1

                    if annee and not InscriptionAdministrative.objects.filter(
                        etudiant=etudiant, annee_univ=annee
                    ).exists():
                        num_insc = f'INS-{annee_label}-{uuid.uuid4().hex[:6].upper()}'
                        insc_admin_created = InscriptionAdministrative.objects.create(
                            etudiant           = etudiant,
                            annee_univ         = annee,
                            filiere            = filiere,
                            niveau             = niveau_int,
                            institution        = institution,
                            numero_inscription = num_insc,
                            statut             = 'validee' if mode == 'historique' else 'en_cours',
                            est_payee          = (mode == 'historique'),
                            validee_par        = request.user,
                        )

            except Exception as exc:
                errors.append({'row': row_idx, 'nni': nni or '', 'message': str(exc)})

            # Inscriptions pédagogiques : hors transaction pour ne pas
            # annuler l'inscription administrative en cas d'erreur inattendue.
            if insc_admin_created:
                try:
                    _creer_inscriptions_pedagogiques(insc_admin_created, request.user)
                except Exception as exc_ped:
                    errors.append({
                        'row': row_idx, 'nni': nni or '',
                        'message': f'Inscr. pédago. (non bloquant) : {exc_ped}',
                    })

        return Response({
            'created': created_count,
            'updated': updated_count,
            'errors':  errors,
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='inscrire')
    def inscrire(self, request):
        """
        Inscription manuelle complète : crée Etudiant + InscriptionAdministrative.
        POST /api/v1/inscriptions/admin/inscrire/
        """
        from django.db import transaction
        from apps.absence.models import Etudiant
        from apps.scolarite.models import Filiere
        from apps.departement.models import Departement
        from apps.parametres.models import Year

        data = request.data
        cni  = str(data.get('cni', '')).strip()
        if not cni:
            return Response({'detail': 'Le NNI (cni) est obligatoire.'}, status=status.HTTP_400_BAD_REQUEST)

        if Etudiant.objects.filter(cni=cni).exists():
            return Response(
                {'detail': f'Un étudiant avec le NNI {cni} existe déjà.'},
                status=status.HTTP_409_CONFLICT,
            )

        filiere_id = data.get('filiere')
        dept_id    = data.get('departement')
        niveau_raw = data.get('niveau')

        if not filiere_id or not dept_id or not niveau_raw:
            return Response(
                {'detail': 'filiere, departement et niveau sont requis.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            filiere     = Filiere.objects.get(pk=filiere_id)
            departement = Departement.objects.get(pk=dept_id)
            niveau_int  = int(niveau_raw)
        except (Filiere.DoesNotExist, Departement.DoesNotExist, ValueError):
            return Response({'detail': 'Filière ou département introuvable.'}, status=status.HTTP_400_BAD_REQUEST)

        annee       = Year.objects.filter(est_active=True).order_by('-annee').first() or Year.objects.order_by('-annee').first()
        annee_label = annee.annee if annee else '2025-2026'

        # Matricule : prioriser celui fourni dans le payload, sinon generer
        matricule_input = (str(data.get('matricule') or '')).strip()
        if matricule_input:
            if Etudiant.objects.filter(matricule=matricule_input).exists():
                return Response(
                    {'matricule': f'Le matricule {matricule_input} est deja attribue a un autre etudiant.'},
                    status=status.HTTP_409_CONFLICT,
                )
            matricule = matricule_input
        else:
            matricule = _generer_matricule(annee_obj=annee)

        from decimal import Decimal
        def to_dec(v):
            try:
                return Decimal(str(v)) if v is not None else None
            except Exception:
                return None

        try:
            with transaction.atomic():
                # Champs alignes avec les colonnes Excel MERS
                # (NNI, NBAC, NOMFR, NOMAR, DATN, LIEUNFR, LIEUNAR, GENRE,
                #  NATIOFR, NATIOAR, SERIE, MOYG, CODEDEPT, FILIERE)
                etudiant = Etudiant.objects.create(
                    matricule         = matricule,
                    cni               = cni,
                    nom               = data.get('nom_fr', ''),
                    nom_fr            = data.get('nom_fr', ''),
                    nom_ar            = data.get('nom_ar', ''),
                    date_naissance    = data.get('date_naissance') or None,
                    lieu_naissance_fr = data.get('lieu_naissance_fr', ''),
                    lieu_naissance_ar = data.get('lieu_naissance_ar', ''),
                    genre             = (data.get('genre', 'M') or 'M')[0].upper(),
                    nationalite_fr    = data.get('nationalite_fr', 'Mauritanienne'),
                    nationalite_ar    = data.get('nationalite_ar', 'موريتانية'),
                    nbac              = data.get('nbac') or None,
                    serie_bac         = data.get('serie_bac', ''),
                    moyenne_bac       = to_dec(data.get('moyenne_bac')),
                    departement       = departement,
                    filiere           = filiere,
                    statut            = 'actif',
                )
                from apps.parametres.models import Institution
                principale = Institution.objects.filter(est_principale=True).first()
                num_insc    = f'INS-{annee_label}-{uuid.uuid4().hex[:6].upper()}'
                inscription = InscriptionAdministrative.objects.create(
                    etudiant           = etudiant,
                    annee_univ         = annee,
                    filiere            = filiere,
                    niveau             = niveau_int,
                    institution        = principale,
                    numero_inscription = num_insc,
                    statut             = 'en_cours',
                    validee_par        = request.user,
                )
                # Mode "Référentiel BAC" : si l'inscription provient d'un candidat
                # du vivier, le marquer inscrit et le lier au nouvel étudiant
                # (idempotent : le candidat disparaît alors des recherches).
                candidat_bac_id = data.get('candidat_bac')
                if candidat_bac_id:
                    CandidatBac.objects.filter(pk=candidat_bac_id).update(
                        inscrit=True, etudiant=etudiant,
                    )
        except Exception as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # Inscriptions pédagogiques hors transaction : l'étudiant est déjà sauvegardé
        _creer_inscriptions_pedagogiques(inscription, request.user)

        from apps.absence.serializers import EtudiantSerializer
        return Response({
            'etudiant':    EtudiantSerializer(etudiant).data,
            'inscription': InscriptionAdministrativeSerializer(inscription).data,
        }, status=status.HTTP_201_CREATED)


# ── Inscription pédagogique ──────────────────────────────────────────────────────

class InscriptionPedagogiqueViewSet(InstitutionScopedMixin, viewsets.ModelViewSet):
    institution_filter_field = 'inscription_admin__institution'
    queryset = InscriptionPedagogique.objects.select_related(
        'inscription_admin', 'inscription_admin__etudiant', 'semestre', 'validee_par'
    ).annotate(nb_elements=Count('inscriptions_elements')).all()
    serializer_class   = InscriptionPedagogiqueSerializer
    permission_classes = [RBACPermission]
    required_module    = 'insc_pedagogique'
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields   = [
        'semestre', 'est_redoublant',
        'inscription_admin', 'inscription_admin__etudiant',
        'inscription_admin__annee_univ',
        'inscription_admin__annee_univ__annee',
    ]
    search_fields      = [
        'inscription_admin__etudiant__nom',
        'inscription_admin__etudiant__matricule',
        'semestre__code_semestre',
    ]

    def perform_create(self, serializer):
        serializer.save(validee_par=self.request.user)

    @action(detail=True, methods=['get'], url_path='elements')
    def elements(self, request, pk=None):
        """Retourne les InscriptionElement liés à cette inscription pédagogique."""
        insc_ped = self.get_object()
        qs = InscriptionElement.objects.filter(
            inscription_ped=insc_ped
        ).select_related('element', 'annee_dette')
        return Response(InscriptionElementSerializer(qs, many=True).data)

    @action(detail=True, methods=['post'], url_path='ajouter-element')
    def ajouter_element(self, request, pk=None):
        """Ajoute un élément à cette inscription pédagogique.

        Accepte `em` (em.EM, planification — cas réel des données) OU `element`
        (modules.ElementModule, LMD). Sans le support de `em`, le menu d'ajout
        était inopérant (la table LMD ElementModule est vide).
        """
        insc_ped   = self.get_object()
        em_id      = request.data.get('em')
        element_id = request.data.get('element')
        est_dette  = request.data.get('est_dette', False)
        if not em_id and not element_id:
            return Response({'detail': 'em ou element requis.'}, status=status.HTTP_400_BAD_REQUEST)
        lookup = {'em_id': em_id} if em_id else {'element_id': element_id}
        elem, created = InscriptionElement.objects.get_or_create(
            inscription_ped=insc_ped,
            **lookup,
            defaults={'est_dette': est_dette},
        )
        return Response(
            InscriptionElementSerializer(elem).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=['delete'], url_path='retirer-element/(?P<ie_id>[0-9]+)')
    def retirer_element(self, request, pk=None, ie_id=None):
        """Retire un élément (par PK de l'InscriptionElement).

        On supprime par PK et non par element_id : une InscriptionElement peut
        être liée via `em` (element_id=NULL), auquel cas un filtre element_id
        ne matcherait jamais.
        """
        deleted, _ = InscriptionElement.objects.filter(
            inscription_ped_id=pk,
            pk=ie_id,
        ).delete()
        if not deleted:
            return Response({'detail': 'Élément non trouvé.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Inscription élément ──────────────────────────────────────────────────────────

class InscriptionElementViewSet(InstitutionScopedMixin, viewsets.ModelViewSet):
    institution_filter_field = 'inscription_ped__inscription_admin__institution'
    queryset = InscriptionElement.objects.select_related(
        'inscription_ped', 'element', 'annee_dette',
    ).all()
    serializer_class   = InscriptionElementSerializer
    permission_classes = [RBACPermission]
    required_module    = 'insc_pedagogique'
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['est_dette', 'annee_dette', 'inscription_ped']


# ── Dérogations ──────────────────────────────────────────────────────────────────

class DerogationViewSet(InstitutionScopedMixin, viewsets.ModelViewSet):
    """
    CRUD complet sur les dérogations administratives (année blanche, etc.).
    Filtrable par étudiant, année, type, statut.
    Recherche libre sur le matricule et le motif.
    """
    queryset = Derogation.objects.select_related(
        'etudiant', 'annee_univ', 'decide_par',
    ).all()
    serializer_class   = DerogationSerializer
    permission_classes = [RBACPermission]
    required_module    = 'insc_derogation'
    parser_classes     = [MultiPartParser, FormParser, JSONParser]
    filter_backends    = [DjangoFilterBackend, SearchFilter]
    filterset_fields   = ['etudiant', 'annee_univ', 'type_derogation', 'statut']
    search_fields      = ['etudiant__matricule', 'etudiant__nom_fr', 'motif']

    def perform_create(self, serializer):
        serializer.save(decide_par=self.request.user)
        try:
            from core.audit_helpers import write_audit
            d = serializer.instance
            write_audit(
                action='CREATE', model_name='Derogation', object_id=str(d.pk),
                changes={
                    'type_derogation': d.type_derogation,
                    'statut':          d.statut,
                    'etudiant':        str(d.etudiant),
                    'annee':           str(d.annee_univ),
                },
                label=f'Dérogation {d.get_type_derogation_display()} — {d.etudiant}',
                keep_forever=True,
            )
        except Exception:
            import logging
            logging.getLogger('siga').warning('Audit dérogation échoué', exc_info=True)


class GrilleFraisViewSet(InstitutionScopedMixin, viewsets.ModelViewSet):
    """
    CRUD sur la grille tarifaire des frais d'inscription.
    Le montant est fixé par (année, type_diplome, niveau) et alimente
    automatiquement le paiement (plus de saisie manuelle du montant).
    Réutilise le module RBAC 'insc_administrative'.
    """
    queryset = GrilleFrais.objects.select_related('annee_univ', 'institution').all()
    serializer_class   = GrilleFraisSerializer
    permission_classes = [RBACPermission]
    required_module    = 'insc_grille_frais'
    filter_backends    = [DjangoFilterBackend, SearchFilter]
    filterset_fields   = ['annee_univ', 'type_diplome', 'niveau', 'actif']


# ── Référentiel BAC (vivier des bacheliers) ──────────────────────────────────────

# Mapping en-tête Excel -> champ CandidatBac. Centralise et ADAPTABLE : le format
# du fichier officiel du BAC peut varier d'une année à l'autre.
COLONNES_BAC = {
    'NNI':     'nni',
    'NUMBAC':  'num_bac',
    'NOMFR':   'nom_fr',
    'NOMAR':   'nom_ar',
    'DATN':    'date_naissance',
    'LIEUN':   'lieu_naissance',
    'SEXE':    'sexe',
    'SERIE':   'serie',
    'MOYBAC':  'moyenne',
    'MENTION': 'mention',
    'WILAYA':  'wilaya',
}
MAX_LIGNES_BAC = 20000


def _bac_parse_date(v):
    """Dates multi-formats + objets date/datetime natifs -> date | None."""
    import datetime as _dt
    if v is None or v == '':
        return None
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    s = str(v).strip()
    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _bac_parse_decimal(v):
    """Décimal robuste (',' -> '.') -> Decimal | None."""
    from decimal import Decimal, InvalidOperation
    if v is None or v == '':
        return None
    try:
        return Decimal(str(v).strip().replace(',', '.'))
    except (InvalidOperation, ValueError):
        return None


def _bac_normalize_sexe(v):
    """Masculin/H -> M ; Féminin/Femme -> F ; sinon ''."""
    s = str(v or '').strip().upper()
    if not s:
        return ''
    if s in ('M', 'MASCULIN', 'H', 'HOMME'):
        return 'M'
    if s in ('F', 'FÉMININ', 'FEMININ', 'FEMME'):
        return 'F'
    return s[0] if s[0] in ('M', 'F') else ''


class CandidatBacViewSet(InstitutionScopedMixin, viewsets.ReadOnlyModelViewSet):
    """
    Vivier des bacheliers importés (référentiel BAC). Lecture seule :
    - GET /api/v1/inscriptions/candidats-bac/  (liste paginée, recherche NNI/N°BAC/nom)
    - POST /api/v1/inscriptions/candidats-bac/importer/  (import Excel)

    Par défaut, les candidats déjà inscrits sont MASQUÉS (?inscrit=false implicite).
    RBAC : même module que l'inscription administrative.
    """
    queryset = CandidatBac.objects.select_related('annee_univ', 'institution', 'etudiant').all()
    serializer_class   = CandidatBacSerializer
    permission_classes = [RBACPermission]
    required_module    = 'insc_administrative'
    filter_backends    = [DjangoFilterBackend, SearchFilter]
    filterset_fields   = ['annee_univ', 'inscrit']
    search_fields      = ['nni', 'num_bac', 'nom_fr']

    def get_queryset(self):
        qs = super().get_queryset()
        # Par défaut on masque les déjà inscrits, sauf si ?inscrit explicitement fourni.
        if 'inscrit' not in self.request.query_params:
            qs = qs.filter(inscrit=False)
        return qs

    @action(detail=False, methods=['post'], url_path='importer',
            parser_classes=[MultiPartParser, FormParser])
    def importer(self, request):
        """Import du fichier officiel du BAC (.xlsx). Upsert idempotent par num_bac."""
        import openpyxl
        from apps.parametres.models import Year, Institution

        fichier = request.FILES.get('fichier')
        if not fichier:
            return Response({'detail': "Fichier 'fichier' manquant."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            wb = openpyxl.load_workbook(fichier, read_only=True, data_only=True)
        except Exception as exc:
            return Response({'detail': f'Fichier Excel illisible : {exc}'}, status=status.HTTP_400_BAD_REQUEST)
        ws = wb.active

        # En-têtes (1ère ligne, insensible à la casse/espaces)
        first_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if not first_row:
            return Response({'detail': 'Fichier vide.'}, status=status.HTTP_400_BAD_REQUEST)
        entetes = [str(c).strip().upper() if c is not None else '' for c in first_row]
        # index colonne -> champ modele
        col_index = {}
        for idx, ent in enumerate(entetes):
            if ent in COLONNES_BAC:
                col_index[COLONNES_BAC[ent]] = idx
        if 'num_bac' not in col_index and 'nni' not in col_index:
            return Response(
                {'detail': "En-têtes invalides : au moins NUMBAC ou NNI est requis."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        annee = Year.objects.filter(est_active=True).order_by('-annee').first() or Year.objects.order_by('-annee').first()
        if not annee:
            return Response({'detail': "Aucune année universitaire configurée."}, status=status.HTTP_400_BAD_REQUEST)
        institution = Institution.objects.filter(est_principale=True).first()

        def cell(row, champ):
            i = col_index.get(champ)
            return row[i] if (i is not None and i < len(row)) else None

        created = updated = 0
        errors = []
        for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            if row_idx - 1 > MAX_LIGNES_BAC:
                errors.append({'row': row_idx, 'nni': '', 'message': f'Limite de {MAX_LIGNES_BAC} lignes atteinte — reste ignoré.'})
                break
            if row is None or all(c is None or str(c).strip() == '' for c in row):
                continue

            num_bac = str(cell(row, 'num_bac') or '').strip()
            nni     = str(cell(row, 'nni') or '').strip()
            if not num_bac and not nni:
                continue  # ligne sans identifiant -> ignorée
            if not num_bac:
                errors.append({'row': row_idx, 'nni': nni, 'message': 'NUMBAC manquant — ligne ignorée.'})
                continue

            defaults = {
                'institution':    institution,
                'nni':            nni,
                'nom_fr':         str(cell(row, 'nom_fr') or '').strip(),
                'nom_ar':         str(cell(row, 'nom_ar') or '').strip(),
                'date_naissance': _bac_parse_date(cell(row, 'date_naissance')),
                'lieu_naissance': str(cell(row, 'lieu_naissance') or '').strip(),
                'sexe':           _bac_normalize_sexe(cell(row, 'sexe')),
                'serie':          str(cell(row, 'serie') or '').strip(),
                'moyenne':        _bac_parse_decimal(cell(row, 'moyenne')),
                'mention':        str(cell(row, 'mention') or '').strip(),
                'wilaya':         str(cell(row, 'wilaya') or '').strip(),
            }
            try:
                _, was_created = CandidatBac.objects.update_or_create(
                    annee_univ=annee, num_bac=num_bac, defaults=defaults,
                )
                if was_created:
                    created += 1
                else:
                    updated += 1
            except Exception as exc:
                errors.append({'row': row_idx, 'nni': nni, 'message': str(exc)})

        wb.close()
        return Response({
            'created': created,
            'updated': updated,
            'errors':  errors,
            'annee':   annee.annee,
        }, status=status.HTTP_200_OK)
