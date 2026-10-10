import logging
import os
from datetime import date
from collections import defaultdict

from django.db.models import Sum, F, FloatField, ExpressionWrapper
from django.http import HttpResponse
from django.template.loader import get_template
from django.conf import settings
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from core.types_seance import TYPES_SURVEILLANCE
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import OrderingFilter
from core.permissions import RBACPermission, EDTDepartementPermission
from core.mixins import AuditMixin, InstitutionScopedMixin, DepartementScopedMixin
from core.pagination import StandardPagination
from .models import Vacation, Surveillance
from .serializers import (
    VacationSerializer, VacationCreateSerializer,
    SurveillanceSerializer,
)
from core.telechargement import entete_piece_jointe

logger = logging.getLogger('siga')


# ── Utilitaire : année universitaire courante ────────────────────────────────
def _get_annee(request):
    """Retourne l'annee_univ : depuis les query params, sinon depuis le paramètre global."""
    annee = request.query_params.get('annee_univ')
    if annee:
        return annee
    try:
        from apps.parametres.models import Parametres
        return Parametres.objects.first().annee_universitaire
    except Exception:
        pass
    return ''


# ── Utilitaire PDF (même pattern qu'avancement) ───────────────────────────────
def _render_pdf(template_name, context, filename, orientation='Portrait'):
    """Délègue au renderer PDF partagé (core/pdf_renderer)."""
    from core.pdf_renderer import render_pdf_response
    return render_pdf_response(template_name, context, filename, orientation)


def _compute_payement_mensuel(annee, month, year):
    """
    Calcule le montant total à payer par vacataire pour un mois donné.
    Logique identique à GesAFPED payement_pdf :
      - SuiviePointage (Fait) → équiv CM → * taux_CM
      - Vacation CM/TD/TP/PR  → équiv CM → * taux_CM
      - Vacation Surveillance  → * taux_Surveillance
      - Vacation Encadrement   → * taux_Encadrement
      - Vacation Mission       → * taux_Mission
    Retourne uniquement les profs avec montant_total > 0.
    """
    from apps.prof.models import Prof, TYPES_PERMANENTS
    from apps.suivi.models import SuiviePointage
    from apps.parametres.models import Paiement

    def get_taux(t):
        # Même logique que GesAFPED : prend le taux le plus récent, sans filtrer par date
        obj = Paiement.objects.filter(type__iexact=t).order_by('-date_debut').first()
        return obj.taux if obj else 0.0

    taux_CM           = get_taux('CM')
    taux_Surveillance = get_taux('Surveillance')
    taux_Encadrement  = get_taux('Encadrement')
    taux_Mission      = get_taux('Mission')

    # Feature flag : si actif, on inclut les profs PAYES A L'HEURE (vacataire +
    # personnel_admin + personnel_militaire) AU COURS du mois (via prof_type_history).
    # Sinon : snapshot Prof.type courant. Toggle : USE_PROF_TYPE_HISTORY=true dans .env.
    from apps.prof.services import (
        use_prof_type_history, payes_a_lheure_ids_for_month, TYPES_PAYES_A_LHEURE,
    )
    if use_prof_type_history():
        ids = payes_a_lheure_ids_for_month(year, month)
        profs = Prof.objects.filter(id__in=ids).select_related('banque').order_by('nom')
    else:
        profs = Prof.objects.filter(type__in=TYPES_PAYES_A_LHEURE).select_related('banque').order_by('nom')
    data_em = []

    for prof in profs:
        # SuiviePointage (Phase 5 : FK uniquement)
        suivies = SuiviePointage.objects.filter(
            prof_id=prof.id,
            commentaire='Fait',
            date_suivie__month=month,
            date_suivie__year=year,
        ).distinct().select_related('creneau_fk', 'type_seance_fk')
        vacations = Vacation.objects.filter(
            prof_id=prof.id,
            date__month=month,
            date__year=year,
        ).select_related('type')

        if not suivies.exists() and not vacations.exists():
            continue

        # --- SuiviePointage → équiv CM ---
        tot_cm = tot_td = tot_tp = tot_pr = 0.0
        for s in suivies:
            if s.duree_creneau is not None:
                duree = float(s.duree_creneau)
            elif s.creneau_fk_id and s.creneau_fk:
                duree = float(s.creneau_fk.duree or 0)
            else:
                duree = 0.0
            type_s = ''
            if s.type_seance_fk_id and s.type_seance_fk:
                type_s = (s.type_seance_fk.type_seance or '').upper()
            if type_s == 'CM':
                tot_cm += duree
            elif type_s == 'TD':
                tot_td += duree
            elif type_s == 'TP':
                tot_tp += duree
            elif type_s == 'PR':
                tot_pr += duree

        eq_cm_suivis    = tot_cm + (tot_td + tot_tp + tot_pr) * (2 / 3)
        montant_seances = eq_cm_suivis * taux_CM

        # --- Vacation ---
        vac_cm = vac_td = vac_tp = vac_pr = 0.0
        vac_surv = vac_enc = vac_miss = 0.0

        for v in vacations:
            lib_v   = (v.type.type_seance if v.type else '').lower()
            duree_v = float(v.duree or 0)
            if lib_v == 'cm':
                vac_cm += duree_v
            elif lib_v == 'td':
                vac_td += duree_v
            elif lib_v == 'tp':
                vac_tp += duree_v
            elif lib_v == 'pr':
                vac_pr += duree_v
            elif lib_v == 'surveillance':
                vac_surv += duree_v
            elif lib_v == 'encadrement':
                vac_enc += duree_v
            elif lib_v in ('mission', 'missions'):
                vac_miss += duree_v

        eq_cm_vac          = vac_cm + (vac_td + vac_tp + vac_pr) * (2 / 3)
        montant_vac_cm     = eq_cm_vac  * taux_CM
        montant_surv       = vac_surv   * taux_Surveillance
        montant_enc        = vac_enc    * taux_Encadrement
        montant_miss       = vac_miss   * taux_Mission

        montant_total = montant_seances + montant_vac_cm + montant_surv + montant_enc + montant_miss

        if montant_total == 0:
            continue

        # EM liés (depuis suivis)
        from apps.em.models import EM
        em_ids   = [eid for eid in suivies.values_list('em_id', flat=True).distinct() if eid]
        em_names = ', '.join(EM.objects.filter(id__in=em_ids).values_list('code_em', flat=True))

        data_em.append({
            'prof_nom':          prof.nom,
            'numero_de_compte':  prof.numero_de_compte or '',
            'banque_nom':        prof.banque.nom if prof.banque else '',
            'em_names':          em_names,
            'montant_seances':   round(montant_seances, 2),
            'montant_vacation':  round(montant_vac_cm,  2),
            'montant_surv':      round(montant_surv,    2),
            'montant_enc':       round(montant_enc,     2),
            'montant_miss':      round(montant_miss,    2),
            'montant_total':     round(montant_total,   2),
            'detail': {
                'eq_cm_suivis': round(eq_cm_suivis, 2),
                'eq_cm_vac':    round(eq_cm_vac, 2),
                'tot_cm': tot_cm, 'tot_td': tot_td, 'tot_tp': tot_tp, 'tot_pr': tot_pr,
                'vac_cm': vac_cm, 'vac_td': vac_td, 'vac_tp': vac_tp, 'vac_pr': vac_pr,
                'vac_surv': vac_surv, 'vac_enc': vac_enc, 'vac_miss': vac_miss,
            },
        })

    return data_em


def _compute_fiches_mensuelles(annee, month, year):
    """
    Calcule le récapitulatif mensuel pour tous les vacataires.
    Fusionne SuiviePointage (commentaire='Fait') + Vacation pour le mois/année donnés.
    Retourne uniquement les profs dont le total_general > 0.
    """
    from apps.prof.models import Prof
    from apps.suivi.models import SuiviePointage
    from apps.em.models import EM

    # Feature flag prof_type_history : inclut les 3 types payes a l'heure
    # (vacataire + personnel_admin + personnel_militaire) au cours du mois.
    from apps.prof.services import (
        use_prof_type_history, payes_a_lheure_ids_for_month, TYPES_PAYES_A_LHEURE,
    )
    if use_prof_type_history():
        ids = payes_a_lheure_ids_for_month(year, month)
        profs = Prof.objects.filter(id__in=ids).order_by('nom')
    else:
        profs = Prof.objects.filter(type__in=TYPES_PAYES_A_LHEURE).order_by('nom')
    data_em = []

    # Mapping type_seance brut -> bucket type pour les montants
    def _bucket_type(raw_type: str) -> str | None:
        t = (raw_type or '').upper()
        if t in ('CM', 'TD', 'TP', 'PR'): return t
        if t in TYPES_SURVEILLANCE: return 'Surveillance'
        if t == 'ENCADREMENT': return 'Encadrement'
        if t in ('MISSION', 'MISSIONS'): return 'Mission'
        return None

    # Taux applicable au mois affiche : utilise une date au milieu du mois pour
    # interroger Paiement.get_taux_at(). Sert de fallback quand un prof n'a 0
    # heure d'un type donne -> on affiche le taux qui aurait du etre applique.
    from datetime import date as _date
    from apps.parametres.models import Paiement as _Paiement
    try:
        mid_month = _date(int(year), int(month), 15)
    except (TypeError, ValueError):
        mid_month = _date.today()
    taux_fallback_mois = {
        'CM':           _Paiement.get_taux_at('CM',           mid_month) or 0,
        'TD':           _Paiement.get_taux_at('TD',           mid_month) or 0,
        'TP':           _Paiement.get_taux_at('TP',           mid_month) or 0,
        'PR':           _Paiement.get_taux_at('PR',           mid_month) or 0,
        'Surveillance': _Paiement.get_taux_at('Surveillance', mid_month) or 0,
        'Encadrement':  _Paiement.get_taux_at('Encadrement',  mid_month) or 0,
        'Mission':      _Paiement.get_taux_at('Mission',      mid_month) or 0,
    }

    for prof in profs:
        totaux = {
            'CM_total': 0.0, 'TD_total': 0.0, 'TP_total': 0.0, 'PR_total': 0.0,
            'DS_total': 0.0, 'EF_total': 0.0, 'ER_total': 0.0,
            'Surveillance_total': 0.0, 'Encadrement_total': 0.0, 'Mission_total': 0.0,
        }
        # Montants par type calcules depuis le taux_paiement STOCKE de chaque ligne
        # (source de verite paie — coherent avec stats vacations).
        montants_par_type = {
            'CM': 0.0, 'TD': 0.0, 'TP': 0.0, 'PR': 0.0,
            'Surveillance': 0.0, 'Encadrement': 0.0, 'Mission': 0.0,
        }
        montant_net = 0.0   # Cumul duree x taux_paiement stocke par ligne

        # --- SuiviePointage --- (Phase 5 : FK uniquement)
        suivies = SuiviePointage.objects.filter(
            prof_id=prof.id,
            commentaire='Fait',
            date_suivie__month=month,
            date_suivie__year=year,
        ).distinct().select_related('em', 'creneau_fk', 'type_seance_fk')

        for s in suivies:
            if s.duree_creneau is not None:
                duree = float(s.duree_creneau)
            elif s.creneau_fk_id and s.creneau_fk:
                duree = float(s.creneau_fk.duree or 0)
            else:
                duree = 0.0
            type_s = ''
            if s.type_seance_fk_id and s.type_seance_fk:
                type_s = (s.type_seance_fk.type_seance or '').upper()
            key    = f'{type_s}_total'
            if key in totaux:
                totaux[key] += duree
            # Montant : duree x taux_paiement STOCKE de la ligne
            m_line = duree * float(s.taux_paiement or 0)
            montant_net += m_line
            bucket = _bucket_type(type_s)
            if bucket:
                montants_par_type[bucket] += m_line

        # DS/EF/ER → Surveillance
        totaux['Surveillance_total'] += (
            totaux.pop('DS_total', 0) +
            totaux.pop('EF_total', 0) +
            totaux.pop('ER_total', 0)
        )

        # --- Vacation --- (pas de filtre annee : month+year suffisent)
        vacations = Vacation.objects.filter(
            prof_id=prof.id,
            date__month=month,
            date__year=year,
        ).select_related('type')

        for v in vacations:
            lib_v   = (v.type.type_seance if v.type else '').lower()
            duree_v = float(v.duree or 0)
            if lib_v == 'surveillance':
                totaux['Surveillance_total'] += duree_v
            elif lib_v == 'encadrement':
                totaux['Encadrement_total'] += duree_v
            elif lib_v in ('mission', 'missions'):
                totaux['Mission_total'] += duree_v
            elif lib_v == 'cm':
                totaux['CM_total'] += duree_v
            elif lib_v == 'td':
                totaux['TD_total'] += duree_v
            elif lib_v == 'tp':
                totaux['TP_total'] += duree_v
            elif lib_v == 'pr':
                totaux['PR_total'] += duree_v
            # Montant : duree x taux_paiement STOCKE de la vacation
            m_line = duree_v * float(v.taux_paiement or 0)
            montant_net += m_line
            bucket = _bucket_type(v.type.type_seance if v.type else '')
            if bucket:
                montants_par_type[bucket] += m_line

        total_general = sum(totaux.values())

        # Ignorer les profs avec total nul
        if total_general == 0:
            continue

        # EM liés (Phase 5 : FK uniquement)
        em_ids = [eid for eid in suivies.values_list('em_id', flat=True).distinct() if eid]
        em_names = ', '.join(EM.objects.filter(id__in=em_ids).values_list('code_em', flat=True))

        # Taux effectif par type : montant_stocke/heures si le prof a des
        # heures de ce type, sinon le taux applicable au mois affiche.
        # Evite l'affichage trompeur du taux courant pour des mois historiques.
        totaux_map = {
            'CM':           totaux.get('CM_total', 0),
            'TD':           totaux.get('TD_total', 0),
            'TP':           totaux.get('TP_total', 0),
            'PR':           totaux.get('PR_total', 0),
            'Surveillance': totaux.get('Surveillance_total', 0),
            'Encadrement':  totaux.get('Encadrement_total', 0),
            'Mission':      totaux.get('Mission_total', 0),
        }
        taux_effectifs = {}
        for k, heures_k in totaux_map.items():
            if heures_k and heures_k > 0:
                taux_effectifs[k] = round(montants_par_type[k] / heures_k, 2)
            else:
                taux_effectifs[k] = float(taux_fallback_mois.get(k, 0))

        data_em.append({
            'prof_nom':           prof.nom,
            'prof_genre':         prof.genre,
            'em_names':           em_names,
            'totaux':             totaux,
            'total_general':      total_general,
            'montant_net':        round(montant_net, 2),   # Source de verite paie
            'montants_par_type':  {k: round(v, 2) for k, v in montants_par_type.items()},
            'taux_effectifs':     taux_effectifs,
        })

    return data_em


# ══════════════════════════════════════════════════════════════════════════════
# VACATION VIEWSET
# ══════════════════════════════════════════════════════════════════════════════
def _build_attestation_qr(prof, annee, date_debut, date_fin, titre_document, heures_eq_cm, request):
    """Crée (idempotent) l'AttestationTravail vérifiable et son QR pointant vers
    la page publique de vérification. Le QR encode UNIQUEMENT verify_url, et
    verify_url mène TOUJOURS à {DOCUMENTS_BASE_URL}/verifier/{token} — jamais
    l'hôte de la requête (sinon le QR mènerait à un vérificateur inexistant).
    Retourne (numero, qr_data_uri, token_str)."""
    import hashlib
    import base64 as _b64
    from datetime import date as _date, datetime as _datetime
    from apps.documents.models import AttestationTravail
    from apps.parametres.models import Institution

    def _coerce(d):
        if isinstance(d, _datetime):
            return d.date()
        if isinstance(d, _date):
            return d
        if isinstance(d, str) and d:
            try:
                return _datetime.strptime(d[:10], '%Y-%m-%d').date()
            except Exception:
                return None
        return None

    dd, df = _coerce(date_debut), _coerce(date_fin)

    # Numéro STABLE (idempotent) : mêmes (prof, année, dates) → même numéro.
    annee_compact = (annee or '').replace('-', '').replace('/', '')[:9]
    cle = hashlib.md5(f'{dd}|{df}'.encode('utf-8')).hexdigest()[:4].upper()
    numero = f'AT-{annee_compact}-{prof.pk:04d}-{cle}'

    institution = getattr(prof, 'institution', None) \
        or Institution.objects.filter(est_principale=True).first()
    genere_par = request.user if getattr(request, 'user', None) and \
        request.user.is_authenticated else None

    att, _created = AttestationTravail.objects.get_or_create(
        numero=numero,
        defaults={
            'prof':                prof,
            'institution':         institution,
            'titre_document':      titre_document or '',
            'annee_universitaire': annee or '',
            'date_debut':          dd,
            'date_fin':            df,
            'heures_eq_cm':        heures_eq_cm,
            'hash_sha256':         hashlib.sha256(
                                       f'{numero}{prof.NNI}{annee}'.encode('utf-8')).hexdigest(),
            'est_valide':          True,
            'genere_par':          genere_par,
        },
    )

    # ⚠️ TOUJOURS le domaine public de vérification (jamais l'hôte de la requête).
    base_url = (getattr(settings, 'DOCUMENTS_BASE_URL', '') or '').rstrip('/') \
        or 'https://ent.ipgei.mr'
    verify_url = f'{base_url}/verifier/{att.token_verification}'

    qr_data_uri = None
    try:
        import qrcode
        import qrcode.image.svg
        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(verify_url, image_factory=factory, box_size=6, border=2)
        svg_bytes = img.to_string()
        qr_data_uri = f'data:image/svg+xml;base64,{_b64.b64encode(svg_bytes).decode("ascii")}'
    except Exception:
        qr_data_uri = None

    return numero, qr_data_uri, str(att.token_verification)


def _check_vacation_module(user, code, action='modifier'):
    """Helper RBAC pour les @actions sensibles (paiement, validation).
    Admin/superuser bypassent. Sinon raise PermissionDenied si pas autorise."""
    from core.permissions import _has_access
    from rest_framework.exceptions import PermissionDenied
    if user.role == 'admin' or user.is_superuser:
        return
    if not _has_access(user, code, action):
        raise PermissionDenied(f'Vous n\'avez pas le droit "{action}" sur "{code}".')


class VacationViewSet(InstitutionScopedMixin, DepartementScopedMixin, AuditMixin, viewsets.ModelViewSet):
    queryset = Vacation.objects.select_related('prof', 'em', 'type').prefetch_related('departements').all()
    permission_classes = [RBACPermission, EDTDepartementPermission]
    required_module    = 'vac_saisie'
    # Vacation utilise M2M `departements`
    departement_filter_field  = 'departements'
    departement_filter_lookup = 'in'
    # Le payload de création envoie la liste M2M `departements` (et non `departement`)
    # → sans ça, EDTDepartementPermission lit le mauvais champ et refuse toute création.
    departement_payload_field = 'departements'
    # Les vacations transversales (ex. Encadrement) n'ont pas de departement :
    # on les laisse visibles aux utilisateurs scopes, sinon elles disparaissent
    # de la liste pour tout non-superuser.
    departement_scope_include_null = True
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['annee_univ', 'prof', 'type']
    ordering_fields    = ['date', 'prof__nom']
    pagination_class   = StandardPagination

    def get_permissions(self):
        from rest_framework.permissions import IsAuthenticated
        if self.action == 'resume_vacataire':
            return [IsAuthenticated()]
        # Self-service enseignant : télécharger SES fiche + attestation sans
        # droit vac_paiement (auto-scopé à son propre profil dans l'action).
        if self.action in ('pdf_fiches', 'pdf_attestation') and \
           getattr(self.request.user, 'role', None) == 'enseignant':
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return VacationCreateSerializer
        return VacationSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        date_debut = self.request.query_params.get('date_debut')
        date_fin   = self.request.query_params.get('date_fin')
        if date_debut:
            qs = qs.filter(date__gte=date_debut)
        if date_fin:
            qs = qs.filter(date__lte=date_fin)
        return qs

    # ── Résumé par type pour un vacataire ────────────────────────────────────
    @action(detail=False, methods=['get'], url_path='resume-vacataire')
    def resume_vacataire(self, request):
        """
        Résumé par type de séance pour un prof (vacataire),
        avec heures, nombre de séances, taux et montant.
        Accepte : prof, annee_univ, date_debut, date_fin
        """
        annee   = request.query_params.get('annee_univ') or _get_annee(request)

        # Enseignant connecté : forcer son propre prof_id
        if getattr(request.user, 'role', None) == 'enseignant':
            try:
                prof = request.user.prof_profile
            except Exception:
                return Response({'error': 'Profil enseignant introuvable.'}, status=404)
            # Utiliser l'année du contexte si non fournie
            if not annee:
                try:
                    annee = request.user.contexte.annee_universitaire
                except Exception:
                    pass
        else:
            prof_id = request.query_params.get('prof')
            if not prof_id:
                return Response({'error': 'prof requis.'}, status=400)
            from apps.prof.models import Prof
            try:
                prof = Prof.objects.get(pk=prof_id)
            except Prof.DoesNotExist:
                return Response({'error': 'Professeur introuvable.'}, status=404)

        # Tous les types payes a l'heure (vacataire + personnel admin/militaire)
        # ont un resume detaille. Les permanents/contractuels/enseignants militaires
        # ont un salaire fixe -> pas de resume base sur vacations.
        is_vacataire = prof.type.lower() in (
            'vacataire', 'personnel_admin', 'personnel_militaire',
        )

        qs = Vacation.objects.filter(prof_id=prof.pk)
        if annee:
            qs = qs.filter(annee_univ=annee)
        date_debut = request.query_params.get('date_debut')
        date_fin   = request.query_params.get('date_fin')
        if date_debut:
            qs = qs.filter(date__gte=date_debut)
        if date_fin:
            qs = qs.filter(date__lte=date_fin)

        from django.db.models import Count

        TYPES = ['CM', 'TD', 'TP', 'PR', 'Mission', 'Surveillance', 'Encadrement']
        summary = []
        total_heures = 0.0
        total_montant = 0.0

        for t in TYPES:
            qs_t = qs.filter(type__type_seance__iexact=t)
            # Montant calcule sur le taux_paiement FIGE de chaque ligne Vacation
            # (coherent avec etat/stats_mensuelles). On n'utilise plus le tarif
            # courant get_taux_at(), qui faussait le resume des qu'un tarif
            # changeait (resume = h x tarif_du_jour, etat = somme par ligne au
            # taux fige -> divergence).
            agg  = qs_t.aggregate(
                sum_h=Sum('duree'),
                cnt=Count('id'),
                sum_m=Sum(ExpressionWrapper(F('duree') * F('taux_paiement'), output_field=FloatField())),
            )
            h    = float(agg['sum_h'] or 0)
            cnt  = agg['cnt']
            m    = float(agg['sum_m'] or 0.0)
            # Taux effectif (= taux fige si uniforme sur la periode)
            taux = round(m / h, 2) if h else 0.0
            if h > 0:
                summary.append({'type': t, 'heures': h, 'count': cnt, 'taux': taux, 'montant': m})
            total_heures  += h
            total_montant += m

        return Response({
            'is_vacataire':    is_vacataire,
            'prof_type':       prof.type,
            'prof_nom':        prof.nom,
            'summary':         summary,
            'total_heures':    total_heures,
            'total_montant':   total_montant,
        })

    @action(detail=False, methods=['get'], url_path='etat')
    def etat(self, request):
        """État de paiement mensuel des vacataires."""
        annee = request.query_params.get('annee_univ')
        if not annee:
            return Response({'error': 'annee_univ requis.'}, status=400)
        qs = Vacation.objects.filter(annee_univ=annee)
        dept_id = request.query_params.get('departement')
        if dept_id:
            qs = qs.filter(departements__id=dept_id)
        data = qs.values('prof__nom', 'prof__id', 'type__type_seance').annotate(
            total_heures=Sum('duree'),
            total_montant=Sum(ExpressionWrapper(F('duree') * F('taux_paiement'), output_field=FloatField())),
        ).order_by('prof__nom')
        return Response(list(data))

    @action(detail=False, methods=['get'], url_path='stats-mensuelles')
    def stats_mensuelles(self, request):
        annee = request.query_params.get('annee_univ')
        if not annee:
            return Response({'error': 'annee_univ requis.'}, status=400)
        from django.db.models.functions import TruncMonth
        data = Vacation.objects.filter(annee_univ=annee).annotate(
            mois=TruncMonth('date')
        ).values('mois').annotate(
            total=Sum(ExpressionWrapper(F('duree') * F('taux_paiement'), output_field=FloatField())),
            nb_profs=Sum('prof_id')
        ).order_by('mois')
        return Response(list(data))

    @action(detail=False, methods=['get'], url_path='fiches-individuelles')
    def fiches_individuelles(self, request):
        annee   = request.query_params.get('annee_univ')
        prof_id = request.query_params.get('prof')
        if not annee or not prof_id:
            return Response({'error': 'annee_univ et prof requis.'}, status=400)
        qs = Vacation.objects.filter(
            annee_univ=annee, prof_id=prof_id
        ).select_related('prof', 'em', 'type').prefetch_related('departements').order_by('date')
        return Response(VacationSerializer(qs, many=True).data)

    # ── PDF : Fiche individuelle ──────────────────────────────────────────────
    @action(detail=False, methods=['get'], url_path='pdf-fiches')
    def pdf_fiches(self, request):
        annee   = request.query_params.get('annee_univ')
        prof_id = request.query_params.get('prof')

        # Enseignant connecté : auto-scope sur son propre profil (avant le
        # contrôle 400 : il ne passe ni prof ni forcément annee).
        if getattr(request.user, 'role', None) == 'enseignant':
            try:
                prof_id = request.user.prof_profile.pk
            except Exception:
                return Response({'error': 'Profil enseignant introuvable.'}, status=404)
            if not annee:
                try:
                    annee = request.user.contexte.annee_universitaire
                except Exception:
                    pass

        if not annee or not prof_id:
            return Response({'error': 'annee_univ et prof requis.'}, status=400)

        qs = Vacation.objects.filter(
            annee_univ=annee, prof_id=prof_id
        ).select_related('prof', 'em', 'type').prefetch_related('departements').order_by('date')

        if not qs.exists():
            return Response({'error': 'Aucune vacation pour ce professeur.'}, status=404)

        rows = VacationSerializer(qs, many=True).data
        total_heures  = sum(float(r['duree']   or 0) for r in rows)
        total_montant = sum(float(r['montant'] or 0) for r in rows)

        # Récapitulatif par type
        by_type = defaultdict(lambda: {'heures': 0.0, 'montant': 0.0})
        for r in rows:
            t = r.get('type_label') or 'Autre'
            by_type[t]['heures']  += float(r['duree']   or 0)
            by_type[t]['montant'] += float(r['montant'] or 0)

        prof_nom = rows[0]['prof_nom'] if rows else f'Prof #{prof_id}'

        # Titre du semestre (parité) pour l'en-tête du PDF :
        # ?type_semestre=P/I, sinon le contexte de l'utilisateur, sinon ''.
        ts_param = request.query_params.get('type_semestre')
        if ts_param == 'P':
            titre_semestre = 'Pairs'
        elif ts_param == 'I':
            titre_semestre = 'Impairs'
        else:
            titre_semestre = ''
            try:
                sem = request.user.contexte.semestre
                if sem in ('Pairs', 'Impairs'):
                    titre_semestre = sem
            except Exception:
                pass

        context = {
            'rows':           rows,
            'by_type':        dict(by_type),
            'prof_nom':       prof_nom,
            'annee_univ':     annee,
            'total_heures':   total_heures,
            'total_montant':  total_montant,
            'titre_semestre': titre_semestre,
        }
        filename = f"fiche_vacation_{prof_nom}_{annee}.pdf".replace(' ', '_').replace('/', '-')
        return _render_pdf('vacation_fiches_pdf.html', context, filename, orientation='Portrait')

    # ── JSON : État de paiement mensuel ──────────────────────────────────────
    @action(detail=False, methods=['get'], url_path='etat-paiement')
    def etat_paiement(self, request):
        annee = _get_annee(request)
        mois  = request.query_params.get('mois')
        if not mois:
            return Response({'error': 'mois requis (format YYYY-MM).'}, status=400)
        try:
            from datetime import datetime as _dt
            d = _dt.strptime(mois, '%Y-%m')
            month, year = d.month, d.year
        except ValueError:
            return Response({'error': "Format de mois invalide (attendu : YYYY-MM)."}, status=400)
        data = _compute_payement_mensuel(annee, month, year)
        return Response(data)

    # ── PDF : État de paiement mensuel (avec montants + banque) ─────────────
    @action(detail=False, methods=['get'], url_path='pdf-etat-paiement')
    def pdf_etat_paiement(self, request):
        annee = _get_annee(request)
        mois  = request.query_params.get('mois')
        if not mois:
            return Response({'error': 'mois requis (format YYYY-MM).'}, status=400)
        try:
            from datetime import datetime as _dt
            d = _dt.strptime(mois, '%Y-%m')
            month, year = d.month, d.year
            MONTHS_FR = {1:'janvier',2:'février',3:'mars',4:'avril',5:'mai',6:'juin',
                         7:'juillet',8:'août',9:'septembre',10:'octobre',11:'novembre',12:'décembre'}
            mois_vacation = f"{MONTHS_FR.get(month, '')} {year}"
        except ValueError:
            return Response({'error': "Format de mois invalide (attendu : YYYY-MM)."}, status=400)

        data_em = _compute_payement_mensuel(annee, month, year)
        if not data_em:
            return Response({'error': f'Aucune vacation trouvée pour {mois_vacation}.'}, status=404)

        montant_global = sum(d['montant_total'] for d in data_em)
        context = {
            'data_em':       data_em,
            'mois_vacation': mois_vacation,
            'annee_univ':    annee,
            'montant_global': montant_global,
        }
        filename = f"etat_paiement_{mois_vacation.replace(' ', '_')}.pdf"
        return _render_pdf('vacation_etat_paiement_pdf.html', context, filename, orientation='Portrait')

    # ── Excel : État de paiement mensuel ─────────────────────────────────────
    @action(detail=False, methods=['get'], url_path='excel-etat-paiement')
    def excel_etat_paiement(self, request):
        annee = _get_annee(request)
        mois  = request.query_params.get('mois')
        if not mois:
            return Response({'error': 'mois requis (format YYYY-MM).'}, status=400)
        try:
            from datetime import datetime as _dt
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
            d = _dt.strptime(mois, '%Y-%m')
            month, year = d.month, d.year
            MONTHS_FR = {1:'janvier',2:'février',3:'mars',4:'avril',5:'mai',6:'juin',
                         7:'juillet',8:'août',9:'septembre',10:'octobre',11:'novembre',12:'décembre'}
            mois_vacation = f"{MONTHS_FR.get(month, '')} {year}"
        except ValueError:
            return Response({'error': "Format de mois invalide (attendu : YYYY-MM)."}, status=400)

        data_em = _compute_payement_mensuel(annee, month, year)
        if not data_em:
            return Response({'error': f'Aucune vacation trouvée pour {mois_vacation}.'}, status=404)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = mois_vacation

        # Styles
        header_font  = Font(bold=True, color='FFFFFF', size=11)
        header_fill  = PatternFill('solid', fgColor='006633')
        total_fill   = PatternFill('solid', fgColor='FFD966')
        total_font   = Font(bold=True, size=12)
        center       = Alignment(horizontal='center', vertical='center')
        left         = Alignment(horizontal='left',   vertical='center')
        thin         = Side(style='thin')
        border       = Border(left=thin, right=thin, top=thin, bottom=thin)

        # Titre
        ws.merge_cells('A1:E1')
        title_cell = ws['A1']
        title_cell.value     = f'État de vacation — {mois_vacation}'
        title_cell.font      = Font(bold=True, size=13)
        title_cell.alignment = center
        ws.row_dimensions[1].height = 22

        # En-tête colonnes
        headers = ['Enseignant vacataire', 'Somme perçue (MRU)', 'Compte bancaire', 'Banque', 'Émargement']
        for ci, h in enumerate(headers, 1):
            cell           = ws.cell(row=2, column=ci, value=h)
            cell.font      = header_font
            cell.fill      = header_fill
            cell.alignment = center
            cell.border    = border
        ws.row_dimensions[2].height = 18

        # Données
        for ri, item in enumerate(data_em, 3):
            row_data = [
                item['prof_nom'],
                item['montant_total'],
                item['numero_de_compte'] or '',
                item['banque_nom'] or '',
                '',
            ]
            for ci, val in enumerate(row_data, 1):
                cell           = ws.cell(row=ri, column=ci, value=val)
                cell.alignment = left if ci == 1 else center
                cell.border    = border
                if ci == 2:
                    cell.number_format = '#,##0.00'

        # Total
        total_row = len(data_em) + 3
        montant_global = sum(d['montant_total'] for d in data_em)
        total_cells = [
            (1, 'TOTAL'),
            (2, montant_global),
            (3, ''), (4, ''), (5, ''),
        ]
        for ci, val in total_cells:
            cell           = ws.cell(row=total_row, column=ci, value=val)
            cell.font      = total_font
            cell.fill      = total_fill
            cell.alignment = center
            cell.border    = border
            if ci == 2:
                cell.number_format = '#,##0.00'
        ws.row_dimensions[total_row].height = 20

        # Largeurs colonnes
        ws.column_dimensions['A'].width = 35
        ws.column_dimensions['B'].width = 20
        ws.column_dimensions['C'].width = 22
        ws.column_dimensions['D'].width = 20
        ws.column_dimensions['E'].width = 18

        import io
        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)

        filename = f"etat_paiement_{mois_vacation.replace(' ', '_')}.xlsx"
        response = HttpResponse(
            buffer.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = entete_piece_jointe(filename)
        return response

    # ── PDF : État de vacation ────────────────────────────────────────────────
    @action(detail=False, methods=['get'], url_path='pdf-etat')
    def pdf_etat(self, request):
        annee   = request.query_params.get('annee_univ')
        dept_id = request.query_params.get('departement')
        if not annee:
            return Response({'error': 'annee_univ requis.'}, status=400)

        qs = Vacation.objects.filter(annee_univ=annee)
        if dept_id:
            qs = qs.filter(departements__id=dept_id)

        rows = list(qs.values('prof__nom', 'prof__id', 'type__type_seance').annotate(
            total_heures=Sum('duree'),
            total_montant=Sum(ExpressionWrapper(F('duree') * F('taux_paiement'), output_field=FloatField())),
        ).order_by('prof__nom'))

        if not rows:
            return Response({'error': 'Aucune donnée.'}, status=404)

        # Grouper par prof
        profs_dict = defaultdict(lambda: {'nom': '', 'rows': [], 'total_heures': 0.0, 'total_montant': 0.0})
        for r in rows:
            pid = r['prof__id']
            profs_dict[pid]['nom'] = r['prof__nom'] or '—'
            profs_dict[pid]['rows'].append(r)
            profs_dict[pid]['total_heures']  += float(r['total_heures']  or 0)
            profs_dict[pid]['total_montant'] += float(r['total_montant'] or 0)
        profs = list(profs_dict.values())

        total_heures  = sum(r['total_heures']  or 0 for r in rows)
        total_montant = sum(r['total_montant'] or 0 for r in rows)

        dept_nom = ''
        if dept_id:
            from apps.departement.models import Departement
            try:
                dept_nom = Departement.objects.get(pk=dept_id).nom
            except Departement.DoesNotExist:
                pass

        context = {
            'profs':          profs,
            'annee_univ':     annee,
            'departement_nom': dept_nom,
            'total_heures':   total_heures,
            'total_montant':  total_montant,
        }
        filename = f"etat_vacation_{annee}.pdf".replace(' ', '_').replace('/', '-')
        return _render_pdf('vacation_etat_pdf.html', context, filename, orientation='Landscape')

    # ── PDF : Détails de vacation ─────────────────────────────────────────────
    @action(detail=False, methods=['get'], url_path='pdf-details')
    def pdf_details(self, request):
        annee   = request.query_params.get('annee_univ')
        prof_id = request.query_params.get('prof')
        if not annee:
            return Response({'error': 'annee_univ requis.'}, status=400)

        qs = Vacation.objects.filter(annee_univ=annee)
        if prof_id:
            qs = qs.filter(prof_id=prof_id)
        qs = qs.select_related('prof', 'em', 'type').prefetch_related('departements').order_by('prof__nom', 'date')

        if not qs.exists():
            return Response({'error': 'Aucune donnée.'}, status=404)

        rows = VacationSerializer(qs, many=True).data

        # Grouper par prof
        profs_dict = defaultdict(lambda: {'nom': '', 'rows': [], 'total_heures': 0.0, 'total_montant': 0.0})
        for r in rows:
            key = r['prof_nom']
            profs_dict[key]['nom'] = r['prof_nom']
            profs_dict[key]['rows'].append(r)
            profs_dict[key]['total_heures']  += float(r['duree']   or 0)
            profs_dict[key]['total_montant'] += float(r['montant'] or 0)
        profs = list(profs_dict.values())

        total_heures  = sum(float(r['duree']   or 0) for r in rows)
        total_montant = sum(float(r['montant'] or 0) for r in rows)

        context = {
            'profs':          profs,
            'annee_univ':     annee,
            'total_heures':   total_heures,
            'total_montant':  total_montant,
        }
        filename = f"details_vacation_{annee}.pdf".replace(' ', '_').replace('/', '-')
        return _render_pdf('vacation_details_pdf.html', context, filename, orientation='Landscape')

    # ── Fiches mensuelles (JSON) ─────────────────────────────────────────────
    @action(detail=False, methods=['get'], url_path='fiches-mensuelles')
    def fiches_mensuelles(self, request):
        """Récapitulatif mensuel de toutes les vacations pour tous les vacataires."""
        annee = _get_annee(request)
        mois  = request.query_params.get('mois')       # format YYYY-MM
        if not mois:
            return Response({'error': 'mois requis (format YYYY-MM).'}, status=400)
        try:
            from datetime import datetime as _dt
            d = _dt.strptime(mois, '%Y-%m')
            month, year = d.month, d.year
        except ValueError:
            return Response({'error': "Format de mois invalide (attendu : YYYY-MM)."}, status=400)

        data = _compute_fiches_mensuelles(annee, month, year)
        return Response(data)

    # ── PDF : Fiches mensuelles ───────────────────────────────────────────────
    @action(detail=False, methods=['get'], url_path='pdf-fiches-mensuelles')
    def pdf_fiches_mensuelles(self, request):
        """PDF : une page par vacataire pour le mois donné."""
        annee = _get_annee(request)
        mois  = request.query_params.get('mois')
        if not mois:
            return Response({'error': 'mois requis (format YYYY-MM).'}, status=400)
        try:
            from datetime import datetime as _dt
            d = _dt.strptime(mois, '%Y-%m')
            month, year = d.month, d.year
            MONTHS_FR = {1:'janvier',2:'février',3:'mars',4:'avril',5:'mai',6:'juin',
                         7:'juillet',8:'août',9:'septembre',10:'octobre',11:'novembre',12:'décembre'}
            mois_vacation = f"{MONTHS_FR[month]} {year}"
        except ValueError:
            return Response({'error': "Format de mois invalide (attendu : YYYY-MM)."}, status=400)

        data_em = _compute_fiches_mensuelles(annee, month, year)
        if not data_em:
            return Response({'error': f'Aucune vacation trouvée pour {mois_vacation}.'}, status=404)

        from apps.parametres.models import Paiement as _Paiement
        def _get_taux(t):
            obj = _Paiement.objects.filter(type__iexact=t).first()
            return obj.taux if obj else 0

        taux_CM   = _get_taux('CM')
        taux_TD   = _get_taux('TD')
        taux_TP   = _get_taux('TP')
        taux_PR   = _get_taux('PR')
        taux_surv = _get_taux('Surveillance')
        taux_enc  = _get_taux('Encadrement')
        taux_miss = _get_taux('Mission')

        montant_global = 0.0
        for item in data_em:
            t = item['totaux']
            mpt = item.get('montants_par_type') or {}
            # Priorite aux montants par type calcules depuis le taux_paiement
            # STOCKE (source paie). Fallback taux_actuel * heures si absent.
            m_cm   = mpt.get('CM',           taux_CM   * t.get('CM_total', 0))
            m_td   = mpt.get('TD',           taux_TD   * t.get('TD_total', 0))
            m_tp   = mpt.get('TP',           taux_TP   * t.get('TP_total', 0))
            m_pr   = mpt.get('PR',           taux_PR   * t.get('PR_total', 0))
            m_surv = mpt.get('Surveillance', taux_surv * t.get('Surveillance_total', 0))
            m_enc  = mpt.get('Encadrement',  taux_enc  * t.get('Encadrement_total', 0))
            m_miss = mpt.get('Mission',      taux_miss * t.get('Mission_total', 0))
            item['montants'] = {
                'CM': m_cm, 'TD': m_td, 'TP': m_tp, 'PR': m_pr,
                'Surveillance': m_surv, 'Encadrement': m_enc, 'Mission': m_miss,
            }
            # taux_effectifs deja calcule par _compute_fiches_mensuelles
            # (utilise taux_fallback_mois si heures=0 pour le type donne).
            net = item.get('montant_net') if item.get('montant_net') is not None else (
                m_cm + m_td + m_tp + m_pr + m_surv + m_enc + m_miss
            )
            item['net_a_payer'] = net
            montant_global += net

        # Le template ne consomme plus les `taux_de_X` globaux : chaque ligne
        # affiche `item.taux_effectifs.X` (calcule par _compute_fiches_mensuelles
        # via taux_paiement stocke ou Paiement.get_taux_at(mid_month) en fallback).
        # On laisse seulement les variables strictement utilisees par le template.
        context = {
            'data_em':             data_em,
            'mois_vacation':       mois_vacation,
            'annee_univ':          annee,
            'annee_universitaire': annee,
            'montant_global':      montant_global,
        }
        filename = f"details_paiement_{mois_vacation.replace(' ', '_')}.pdf"
        return _render_pdf('vacation_fiches_mensuelles_pdf.html', context, filename, orientation='Landscape')

    # ── Excel : Fiches mensuelles (meme structure que le PDF details) ────────
    @action(detail=False, methods=['get'], url_path='excel-fiches-mensuelles')
    def excel_fiches_mensuelles(self, request):
        """Excel : equivalent du PDF /pdf-fiches-mensuelles/ — 1 ligne par
        vacataire avec Taux/Nombre/Montant pour chaque type de seance."""
        annee = _get_annee(request)
        mois  = request.query_params.get('mois')
        if not mois:
            return Response({'error': 'mois requis (format YYYY-MM).'}, status=400)
        try:
            from datetime import datetime as _dt
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
            d = _dt.strptime(mois, '%Y-%m')
            month, year = d.month, d.year
            MONTHS_FR = {1:'janvier',2:'février',3:'mars',4:'avril',5:'mai',6:'juin',
                         7:'juillet',8:'août',9:'septembre',10:'octobre',11:'novembre',12:'décembre'}
            mois_vacation = f"{MONTHS_FR.get(month, '')} {year}"
        except ValueError:
            return Response({'error': "Format de mois invalide (attendu : YYYY-MM)."}, status=400)

        data_em = _compute_fiches_mensuelles(annee, month, year)
        if not data_em:
            return Response({'error': f'Aucune vacation trouvée pour {mois_vacation}.'}, status=404)

        from apps.parametres.models import Paiement as _Paiement
        def _get_taux(t):
            obj = _Paiement.objects.filter(type__iexact=t).first()
            return float(obj.taux) if obj else 0.0

        taux = {
            'CM':            _get_taux('CM'),
            'TD':            _get_taux('TD'),
            'TP':            _get_taux('TP'),
            'PR':            _get_taux('PR'),
            'Surveillance':  _get_taux('Surveillance'),
            'Encadrement':   _get_taux('Encadrement'),
            'Mission':       _get_taux('Mission'),
        }

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = mois_vacation[:31]  # max 31 chars

        # Styles
        header_font  = Font(bold=True, color='FFFFFF', size=11)
        header_fill  = PatternFill('solid', fgColor='006633')
        sub_font     = Font(bold=True, size=10)
        sub_fill     = PatternFill('solid', fgColor='F2F2F2')
        total_fill   = PatternFill('solid', fgColor='006633')
        total_font   = Font(bold=True, size=12, color='FFFFFF')
        center       = Alignment(horizontal='center', vertical='center', wrap_text=True)
        left         = Alignment(horizontal='left',   vertical='center')
        thin         = Side(style='thin')
        border       = Border(left=thin, right=thin, top=thin, bottom=thin)

        # Titre
        last_col = 23  # A=Nom + 7*3 colonnes + 1 net = 23 colonnes
        ws.cell(row=1, column=1, value=f'Etat de vacation — {mois_vacation}')
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
        ws['A1'].font = Font(bold=True, size=13)
        ws['A1'].alignment = center
        ws.row_dimensions[1].height = 22

        # En-tete ligne 1 : groupes par categorie
        cat_groups = ['Cours', 'TD', 'TP', 'PR', 'Surveillance', 'Encadrement', 'Missions']
        ws.cell(row=2, column=1, value='Nom')
        ws.merge_cells(start_row=2, start_column=1, end_row=3, end_column=1)
        ws['A2'].font = header_font
        ws['A2'].fill = header_fill
        ws['A2'].alignment = center
        ws['A2'].border = border

        col = 2
        for cat in cat_groups:
            ws.cell(row=2, column=col, value=cat)
            ws.merge_cells(start_row=2, start_column=col, end_row=2, end_column=col+2)
            for c in range(col, col+3):
                cell = ws.cell(row=2, column=c)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = center
                cell.border = border
            col += 3

        # Net a payer (rowspan)
        ws.cell(row=2, column=last_col, value='Net à payer')
        ws.merge_cells(start_row=2, start_column=last_col, end_row=3, end_column=last_col)
        ws.cell(row=2, column=last_col).font = header_font
        ws.cell(row=2, column=last_col).fill = header_fill
        ws.cell(row=2, column=last_col).alignment = center
        ws.cell(row=2, column=last_col).border = border

        # En-tete ligne 2 : Taux / Nombre / Montant
        sub_headers = ['Taux', 'Nombre', 'Montant']
        col = 2
        for _ in cat_groups:
            for sh in sub_headers:
                cell = ws.cell(row=3, column=col, value=sh)
                cell.font = sub_font
                cell.fill = sub_fill
                cell.alignment = center
                cell.border = border
                col += 1

        ws.row_dimensions[2].height = 20
        ws.row_dimensions[3].height = 16

        # Donnees
        cat_keys = [
            ('CM',           'CM_total'),
            ('TD',           'TD_total'),
            ('TP',           'TP_total'),
            ('PR',           'PR_total'),
            ('Surveillance', 'Surveillance_total'),
            ('Encadrement',  'Encadrement_total'),
            ('Mission',      'Mission_total'),
        ]
        montant_global = 0.0
        start_data_row = 4
        for ri, item in enumerate(data_em, start_data_row):
            ws.cell(row=ri, column=1, value=item['prof_nom']).alignment = left
            ws.cell(row=ri, column=1).border = border
            mpt = item.get('montants_par_type') or {}
            col = 2
            for tkey, dkey in cat_keys:
                n = float(item['totaux'].get(dkey, 0) or 0)
                # Priorite au montant calcule depuis le taux_paiement stocke
                m = float(mpt.get(tkey, taux[tkey] * n))
                # Taux effectif affiche = montant / heures (moyenne ponderee),
                # sinon taux actuel en fallback (cas 0 heure ou montant absent).
                t_eff = (m / n) if n > 0 else taux[tkey]
                for ci, val, fmt in [
                    (col,     t_eff, '#,##0'),
                    (col + 1, n,     '0.0'),
                    (col + 2, m,     '#,##0'),
                ]:
                    cell = ws.cell(row=ri, column=ci, value=val)
                    cell.alignment = center
                    cell.border = border
                    cell.number_format = fmt
                col += 3
            net_final = item.get('montant_net') if item.get('montant_net') is not None else sum(
                float(mpt.get(tkey, taux[tkey] * float(item['totaux'].get(dkey, 0) or 0)))
                for tkey, dkey in cat_keys
            )
            cell = ws.cell(row=ri, column=last_col, value=net_final)
            cell.font = Font(bold=True)
            cell.alignment = center
            cell.border = border
            cell.number_format = '#,##0'
            montant_global += net_final

        # Ligne TOTAL
        total_row = start_data_row + len(data_em)
        ws.cell(row=total_row, column=1, value='TOTAL')
        ws.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=last_col - 1)
        for c in range(1, last_col):
            cell = ws.cell(row=total_row, column=c)
            cell.font = total_font
            cell.fill = total_fill
            cell.alignment = center
            cell.border = border
        cell = ws.cell(row=total_row, column=last_col, value=montant_global)
        cell.font = total_font
        cell.fill = total_fill
        cell.alignment = center
        cell.border = border
        cell.number_format = '#,##0'
        ws.row_dimensions[total_row].height = 22

        # Largeurs
        ws.column_dimensions['A'].width = 28
        for c in range(2, last_col):
            ws.column_dimensions[openpyxl.utils.get_column_letter(c)].width = 9
        ws.column_dimensions[openpyxl.utils.get_column_letter(last_col)].width = 14

        # Figer la 1ere colonne et les 3 lignes d'en-tete
        ws.freeze_panes = 'B4'

        import io
        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)

        filename = f"details_paiement_{mois_vacation.replace(' ', '_')}.xlsx"
        response = HttpResponse(
            buffer.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = entete_piece_jointe(filename)
        return response

    # ── PDF : Fiches individuelles (1 page par vacataire) ─────────────────────
    @action(detail=False, methods=['get'], url_path='pdf-fiches-individuelles')
    def pdf_fiches_individuelles(self, request):
        """PDF : une page recapitulative par vacataire (sans montants).
        Utilise le template vacation_fiches_individuelles_pdf.html.
        Respecte prof_type_history via _compute_fiches_mensuelles."""
        annee = _get_annee(request)
        mois  = request.query_params.get('mois')
        if not mois:
            return Response({'error': 'mois requis (format YYYY-MM).'}, status=400)
        try:
            from datetime import datetime as _dt
            d = _dt.strptime(mois, '%Y-%m')
            month, year = d.month, d.year
            MONTHS_FR = {1:'janvier',2:'février',3:'mars',4:'avril',5:'mai',6:'juin',
                         7:'juillet',8:'août',9:'septembre',10:'octobre',11:'novembre',12:'décembre'}
            mois_vacation = f"{MONTHS_FR[month]} {year}"
        except ValueError:
            return Response({'error': "Format de mois invalide (attendu : YYYY-MM)."}, status=400)

        data_em = _compute_fiches_mensuelles(annee, month, year)
        if not data_em:
            return Response({'error': f'Aucune vacation trouvée pour {mois_vacation}.'}, status=404)

        context = {
            'data_em':              data_em,
            'mois_vacation':        mois_vacation,
            'annee_universitaire':  annee,
        }
        filename = f"fiches_recap_{mois_vacation.replace(' ', '_')}.pdf"
        return _render_pdf('vacation_fiches_individuelles_pdf.html', context, filename, orientation='Portrait')

    # ── PDF : Attestation (même logique que GesAFPED) ─────────────────────────
    def _build_attestation_context(self, request, prof, annee, filiere_id=None,
                                    date_debut=None, date_fin=None):
        """Construit le contexte de l'attestation (utilise par pdf_attestation
        ET par attestation_info pour le precheck cote frontend).

        Retourne un dict {context_pdf, error, status_code}.
        - context_pdf : context pret pour le template, ou None si erreur
        - error : message d'erreur user-friendly
        - status_code : 200 / 400 / 404
        """
        from apps.suivi.models import SuiviePointage
        from apps.prof.models import TYPES_PERMANENTS
        from decimal import Decimal

        # Ponderation des heures de seances pour l'attestation :
        #   1 h TD = 2/3 h eq CM
        #   1 h TP = 2/3 h eq CM
        #   1 h PR = 2/3 h eq CM (assimile au TD/TP cote remuneration)
        # /!\ Cette ponderation est SPECIFIQUE au calcul d'equivalence horaire
        # pour les attestations de vacation. NE PAS la confondre avec
        # ParametresPonderation (coeff_cc/coeff_tp), qui regit le calcul des
        # notes (evaluations) et n'a aucun rapport avec les heures de vacation.
        coeff_td_tp = 2 / 3

        # ── Source 1 : SuiviePointage (séances "Fait") ────────────────────────
        suivies_qs = SuiviePointage.objects.filter(
            prof_id=prof.pk,
            annee_universitaire=annee,
            commentaire='Fait',
        )
        if date_debut:
            suivies_qs = suivies_qs.filter(date_suivie__gte=date_debut)
        if date_fin:
            suivies_qs = suivies_qs.filter(date_suivie__lte=date_fin)
        suivies = suivies_qs.select_related(
            'em', 'em__filiere', 'em__semestre', 'em__semestre__niveau_semestre',
            'em__module_lmd', 'em__module_lmd__filiere',
            'em__departement', 'em__departement__filiere',
            'type_seance_fk',
        ).prefetch_related('departements')

        # ── Source 2 : Vacations ───────────────────────────────────────────────
        # Cas normal (enseignants) : EM obligatoire — on ne compte que l'enseignement
        # Cas personnel admin/militaire : on accepte AUSSI les vacations sans EM
        # (Surveillance, Encadrement, Mission — typiques de leur activite)
        # Cas permanent (exception) : on accepte aussi les vacations Encadrement
        # sans EM (comptees comme CM par la suite, rattachees a une filiere).
        PERSONNEL_SERVICE = ('personnel_admin', 'personnel_militaire')
        is_personnel = (prof.type or '').lower() in PERSONNEL_SERVICE
        is_permanent_local = (prof.type or '').lower() in TYPES_PERMANENTS

        vacations_qs = Vacation.objects.filter(
            prof_id=prof.pk,
            annee_univ=annee,
        )
        if is_personnel:
            pass  # garde TOUTES les vacations (avec ou sans EM)
        elif is_permanent_local:
            from django.db.models import Q
            # Permanent : EM obligatoire SAUF pour les Encadrements (vacations
            # sans EM mais qui representent un enseignement, comptees comme CM)
            vacations_qs = vacations_qs.filter(
                Q(em__isnull=False) | Q(em__isnull=True, type__type_seance='Encadrement')
            )
        else:
            from django.db.models import Q
            # Vacataire / contractuel / militaire : EM obligatoire SAUF Encadrement.
            # L'encadrement (PFE/stage/mémoire) n'a pas d'EM mais doit figurer sur
            # l'attestation — il est classé en ligne "service" (cf. boucle Source 2,
            # `elif em_obj is None` -> heures_service). Sans cette exception, les
            # heures d'encadrement d'un vacataire etaient silencieusement ignorees.
            vacations_qs = vacations_qs.filter(
                Q(em__isnull=False) | Q(em__isnull=True, type__type_seance='Encadrement')
            )
        if date_debut:
            vacations_qs = vacations_qs.filter(date__gte=date_debut)
        if date_fin:
            vacations_qs = vacations_qs.filter(date__lte=date_fin)
        vacations = vacations_qs.select_related(
            'type', 'em', 'em__filiere', 'em__semestre', 'em__semestre__niveau_semestre',
            'em__module_lmd', 'em__module_lmd__filiere',
            'em__departement', 'em__departement__filiere',
        ).prefetch_related('departements')

        # ── Filtre filiere (chaine stable via module_lmd, sinon departement)
        if filiere_id:
            from django.db.models import Q
            f_filter = (
                Q(em__filiere_id=filiere_id)
                | Q(em__module_lmd__filiere_id=filiere_id)
                | Q(em__departement__filiere_id=filiere_id)
            )
            suivies = suivies.filter(f_filter)
            vacations = vacations.filter(f_filter)

        if not suivies.exists() and not vacations.exists():
            return {
                'context_pdf': None,
                'error': f'Aucune séance trouvée pour {prof.nom} — {annee}.',
                'status_code': 404,
            }

        # ══ Cas PERSONNEL admin/militaire : tableau simplifie par TYPE d'activite
        # (Surveillance, Mission, Encadrement) — pas de CM/TD/TP ni de filiere/niveau.
        if is_personnel:
            services_dict       = {}   # cle = type_seance ('Surveillance', 'Mission', ...)
            depts_codes_globaux = set()
            filieres_personnel  = set()   # filieres derivees des departements

            def _accumuler_dept(d):
                """Stocke nom du dept + filiere stable derive du Departement annuel."""
                if d.nom:
                    depts_codes_globaux.add(d.nom)
                # Departement.filiere -> Filiere stable
                if getattr(d, 'filiere_id', None) and getattr(d, 'filiere', None):
                    fil_nom = (d.filiere.intitule_fr or '').strip()
                    if fil_nom:
                        filieres_personnel.add(fil_nom)

            # Source 1 : SuiviePointage (rare pour personnel mais possible)
            for s in suivies:
                type_s = (s.type_seance_fk.type_seance if s.type_seance_fk else '').strip() or 'Service'
                duree  = float(s.duree_creneau or 0)
                if duree <= 0:
                    continue
                if type_s not in services_dict:
                    services_dict[type_s] = {'type_activite': type_s, 'nb_seances': 0, 'total_heures': 0.0}
                services_dict[type_s]['nb_seances']  += 1
                services_dict[type_s]['total_heures'] += duree
                for d in s.departements.all():
                    _accumuler_dept(d)
            # Source 2 : Vacations (avec ou sans EM)
            for v in vacations:
                type_s = (v.type.type_seance if v.type else '').strip() or 'Service'
                duree  = float(v.duree or 0)
                if duree <= 0:
                    continue
                if type_s not in services_dict:
                    services_dict[type_s] = {'type_activite': type_s, 'nb_seances': 0, 'total_heures': 0.0}
                services_dict[type_s]['nb_seances']  += 1
                services_dict[type_s]['total_heures'] += duree
                for d in v.departements.all():
                    _accumuler_dept(d)

            # Civilité + qualité + titre
            if prof.genre == 'F':
                civilite, interesse = 'Madame', "l'intéressée"
            else:
                civilite, interesse = 'Monsieur', "l'intéressé"
            # Militaire : on s'adresse par le grade (Capitaine, Commandant…)
            # au lieu de Monsieur/Madame.
            if prof.type in ('militaire', 'personnel_militaire') and prof.grade:
                civilite = prof.grade
            if prof.type == 'personnel_admin':
                qualite = 'Personnel administratif'
            elif prof.type == 'personnel_militaire':
                qualite = 'Personnel militaire'
            else:
                qualite = prof.type
            titre_document = 'Attestation de Service Fait'

            services_list      = sorted(services_dict.values(), key=lambda x: x['type_activite'])
            grand_total_heures = sum(s['total_heures'] for s in services_list)

            # Texte d'introduction
            types_tries = [s['type_activite'] for s in services_list]
            if not types_tries:
                texte_types = 'son service'
            elif len(types_tries) == 1:
                texte_types = f"des activités de {types_tries[0].lower()}"
            elif len(types_tries) == 2:
                texte_types = f"des activités de {types_tries[0].lower()} et de {types_tries[1].lower()}"
            else:
                texte_types = ('des activités de '
                               + ', '.join(t.lower() for t in types_tries[:-1])
                               + f", et de {types_tries[-1].lower()}")

            # Texte départements (utilisé en fallback si pas de filiere)
            descriptions_depts = sorted(depts_codes_globaux)
            if not descriptions_depts:
                texte_depts_description = 'divers départements'
            elif len(descriptions_depts) == 1:
                texte_depts_description = descriptions_depts[0]
            elif len(descriptions_depts) == 2:
                texte_depts_description = f"{descriptions_depts[0]} et {descriptions_depts[1]}"
            else:
                texte_depts_description = (', '.join(descriptions_depts[:-1])
                                           + f" et {descriptions_depts[-1]}")

            # Texte filieres (prefere aux departements dans l'attestation)
            descriptions_filieres = sorted(filieres_personnel)
            if not descriptions_filieres:
                texte_filieres_description = ''
            elif len(descriptions_filieres) == 1:
                texte_filieres_description = descriptions_filieres[0]
            elif len(descriptions_filieres) == 2:
                texte_filieres_description = f"{descriptions_filieres[0]} et {descriptions_filieres[1]}"
            else:
                texte_filieres_description = (', '.join(descriptions_filieres[:-1])
                                              + f" et {descriptions_filieres[-1]}")

            # Numéro + QR vérifiable (service fait → total des heures)
            numero_attestation, qr_data_uri, _tok = _build_attestation_qr(
                prof, annee, date_debut, date_fin, titre_document, grand_total_heures, request,
            )

            context_service = {
                'prof':                       prof,
                'civilite':                   civilite,
                'interesse':                  interesse,
                'qualite':                    qualite,
                'afficher_nni':               True,
                'titre_document':             titre_document,
                'numero_attestation':         numero_attestation,
                'qr_data_uri':                qr_data_uri,
                'annee_universitaire':        annee,
                'is_service_fait':            True,
                'services_list':              services_list,
                'grand_total_heures':         grand_total_heures,
                'texte_types':                texte_types,
                'texte_depts_description':    texte_depts_description,
                'texte_filieres_description': texte_filieres_description,
                'filieres_globales':          sorted(filieres_personnel),
                'date_debut_periode':         str(date_debut) if date_debut else '',
                'date_fin_periode':           str(date_fin) if date_fin else '',
                'lieu':                       'Nouakchott',
                'date_generation':            date.today().strftime('%d/%m/%Y'),
            }
            return {
                'context_pdf': context_service,
                'meta': {
                    'nb_modules':         len(services_list),
                    'total_heures_cm':    0.0,
                    'total_heures_td':    0.0,
                    'total_heures_tp':    0.0,
                    'grand_total_ponde':  grand_total_heures,
                    'nb_filieres':        0,
                    'nb_departements':    len(depts_codes_globaux),
                    'numero_attestation': numero_attestation,
                },
                'error':       None,
                'status_code': 200,
            }

        # ══ Cas ENSEIGNANT (vacataire/permanent/contractuel/militaire) ════════
        # Fusion dans modules_dict — Clé : (em_id, code_semestre)
        COEFF                 = coeff_td_tp
        modules_dict          = {}
        types_seances_globaux = set()
        depts_codes_globaux   = set()
        filieres_globales     = set()
        niveaux_globaux       = set()

        def _filiere_niveau_de_em(em_obj):
            """Helper local : extrait filiere_nom + niveau_label d'un EM via la chaine
            STABLE (filiere directe -> module LMD), sinon repli departement annuel
            (désormais vestigial/NULL)."""
            if not em_obj:
                return ('', '')
            fil_nom = ''
            # 1. Identité stable de l'EM : filiere directe
            if em_obj.filiere_id and em_obj.filiere:
                fil_nom = em_obj.filiere.intitule_fr or ''
            # 2. Chaine stable : module_lmd -> filiere
            if not fil_nom and em_obj.module_lmd_id and em_obj.module_lmd:
                if em_obj.module_lmd.filiere_id and em_obj.module_lmd.filiere:
                    fil_nom = em_obj.module_lmd.filiere.intitule_fr or ''
            # 3. Repli : departement annuel -> filiere (peut être NULL)
            if not fil_nom and em_obj.departement_id and em_obj.departement:
                if em_obj.departement.filiere_id and em_obj.departement.filiere:
                    fil_nom = em_obj.departement.filiere.intitule_fr or ''
            # Niveau via semestre -> niveau_semestre
            niv = ''
            if em_obj.semestre_id and em_obj.semestre:
                if em_obj.semestre.niveau_semestre_id and em_obj.semestre.niveau_semestre:
                    niv = em_obj.semestre.niveau_semestre.niveau or ''
            return (fil_nom, niv)

        # ── SOURCE 1 : SuiviePointage ─────────────────────────────────────────
        for s in suivies:
            if not s.em_id:
                continue
            em_obj        = s.em
            code_semestre = em_obj.semestre.code_semestre if (em_obj and em_obj.semestre) else '—'
            filiere_nom, niveau_label = _filiere_niveau_de_em(em_obj)
            if filiere_nom: filieres_globales.add(filiere_nom)
            if niveau_label: niveaux_globaux.add(niveau_label)

            # Départements (M2M Phase 5)
            dept_set = {d.nom for d in s.departements.all() if d.nom}
            for d in dept_set:
                depts_codes_globaux.add(d)

            key = (str(s.em_id), code_semestre)
            if key not in modules_dict:
                modules_dict[key] = {
                    'intitule':       em_obj.intitule if em_obj else str(s.em_id),
                    'id_semestre':    code_semestre,
                    'filiere':        filiere_nom,
                    'niveau':         niveau_label,
                    'heures_cm':      0.0,
                    'heures_td':      0.0,
                    'heures_tp':      0.0,
                    'heures_service': 0.0,
                    'departements':   set(dept_set),
                    'is_service':     False,
                }
            else:
                modules_dict[key]['departements'].update(dept_set)

            duree  = float(s.duree_creneau or 0)
            type_s = ''
            if s.type_seance_fk_id and s.type_seance_fk:
                type_s = (s.type_seance_fk.type_seance or '').upper()
            if type_s == 'CM':
                modules_dict[key]['heures_cm'] += duree
                types_seances_globaux.add('CM')
            elif type_s in ('TD', 'PR'):
                modules_dict[key]['heures_td'] += duree
                types_seances_globaux.add('TD')
            elif type_s == 'TP':
                modules_dict[key]['heures_tp'] += duree
                types_seances_globaux.add('TP')

        # ── SOURCE 2 : Vacation ───────────────────────────────────────────────
        # L'Encadrement (vacation sans EM) est une activite d'enseignement :
        # il compte comme CM et se rattache a une filiere derivee des departements.
        # Affichage UNIFORME pour tous les types de prof (permanent ET vacataire /
        # contractuel / militaire) : une ligne "Encadrement" fusionnee, comptee en CM.

        def _filiere_niveau_de_depts(dept_qs):
            """Derive (filiere_nom, niveau_label) depuis le premier dept ayant
            une filiere parmi les departements de la vacation."""
            fil_nom = ''
            niv_lab = ''
            for d in dept_qs:
                if not fil_nom and d.filiere_id and d.filiere:
                    fil_nom = d.filiere.intitule_fr or ''
                if not niv_lab and d.niveau_id and d.niveau:
                    niv_lab = d.niveau.niveau or ''
                if fil_nom and niv_lab:
                    break
            return (fil_nom, niv_lab)

        for v in vacations:
            em_obj        = v.em
            type_s_raw    = v.type.type_seance if v.type else ''
            type_s        = type_s_raw.upper()
            dept_list     = list(v.departements.all())

            # Encadrement (sans EM) -> traite comme CM et regroupe par filiere
            # derivee des departements. Pour TOUS les types de prof (idem permanent).
            encadrement_cm = (em_obj is None and type_s == 'ENCADREMENT')

            if em_obj:
                # Cas standard : vacation rattachee a un EM (enseignement)
                code_semestre = em_obj.semestre.code_semestre if em_obj.semestre else '—'
                filiere_nom, niveau_label = _filiere_niveau_de_em(em_obj)
                key      = (str(em_obj.id), code_semestre)
                intitule = em_obj.intitule
            elif encadrement_cm:
                # Encadrement -> regroupe en UNE seule ligne "Encadrement" quelles que
                # soient les filieres des departements derivees. La filiere de la ligne
                # agrege les filieres distinctes trouvees via les departements
                # (jointes par ' / ' dans le post-traitement).
                code_semestre = '—'
                filiere_nom, niveau_label = _filiere_niveau_de_depts(dept_list)
                key           = ('_ENCADREMENT_', '—')
                intitule      = 'Encadrement'
            else:
                # Cas personnel/non-permanent sans EM (Surveillance, Mission, Encadrement vacataire)
                # On agrege par TYPE de seance — l'attestation montrera une ligne
                # 'Surveillance' / 'Mission' / etc. avec total heures.
                code_semestre = '—'
                filiere_nom   = ''
                niveau_label  = ''
                key           = (f'_NOEM_{type_s_raw}', '—')
                intitule      = type_s_raw or 'Service'

            if filiere_nom: filieres_globales.add(filiere_nom)
            if niveau_label: niveaux_globaux.add(niveau_label)
            dept_noms = {d.nom for d in dept_list}
            for d in dept_noms:
                depts_codes_globaux.add(d)

            if key not in modules_dict:
                modules_dict[key] = {
                    'intitule':              intitule,
                    'id_semestre':           code_semestre,
                    'filiere':               filiere_nom,
                    '_filieres_set':         {filiere_nom} if filiere_nom else set(),
                    'niveau':                niveau_label,
                    'heures_cm':             0.0,
                    'heures_td':             0.0,
                    'heures_tp':             0.0,
                    'heures_service':        0.0,  # surveillance / mission / encadrement
                    'departements':          set(dept_noms),
                    'is_service':            em_obj is None and not encadrement_cm,
                    'is_encadrement_perm':   encadrement_cm,
                }
            else:
                modules_dict[key]['departements'].update(dept_noms)
                # Pour l'Encadrement permanent fusionne : agrege les filieres
                # derivees des differents departements rencontres.
                if filiere_nom:
                    modules_dict[key].setdefault('_filieres_set', set()).add(filiere_nom)

            duree = float(v.duree or 0)
            if type_s == 'CM' or encadrement_cm:
                # Encadrement = compte comme CM (1:1, pondere par 1), tous types de prof
                modules_dict[key]['heures_cm'] += duree
                types_seances_globaux.add('CM')
            elif type_s in ('TD', 'PR'):
                modules_dict[key]['heures_td'] += duree
                types_seances_globaux.add('TD')
            elif type_s == 'TP':
                modules_dict[key]['heures_tp'] += duree
                types_seances_globaux.add('TP')
            elif em_obj is None:
                # Surveillance / Mission / Encadrement (non-permanent) -> service
                modules_dict[key]['heures_service'] += duree
                types_seances_globaux.add('SERVICE')

        # ── Construire la liste finale avec pondération ───────────────────────
        modules_list      = []
        grand_total_ponde = 0.0

        for m in modules_dict.values():
            # Service (Surveillance/Mission/Encadrement) compte 1:1 (pas de coeff)
            total_pondere = (
                m['heures_cm']      * 1 +
                m['heures_td']      * COEFF +
                m['heures_tp']      * COEFF +
                m.get('heures_service', 0.0) * 1
            )
            m['total_pondere']       = total_pondere
            m['departement_affiche'] = ' / '.join(sorted(m['departements'])) or '—'
            # Si des filieres ont ete agregees (cas Encadrement permanent
            # fusionne avec departements de filieres diverses), on prefere
            # leur jointure plutot que la 1ere filiere rencontree (qui peut
            # etre vide si la 1ere vacation n'avait pas de dept avec filiere).
            fset = m.pop('_filieres_set', set())
            if fset:
                m['filiere'] = ' / '.join(sorted(fset))
            grand_total_ponde       += total_pondere
            modules_list.append(m)

        modules_list.sort(key=lambda x: x['id_semestre'])

        # ── Texte dynamique des types de séances ──────────────────────────────
        labels = {
            'CM':      'des Cours Magistraux (CM)',
            'TD':      'des Travaux Dirigés (TD)',
            'TP':      'des Travaux Pratiques (TP)',
            'SERVICE': "des activités de service (surveillance d'examens, encadrement, missions)",
        }
        ordre        = ['CM', 'TD', 'TP', 'SERVICE']
        types_tries  = [t for t in ordre if t in types_seances_globaux]
        labels_tries = [labels[t] for t in types_tries]
        if not labels_tries:
            texte_types = 'son service'
        elif len(labels_tries) == 1:
            texte_types = labels_tries[0]
        elif len(labels_tries) == 2:
            texte_types = f"{labels_tries[0]} et {labels_tries[1]}"
        else:
            texte_types = ', '.join(labels_tries[:-1]) + f" et {labels_tries[-1]}"

        # ── Texte des départements ────────────────────────────────────────────
        descriptions_depts = sorted(depts_codes_globaux)
        if not descriptions_depts:
            texte_depts_description = 'divers départements'
        elif len(descriptions_depts) == 1:
            texte_depts_description = descriptions_depts[0]
        elif len(descriptions_depts) == 2:
            texte_depts_description = f"{descriptions_depts[0]} et {descriptions_depts[1]}"
        else:
            texte_depts_description = (', '.join(descriptions_depts[:-1])
                                       + f" et {descriptions_depts[-1]}")

        # ── Variables selon genre / type du professeur ────────────────────────
        if prof.genre == 'F':
            civilite  = 'Madame'
            interesse = "l'intéressée"
        else:
            civilite  = 'Monsieur'
            interesse = "l'intéressé"
        # Militaire : on s'adresse par le grade (Capitaine, Commandant…) au lieu
        # de Monsieur/Madame.
        if prof.type in ('militaire', 'personnel_militaire') and prof.grade:
            civilite = prof.grade

        if prof.type == 'vacataire':
            titre_document = "Attestation d'Enseignement"
            afficher_nni   = True
            qualite        = 'Vacataire'
        elif prof.type in TYPES_PERMANENTS:
            # Un agrege ou un technologue EST un permanent : meme document,
            # avec son grade propre en qualite (Agrege, Technologue...).
            titre_document = 'Attestation de Service Fait'
            afficher_nni   = False
            qualite        = prof.grade if prof.grade else 'Permanent'
        elif prof.type == 'contractuel':
            titre_document = 'Attestation de Service Fait'
            afficher_nni   = False
            qualite        = 'Contractuel'
        elif prof.type == 'militaire':
            # Enseignant militaire (charge reglementaire) — grade militaire (Capitaine, Colonel...)
            titre_document = 'Attestation de Service Fait'
            afficher_nni   = False
            qualite        = prof.grade if prof.grade else 'Enseignant militaire'
        elif prof.type == 'personnel_admin':
            # Personnel administratif : surveillance / encadrement / mission via vacation
            # -> Attestation de Service Fait (pas d'enseignement)
            titre_document = 'Attestation de Service Fait'
            afficher_nni   = True
            qualite        = 'Personnel administratif'
        elif prof.type == 'personnel_militaire':
            # Personnel militaire : idem, service fait via vacation
            titre_document = 'Attestation de Service Fait'
            afficher_nni   = True
            qualite        = 'Personnel militaire'
        else:
            titre_document = "Attestation d'Enseignement"
            afficher_nni   = True
            qualite        = prof.type

        # ── Texte filieres (chaine stable independante des departements annuels)
        descriptions_filieres = sorted(filieres_globales)
        if not descriptions_filieres:
            texte_filieres_description = ''
        elif len(descriptions_filieres) == 1:
            texte_filieres_description = descriptions_filieres[0]
        elif len(descriptions_filieres) == 2:
            texte_filieres_description = f"{descriptions_filieres[0]} et {descriptions_filieres[1]}"
        else:
            texte_filieres_description = (', '.join(descriptions_filieres[:-1])
                                          + f" et {descriptions_filieres[-1]}")

        # ── Numéro + QR vérifiable (enseignant → total pondéré éq. CM)
        numero_attestation, qr_data_uri, _tok = _build_attestation_qr(
            prof, annee, date_debut, date_fin, titre_document, grand_total_ponde, request,
        )

        context = {
            'prof':                       prof,
            'civilite':                   civilite,
            'interesse':                  interesse,
            'qualite':                    qualite,
            'afficher_nni':               afficher_nni,
            'titre_document':             titre_document,
            'numero_attestation':         numero_attestation,
            'qr_data_uri':                qr_data_uri,
            'annee_universitaire':        annee,
            'is_service_fait':            False,   # Branche enseignant
            'modules':                    modules_list,
            'grand_total_ponde':          grand_total_ponde,
            'texte_types':                texte_types,
            'texte_depts_description':    texte_depts_description,
            'texte_filieres_description': texte_filieres_description,
            'filieres_globales':          sorted(filieres_globales),
            'niveaux_globaux':            sorted(niveaux_globaux),
            'date_debut_periode':         str(date_debut) if date_debut else '',
            'date_fin_periode':           str(date_fin) if date_fin else '',
            'lieu':                       'Nouakchott',
            'date_generation':            date.today().strftime('%d/%m/%Y'),
            'coeff_td_tp':                f'{coeff_td_tp:.4f}',
        }
        # Retour du dict pour reutilisation par pdf_attestation ET attestation_info
        return {
            'context_pdf':   context,
            'meta': {
                'nb_modules':         len(modules_list),
                'total_heures_cm':    sum(m['heures_cm'] for m in modules_list),
                'total_heures_td':    sum(m['heures_td'] for m in modules_list),
                'total_heures_tp':    sum(m['heures_tp'] for m in modules_list),
                'grand_total_ponde':  grand_total_ponde,
                'nb_filieres':        len(filieres_globales),
                'nb_departements':    len(depts_codes_globaux),
                'numero_attestation': numero_attestation,
            },
            'error':       None,
            'status_code': 200,
        }

    @action(detail=False, methods=['get'], url_path='pdf-attestation')
    def pdf_attestation(self, request):
        """Generation de l'attestation PDF.
        Fusionne SuiviePointage (commentaire='Fait') + Vacation (em non nul).
        Cle de fusion : (em_id, code_semestre)
        Ponderation horaire (specifique attestation) : CM=1, TD=TP=PR=2/3 h eq CM.

        Parametres :
          prof_id      : OBLIGATOIRE
          annee_univ   : OBLIGATOIRE
          filiere_id   : OPTIONNEL — filtre par filiere stable
          date_debut   : OPTIONNEL (YYYY-MM-DD) — restreint la periode
          date_fin     : OPTIONNEL (YYYY-MM-DD)
        """
        annee      = request.query_params.get('annee_univ')
        prof_id    = request.query_params.get('prof_id')
        filiere_id = request.query_params.get('filiere_id')
        date_debut = request.query_params.get('date_debut')
        date_fin   = request.query_params.get('date_fin')

        # Enseignant connecté : self-service auto-scopé sur son propre profil,
        # SANS droit vac_paiement. Admin/gestionnaire : RBAC inchangé.
        if getattr(request.user, 'role', None) == 'enseignant':
            try:
                prof_id = request.user.prof_profile.pk
            except Exception:
                return Response({'error': 'Profil enseignant introuvable.'}, status=404)
            if not annee:
                try:
                    annee = request.user.contexte.annee_universitaire
                except Exception:
                    pass
        else:
            _check_vacation_module(request.user, 'vac_paiement', action='modifier')

        if not annee or not prof_id:
            return Response({'error': 'annee_univ et prof_id requis.'}, status=400)

        from apps.prof.models import Prof
        try:
            prof = Prof.objects.get(pk=prof_id)
        except Prof.DoesNotExist:
            return Response({'error': 'Professeur non trouvé.'}, status=404)

        result = self._build_attestation_context(
            request, prof, annee,
            filiere_id=filiere_id, date_debut=date_debut, date_fin=date_fin,
        )
        if result.get('error'):
            return Response({'error': result['error']}, status=result['status_code'])

        # Audit log : trace l'emission de l'attestation via logger Django
        # (channel 'siga' -> logs/siga.log + console + AuditLog si configure)
        try:
            import logging
            _aud_logger = logging.getLogger('siga')
            _aud_logger.info(
                'attestation_generee',
                extra={
                    'user_id':            getattr(request.user, 'pk', None),
                    'username':           getattr(request.user, 'username', '?'),
                    'prof_id':            prof.pk,
                    'prof_nom':           prof.nom,
                    'annee_univ':         annee,
                    'filiere_id':         filiere_id or '',
                    'numero_attestation': result['meta']['numero_attestation'],
                    'nb_modules':         result['meta']['nb_modules'],
                    'grand_total_ponde':  result['meta']['grand_total_ponde'],
                },
            )
        except Exception:
            pass  # silent fail : log non critique

        # Nom de fichier integrant le nom de l'enseignant + numero attestation
        safe_nom  = prof.nom.replace(' ', '_').replace('/', '-')
        filename  = f"attestation_{safe_nom}_{annee}_{result['meta']['numero_attestation']}.pdf"
        return _render_pdf('vacation_attestation_pdf.html',
                           result['context_pdf'], filename, orientation='Portrait')

    @action(detail=False, methods=['get'], url_path='attestation-info')
    def attestation_info(self, request):
        """Precheck JSON pour la page /payement/attestation cote frontend.
        Memes parametres que pdf-attestation. Retourne le resume (nb modules,
        total heures, filieres detectees, numero d'attestation projete) SANS
        generer le PDF — pour permettre au frontend d'afficher un preview avant
        le telechargement.

        Note : le numero_attestation retourne ici est INDICATIF (regenere a chaque
        appel) — le numero definitif est genere a la generation du PDF.
        """
        _check_vacation_module(request.user, 'vac_paiement', action='voir')
        annee      = request.query_params.get('annee_univ')
        prof_id    = request.query_params.get('prof_id')
        filiere_id = request.query_params.get('filiere_id')
        date_debut = request.query_params.get('date_debut')
        date_fin   = request.query_params.get('date_fin')

        if not annee or not prof_id:
            return Response({'error': 'annee_univ et prof_id requis.'}, status=400)

        from apps.prof.models import Prof
        try:
            prof = Prof.objects.get(pk=prof_id)
        except Prof.DoesNotExist:
            return Response({'error': 'Professeur non trouvé.'}, status=404)

        result = self._build_attestation_context(
            request, prof, annee,
            filiere_id=filiere_id, date_debut=date_debut, date_fin=date_fin,
        )
        if result.get('error'):
            return Response({
                'has_data':  False,
                'error':     result['error'],
            }, status=result['status_code'])

        ctx = result['context_pdf']
        is_service = ctx.get('is_service_fait', False)
        return Response({
            'has_data':            True,
            'prof_nom':            prof.nom,
            'prof_type':           prof.type,
            'qualite':             ctx['qualite'],
            'titre_document':      ctx['titre_document'],
            'numero_attestation':  ctx['numero_attestation'],
            'is_service_fait':     is_service,
            'meta':                result['meta'],
            'filieres':            ctx.get('filieres_globales', []),
            'niveaux':             ctx.get('niveaux_globaux', []),
            'departements':        sorted({d for m in ctx.get('modules', []) for d in m.get('departements', [])}) if not is_service else [],
            'services':            ctx.get('services_list', []) if is_service else [],
            'periode': {
                'debut':           ctx['date_debut_periode'],
                'fin':             ctx['date_fin_periode'],
            },
        })


# ══════════════════════════════════════════════════════════════════════════════
# SURVEILLANCE VIEWSET
# ══════════════════════════════════════════════════════════════════════════════
class SurveillanceViewSet(InstitutionScopedMixin, AuditMixin, viewsets.ModelViewSet):
    queryset = Surveillance.objects.select_related('prof', 'departement').all()
    serializer_class   = SurveillanceSerializer
    permission_classes = [RBACPermission]
    required_module    = 'suivi_saisie'
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['annee_univ', 'prof', 'departement']
    ordering_fields    = ['date']
    pagination_class   = StandardPagination
