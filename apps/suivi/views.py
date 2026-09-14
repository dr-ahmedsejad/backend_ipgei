import locale
import logging
from collections import defaultdict

from django.db import transaction
from django.db.models import Count, Max, Sum, F, Q, Min
from django.http import HttpResponse
from django.template.loader import render_to_string
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import OrderingFilter
from rest_framework.permissions import IsAuthenticated
from core.horaire import horaire_affiche
from core.libelle_dept import filiere_distingue, format_depts_compact
from core.permissions import RBACPermission, EDTDepartementPermission
from core.mixins import AuditMixin, InstitutionScopedMixin, DepartementScopedMixin
from core.pagination import StandardPagination
from apps.emplois.services.emplois_service import archiver_emplois, restaurer_depuis_archive
from core.audit_helpers import audit_aggregate, write_audit_bulk
from .models import Suivie, SuiviePointage, SuiviePointageDepartement, ChargeInstitution, SuiviGenerationAuthorization
from .serializers import (
    SuivieSerializer, SuivieCreateSerializer,
    SuiviePointageSerializer, ChargeInstitutionSerializer,
)
from ..emplois.models import Emplois, EmploisArchive

logger = logging.getLogger('siga')


SUIVIE_SELECT_RELATED = (
    'prof', 'em', 'departement', 'salle', 'semestre', 'creneau_fk', 'type_seance_fk', 'jour_fk',
)
POINTAGE_SELECT_RELATED = (
    'prof', 'em', 'salle', 'semestre', 'creneau_fk', 'type_seance_fk', 'jour_fk',
)


# Le helper d'audit bulk est maintenant dans core.audit_helpers.write_audit_bulk
# pour etre reutilisable depuis d'autres modules (evaluations, etc.).
_audit_bulk_safe = write_audit_bulk


class SuivieViewSet(InstitutionScopedMixin, DepartementScopedMixin, AuditMixin, viewsets.ModelViewSet):
    queryset = Suivie.objects.select_related(*SUIVIE_SELECT_RELATED).all()
    permission_classes = [RBACPermission, EDTDepartementPermission]
    required_module    = 'suivi_saisie'
    # FK simple `departement` sur Suivie
    departement_filter_field  = 'departement'
    departement_filter_lookup = 'in'
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['annee_universitaire', 'prof', 'departement', 'semestre',
                          'numero_semaine', 'type_semestre', 'type_seance_fk', 'commentaire']
    ordering_fields    = ['numero_semaine', 'date_suivie']
    pagination_class   = StandardPagination

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return SuivieCreateSerializer
        return SuivieSerializer

    # ── DELETE /api/v1/suivi/suivies/par-semaine/ ─────────────────────────
    @action(detail=False, methods=['delete'], url_path='par-semaine')
    def supprimer_par_semaine(self, request):
        semaine = request.query_params.get('numero_semaine')
        annee = request.query_params.get('annee_universitaire')
        dept = request.query_params.get('departement')
        ts = request.query_params.get('type_semestre')

        if not all([semaine, annee]):
            return Response({'error': 'numero_semaine et annee_universitaire requis.'}, status=400)

        # Scope user : non-admin n'efface que SES departements (intersection
        # avec ?departement= si fourni).
        user_dept_ids = self.user_dept_ids()
        if user_dept_ids is not None and not user_dept_ids:
            return Response(
                {'error': "Aucun groupe ne vous est attribue. Contactez un administrateur."},
                status=403,
            )

        # Verifier l'ordre LIFO sur le PERIMETRE du user (chaque responsable
        # supprime ses semaines independamment).
        qs_gen = Suivie.objects.filter(annee_universitaire=annee)
        if ts:
            qs_gen = qs_gen.filter(type_semestre=ts)
        if user_dept_ids is not None:
            qs_gen = qs_gen.filter(departement_id__in=user_dept_ids)

        max_semaine = qs_gen.aggregate(max=Max('numero_semaine'))['max']
        if max_semaine is not None and int(semaine) != max_semaine:
            return Response(
                {'error': f"Suppression impossible. Vous devez d'abord supprimer la semaine "
                          f"{max_semaine} avant de pouvoir supprimer la semaine {semaine}."},
                status=400,
            )

        with transaction.atomic():
            qs = Suivie.objects.filter(numero_semaine=semaine, annee_universitaire=annee)
            if dept:
                qs = qs.filter(departement_id=dept)
            if ts:
                qs = qs.filter(type_semestre=ts)
            # Scope user prioritaire (intersection)
            if user_dept_ids is not None:
                qs = qs.filter(departement_id__in=user_dept_ids)

            # Garde-fou : Presence.suivi est en CASCADE — supprimer ces Suivie
            # détruirait les absences/justificatifs enregistrés. On bloque si des
            # présences NON vierges existent, sauf confirmation explicite ?force=1.
            from apps.absence.models import Presence
            force = request.query_params.get('force') in ('1', 'true', 'True')
            presences_reelles = (
                Presence.objects.filter(suivi__in=qs)
                .exclude(statut=0, justificatif='', commentaire='')
                .count()
            )
            if presences_reelles and not force:
                return Response(
                    {
                        'status': 409,
                        'error': (
                            f"Cette semaine contient {presences_reelles} présence(s)/"
                            f"absence(s) enregistrée(s) avec données (statut, justificatif "
                            f"ou commentaire). Les supprimer détruirait cet historique. "
                            f"Relancez avec ?force=1 pour confirmer."
                        ),
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            count, _ = qs.delete()

            # Supprimer les pointages de la semaine + leurs M2M (cascade automatique).
            sp_qs = SuiviePointage.objects.filter(
                numero_semaine=semaine, annee_universitaire=annee,
                **({"type_semestre": ts} if ts else {}),
            )
            if user_dept_ids is not None:
                # Django interdit .delete() apres .distinct() -> resolution en 2 temps
                sp_ids = list(
                    sp_qs.filter(departements__in=user_dept_ids)
                         .values_list('pk', flat=True).distinct()
                )
                sp_qs = SuiviePointage.objects.filter(pk__in=sp_ids)
            sp_qs.delete()

            # remaining : nombre de Suivie restants pour ce user (decide si
            # on restaure son EDT depuis l'archive).
            remaining_qs = Suivie.objects.filter(annee_universitaire=annee)
            if ts:
                remaining_qs = remaining_qs.filter(type_semestre=ts)
            if user_dept_ids is not None:
                remaining_qs = remaining_qs.filter(departement_id__in=user_dept_ids)
            remaining = remaining_qs.count()

            # Restauration EDT : on ne touche QUE les depts qui ont une archive
            # a restaurer pour cette annee+ts. Les Emplois en cours d'edition
            # (importes mais pas encore generes) restent intacts car ils
            # n'ont pas d'archive correspondante.
            archive_qs = EmploisArchive.objects.filter(annee_universitaire=annee)
            if ts:
                archive_qs = archive_qs.filter(type_semestre=ts)
            archive_depts = set(archive_qs.values_list('departement_id', flat=True))

            # Intersection avec le perimetre du user (superuser -> tous les
            # archive_depts, responsable -> intersection avec managed_departements).
            if user_dept_ids is None:
                restore_depts = list(archive_depts)
            else:
                restore_depts = list(set(user_dept_ids) & archive_depts)

            if restore_depts:
                # On efface d'abord les Emplois existants pour CES depts uniquement
                # (sinon bulk_create restaurerait des doublons par-dessus).
                edt_clear_qs = Emplois.objects.filter(
                    annee_universitaire=annee, departement_id__in=restore_depts,
                )
                if ts:
                    edt_clear_qs = edt_clear_qs.filter(type_semestre=ts)
                edt_clear_qs.delete()

                restored = restaurer_depuis_archive(annee, ts or '', dept_ids=restore_depts)
            else:
                restored = 0

        return Response({'deleted': count, 'restored': restored, 'remaining': remaining})

    # ── GET /api/v1/suivi/suivies/semaines-generees/ ──────────────────────
    @action(detail=False, methods=['get'], url_path='semaines-generees')
    def semaines_generees(self, request):
        from apps.parametres.models import Semaine as SemaineParam
        from django.conf import settings as _settings
        from django.db.models import Min as _Min, Max as _Max
        import datetime as _dt

        annee = request.query_params.get('annee_universitaire')
        ts    = request.query_params.get('type_semestre')
        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)

        # Indicateurs frontend : admin et grace period pour filtrer la liste
        is_admin_user = (
            request.user.is_superuser
            or getattr(request.user, 'role', None) == 'admin'
        )
        grace_days = getattr(_settings, 'SUIVI_GRACE_DAYS_AFTER_WEEK_END', 0)

        # Autorisations de rattrapage actives pour ce user (non-admin) sur cette
        # annee+ts. Permet au frontend d'afficher la semaine clouturee comme
        # "Autorisee" au lieu de "Cloturee".
        authorized_weeks: list = []
        if not is_admin_user and ts:
            authorized_weeks = list(
                SuiviGenerationAuthorization.objects
                .filter(
                    user=request.user, annee_universitaire=annee,
                    type_semestre=ts, used_at__isnull=True,
                )
                .values_list('numero_semaine', flat=True)
            )

        # Calcul de la semaine pedagogique en cours : on cherche celle dont
        # date_debut <= today <= date_fin. Fallback : la prochaine a venir,
        # sinon la derniere terminee. Permet au frontend d'afficher un
        # badge "En cours" et de trier intelligemment.
        today = _dt.date.today()
        semaines_calendar = list(
            SemaineParam.objects
            .filter(
                annee_universitaire=annee,
                type_semestre=ts or 'I',
                type_semaine='cours',
                numero_semaine__isnull=False,
            )
            .values('numero_semaine')
            .annotate(date_debut=_Min('date'), date_fin=_Max('date'))
            .order_by('date_debut')
        )
        current_week = None
        # 1. Match exact : sem contenant today
        for w in semaines_calendar:
            if w['date_debut'] <= today <= w['date_fin']:
                current_week = w['numero_semaine']
                break
        # 2. Fallback : prochaine a venir
        if current_week is None:
            upcoming = [w for w in semaines_calendar if w['date_debut'] > today]
            if upcoming:
                current_week = upcoming[0]['numero_semaine']
        # 3. Fallback : la derniere terminee (semestre fini)
        if current_week is None and semaines_calendar:
            past = [w for w in semaines_calendar if w['date_fin'] < today]
            if past:
                current_week = past[-1]['numero_semaine']

        # Liste des semaines generees, scope user
        qs = Suivie.objects.filter(annee_universitaire=annee)
        if ts:
            qs = qs.filter(type_semestre=ts)
        user_dept_ids = self.user_dept_ids()
        if user_dept_ids is not None:
            if not user_dept_ids:
                return Response({
                    'semaines_generees': [],
                    'current_week': current_week,
                    'is_admin_user': is_admin_user,
                    'grace_days_after_week_end': grace_days,
                    'authorized_weeks': authorized_weeks,
                })
            qs = qs.filter(departement_id__in=user_dept_ids)
        generees = list(
            qs.values_list('numero_semaine', flat=True).distinct().order_by('numero_semaine')
        )
        return Response({
            'semaines_generees': generees,
            'current_week': current_week,
            'is_admin_user': is_admin_user,
            'grace_days_after_week_end': grace_days,
            'authorized_weeks': authorized_weeks,
        })

    # ── POST /api/v1/suivi/suivies/ajouter/ ───────────────────────────────
    @action(detail=False, methods=['post'], url_path='ajouter')
    @audit_aggregate(label='Génération suivi semaine', action='BULK_CREATE',
                     model_name='SuiviGeneration')
    def ajouter_suivie(self, request):
        """
        Genere le suivi de la semaine a partir de l'emploi du temps.
        Body: { annee_universitaire, numero_semaine, type_semestre }
        """
        annee         = request.data.get('annee_universitaire')
        numero_sem    = request.data.get('numero_semaine')
        type_semestre = request.data.get('type_semestre', 'I')

        if not all([annee, numero_sem]):
            return Response({'error': 'annee_universitaire et numero_semaine requis.'}, status=400)

        from apps.parametres.models import Semaine, Creneau, Jour, Paiement
        from apps.parametres.models import Semestre as SemestreModel

        # Scope user : non-admin -> on ne genere que pour ses managed_departements.
        # Admin/superuser : user_dept_ids() retourne None -> aucun scoping (comportement legacy).
        user_dept_ids = self.user_dept_ids()
        if user_dept_ids is not None and not user_dept_ids:
            return Response({
                'error': (
                    "Aucun groupe ne vous est attribue pour la gestion EDT. "
                    "Contactez un administrateur."
                )
            }, status=403)

        # IPGEI : `Emplois` est une projection de l'emploi du temps, et la
        # generation archive puis VIDE cette table — comportement de SIGA, ou
        # l'EDT se ressaisit chaque periode. A l'IPGEI il est permanent : on le
        # reprojette donc AVANT la garde ci-dessous, sinon la deuxieme semaine
        # serait refusee faute d'EDT.
        #
        # La projection part des seances REELLES de la semaine demandee : c'est
        # ce qui fait parvenir jusqu'au Suivi les permutations d'enseignant et
        # les annulations. Sans le numero de semaine, elle retombait sur la
        # grille type, et le remplacant n'etait jamais paye.
        # Idempotent, sans effet si aucune donnee IPGEI n'existe.
        try:
            from apps.ipgei.miroir import rematerialiser_emplois
            rematerialiser_emplois(annee, type_semestre, numero_semaine=numero_sem)
        except Exception:
            logger.exception('Re-materialisation des Emplois IPGEI impossible')

        # Garde : refuser la generation si l'EDT courant est vide *dans le perimetre*.
        # Sinon le flux supprime les Suivie/SuiviePointage existants pour cette
        # semaine sans rien creer, et renvoie un faux succes en ayant detruit
        # les donnees precedentes.
        edt_check_qs = Emplois.objects.filter(
            annee_universitaire=annee, type_semestre=type_semestre,
        )
        if user_dept_ids is not None:
            edt_check_qs = edt_check_qs.filter(departement_id__in=user_dept_ids)
        if not edt_check_qs.exists():
            scope_label = ' dans votre perimetre' if user_dept_ids is not None else ''
            # IPGEI : formuler la cause dans les gestes de l'utilisateur plutot
            # que de lui demander d'« importer un EDT », operation qui n'existe
            # pas dans ce produit.
            try:
                from apps.ipgei.miroir import diagnostic_edt
                message_ipgei = diagnostic_edt(annee, type_semestre)
            except Exception:
                message_ipgei = ''
            if message_ipgei:
                return Response({'error': message_ipgei}, status=400)
            return Response({
                'error': (
                    f"Aucun emploi du temps en cours{scope_label} pour {annee} "
                    f"(semestre {type_semestre}). Importez ou restaurez un EDT "
                    f"avant de generer le suivi de la semaine {numero_sem}."
                )
            }, status=400)

        # Garde complementaire : la semaine cible doit exister dans la table
        # Semaine, sinon les dates_semaine seront vides et les Suivie crees
        # auront date_suivie=None (silencieusement incoherent).
        if not Semaine.objects.filter(
            numero_semaine=numero_sem,
            annee_universitaire=annee,
            type_semestre=type_semestre,
        ).exists():
            return Response({
                'error': (
                    f"La semaine {numero_sem} n'existe pas pour {annee} / "
                    f"semestre {type_semestre}. Ajoutez-la dans Parametres > "
                    f"Semaines avant de generer son suivi."
                )
            }, status=400)

        # NB : pas de blocage temporel. Les cas legitimes de rattrapage
        # (stage L2, absence prolongee, migration de donnees) doivent rester
        # possibles. La protection contre les saisies accidentelles passe
        # par l'UI (affichage des dates, badge "en cours", tri intelligent,
        # modale de confirmation cote frontend pour vieilles semaines).

        # Garde d'ordre sequentiel : refuser la creation d'un trou.
        # Regle (symetrique avec la suppression LIFO) :
        #   - aucune semaine generee  -> N doit valoir 1
        #   - max existant = M        -> N <= M+1 (regeneration ou comblement
        #                                de trou autorise, saut interdit)
        try:
            n_int = int(numero_sem)
        except (TypeError, ValueError):
            return Response({'error': 'numero_semaine doit etre un entier.'}, status=400)

        # Check temporel : une semaine ecoulee (au-dela de la grace period)
        # ne peut etre generee QUE par admin/superuser OU par un user explicitement
        # autorise (SuiviGenerationAuthorization). Plus de LIFO : n'importe quelle
        # semaine en cours ou future est generable.
        from django.conf import settings as _settings
        from django.db.models import Max as _Max
        from django.utils import timezone as _tz
        import datetime as _dt
        is_admin_or_su = (
            request.user.is_superuser
            or getattr(request.user, 'role', None) == 'admin'
        )
        consumed_authorization = None   # pour marquer used_at apres generation reussie
        if not is_admin_or_su:
            grace_days = getattr(_settings, 'SUIVI_GRACE_DAYS_AFTER_WEEK_END', 0)
            sem_max_date = Semaine.objects.filter(
                numero_semaine=numero_sem,
                annee_universitaire=annee,
                type_semestre=type_semestre,
            ).aggregate(d=_Max('date'))['d']
            today = _dt.date.today()
            if sem_max_date and (today - sem_max_date).days > grace_days:
                # Verifier s'il existe une autorisation active pour ce user
                consumed_authorization = SuiviGenerationAuthorization.objects.filter(
                    user=request.user,
                    annee_universitaire=annee,
                    type_semestre=type_semestre,
                    numero_semaine=n_int,
                    used_at__isnull=True,
                ).first()
                if consumed_authorization is None:
                    return Response({
                        'error': (
                            f"La semaine {numero_sem} ({sem_max_date.strftime('%d/%m/%Y')}) "
                            f"est cloturee. Seul un administrateur peut generer son suivi "
                            f"(rattrapage). Contactez votre admin pour proceder."
                        )
                    }, status=403)

        # Determiner l'institution principale (NOT NULL en DB sur Suivie/SuiviePointage)
        inst_id = self._get_default_institution_id()

        with transaction.atomic():
            # Dates de la semaine indexees par jour_fk_id.
            # Phase 5 : Semaine.jour (CharField) -> Semaine.jour_fk (FK Jour).
            # On lit directement jour_fk_id, plus besoin du lookup label -> Jour.
            semaine_entries = list(
                Semaine.objects.filter(
                    numero_semaine=numero_sem,
                    annee_universitaire=annee,
                    type_semestre=type_semestre,
                ).values('jour_fk_id', 'date')
            )
            dates_semaine = {e['jour_fk_id']: e['date']
                             for e in semaine_entries if e['jour_fk_id']}

            # Emplois de l'annee + type, scopes au perimetre du user
            emplois_qs = Emplois.objects.filter(
                annee_universitaire=annee,
                type_semestre=type_semestre,
            )
            if user_dept_ids is not None:
                emplois_qs = emplois_qs.filter(departement_id__in=user_dept_ids)
            emplois_entries = list(
                emplois_qs.select_related('prof', 'em', 'salle', 'departement', 'semestre',
                                          'creneau_fk', 'type_seance_fk', 'jour_fk')
            )

            # Archiver l'EDT courant (scope par dept du lot, voir archiver_emplois)
            archiver_emplois(annee, type_semestre, emplois_entries)

            # Relever les pointages « Fait » AVANT de detruire, pour les
            # reappliquer aux seances inchangees. Sans cela, corriger une salle
            # en semaine 1 faisait reperdre tout le pointage de la semaine :
            # la regeneration etait un remplacement, pas une synchronisation.
            #
            # La cle volontairement EXCLUT la salle : changer de salle ne change
            # pas le fait que le cours a eu lieu. Elle retient ce qui identifie
            # l'enseignement rendu — qui, quoi, quand, de quel type.
            pointages_faits = {
                (p.prof_id, p.em_id, p.jour_fk_id, p.creneau_fk_id, p.type_seance_fk_id)
                for p in SuiviePointage.objects.filter(
                    annee_universitaire=annee, numero_semaine=numero_sem,
                    type_semestre=type_semestre, commentaire='Fait',
                )
            }

            # Supprimer la semaine cible existante (Suivie + SuiviePointage)
            # scopee au perimetre du user. Les lignes d'autres responsables
            # pour la meme semaine ne sont PAS touchees.
            suivie_del_qs = Suivie.objects.filter(
                annee_universitaire=annee, numero_semaine=numero_sem, type_semestre=type_semestre,
            )
            if user_dept_ids is not None:
                suivie_del_qs = suivie_del_qs.filter(departement_id__in=user_dept_ids)
            suivie_del_qs.delete()

            sp_del_qs = SuiviePointage.objects.filter(
                annee_universitaire=annee, numero_semaine=numero_sem, type_semestre=type_semestre,
            )
            if user_dept_ids is not None:
                # SuiviePointage : M2M sur departements. On supprime ceux dont
                # AU MOINS UN dept est dans le perimetre. Django interdit
                # .delete() apres .distinct() -> on resoud en deux temps via
                # une sous-requete d'ids.
                sp_ids = list(
                    sp_del_qs.filter(departements__in=user_dept_ids)
                            .values_list('pk', flat=True).distinct()
                )
                sp_del_qs = SuiviePointage.objects.filter(pk__in=sp_ids)
            sp_del_qs.delete()

            # Creneaux actifs + jours
            creneaux_actifs = list(Creneau.objects.filter(is_actif=True).order_by('ordre'))
            jours_list      = list(Jour.objects.order_by('id'))

            # Taux de paiement par type_seance (via FK)
            import datetime
            today = datetime.date.today()
            paiement_map = {}
            for ts_fk in {e.type_seance_fk_id for e in emplois_entries if e.type_seance_fk_id}:
                ts_label = next((e.type_seance_fk.type_seance for e in emplois_entries
                                 if e.type_seance_fk_id == ts_fk and e.type_seance_fk), None)
                if ts_label:
                    paiement_map[ts_fk] = Paiement.get_taux_at(ts_label, today)

            # Combos dept+semestre dans les emplois (cles via FK)
            dept_sem_combos = list({(e.departement_id, e.semestre_id) for e in emplois_entries})
            existing_keys = {
                (e.departement_id, e.semestre_id, e.jour_fk_id, e.creneau_fk_id)
                for e in emplois_entries
            }

            # --- 1) Emplois -> Suivie (FK seulement) ---
            suivie_objects = []
            for e in emplois_entries:
                date_c = dates_semaine.get(e.jour_fk_id)
                duree  = e.creneau_fk.duree if e.creneau_fk_id and e.creneau_fk else 1.5
                taux   = paiement_map.get(e.type_seance_fk_id, 0.0)
                suivie_objects.append(
                    Suivie(
                        annee_universitaire=annee, numero_semaine=int(numero_sem),
                        commentaire='Non fait', taux_paiement=taux,
                        date_suivie=date_c, type_semestre=type_semestre,
                        duree_creneau=duree,
                        prof_id=e.prof_id, em_id=e.em_id, salle_id=e.salle_id,
                        departement_id=e.departement_id, semestre_id=e.semestre_id,
                        creneau_fk_id=e.creneau_fk_id,
                        type_seance_fk_id=e.type_seance_fk_id,
                        jour_fk_id=e.jour_fk_id,
                        institution_id=e.institution_id or inst_id,
                    )
                )

            # --- 1b) Lignes vides par creneau x jour x dept+sem manquant ---
            for dept_id, sem_id in dept_sem_combos:
                for jour in jours_list:
                    date_c = dates_semaine.get(jour.pk)
                    for cr in creneaux_actifs:
                        if (dept_id, sem_id, jour.pk, cr.pk) not in existing_keys:
                            suivie_objects.append(
                                Suivie(
                                    annee_universitaire=annee, numero_semaine=int(numero_sem),
                                    commentaire='Non fait', date_suivie=date_c,
                                    type_semestre=type_semestre, duree_creneau=cr.duree,
                                    creneau_fk_id=cr.pk, jour_fk_id=jour.pk,
                                    departement_id=dept_id, semestre_id=sem_id,
                                    institution_id=inst_id,
                                )
                            )

            if suivie_objects:
                Suivie.objects.bulk_create(suivie_objects)

            # --- 2) Fusion -> SuiviePointage (cles via FK) ---
            groups = defaultdict(lambda: {
                'departements': set(), 'max_date': None, 'duree_creneau': None,
                'prof_id': None, 'em_id': None, 'salle_id': None,
                'semestre_id': None, 'taux_paiement': None,
                'type_seance_fk_id': None, 'jour_fk_id': None, 'creneau_fk_id': None,
            })
            for s in suivie_objects:
                key = (s.prof_id, s.em_id, s.type_seance_fk_id, s.jour_fk_id, s.creneau_fk_id,
                       s.salle_id, s.semestre_id, annee, int(numero_sem))
                g = groups[key]
                if s.departement_id:
                    g['departements'].add(s.departement_id)
                if s.date_suivie and (g['max_date'] is None or s.date_suivie > g['max_date']):
                    g['max_date'] = s.date_suivie
                if g['duree_creneau'] is None:
                    g['duree_creneau'] = s.duree_creneau
                g['prof_id']           = s.prof_id
                g['em_id']             = s.em_id
                g['salle_id']          = s.salle_id
                g['semestre_id']       = s.semestre_id
                g['taux_paiement']     = s.taux_paiement
                g['type_seance_fk_id'] = s.type_seance_fk_id
                g['jour_fk_id']        = s.jour_fk_id
                g['creneau_fk_id']     = s.creneau_fk_id

            # Calcul du taux SuiviePointage : doit utiliser la date REELLE de
            # chaque seance (date_pointage), pas today. Le precedent code
            # appliquait Paiement.get_taux_at(label, today) globalement, ce qui
            # affectait incorrectement le taux d'une seance prevue avant un
            # changement de tarif applique le jour de la generation.
            ts_label_map = {
                e.type_seance_fk_id: e.type_seance_fk.type_seance
                for e in emplois_entries
                if e.type_seance_fk_id and e.type_seance_fk
            }
            _taux_cache: dict = {}
            def _taux_pour_seance(ts_fk_id, date_seance):
                label = ts_label_map.get(ts_fk_id)
                if not label or not date_seance:
                    return 0.0
                k = (ts_fk_id, date_seance)
                if k not in _taux_cache:
                    _taux_cache[k] = Paiement.get_taux_at(label, date_seance)
                return _taux_cache[k]

            pointage_objects = []
            pointage_dept_ids: list[set] = []
            for key, g in groups.items():
                date_pointage = g['max_date'] or today
                taux_correct  = _taux_pour_seance(g['type_seance_fk_id'], date_pointage)
                # Un enseignement deja marque « Fait » le reste : la
                # regeneration reajuste l'emploi du temps, elle n'efface pas
                # le constat de ce qui a ete assure.
                deja_fait = (
                    g['prof_id'], g['em_id'], g['jour_fk_id'],
                    g['creneau_fk_id'], g['type_seance_fk_id'],
                ) in pointages_faits

                pointage_objects.append(
                    SuiviePointage(
                        annee_universitaire=key[7], numero_semaine=key[8],
                        commentaire='Fait' if deja_fait else 'Non fait',
                        date_suivie=date_pointage,
                        type_semestre=type_semestre,
                        duree_creneau=g['duree_creneau'],
                        taux_paiement=taux_correct,
                        prof_id=g['prof_id'], em_id=g['em_id'], salle_id=g['salle_id'],
                        semestre_id=g['semestre_id'],
                        creneau_fk_id=g['creneau_fk_id'],
                        type_seance_fk_id=g['type_seance_fk_id'],
                        jour_fk_id=g['jour_fk_id'],
                        institution_id=inst_id,
                    )
                )
                pointage_dept_ids.append(set(g['departements']))

            if pointage_objects:
                SuiviePointage.objects.bulk_create(pointage_objects)

                # MySQL ne remplit pas les PKs apres bulk_create
                # (can_return_rows_from_bulk_insert=False). On re-fetch les
                # pointages crees et on les indexe par la meme cle de fusion
                # que celle utilisee dans `groups` pour associer la M2M.
                created_pointages = list(
                    SuiviePointage.objects.filter(
                        annee_universitaire=annee,
                        numero_semaine=int(numero_sem),
                        type_semestre=type_semestre,
                    )
                )
                by_key = {}
                for sp in created_pointages:
                    k = (sp.prof_id, sp.em_id, sp.type_seance_fk_id,
                         sp.jour_fk_id, sp.creneau_fk_id, sp.salle_id,
                         sp.semestre_id, sp.annee_universitaire, sp.numero_semaine)
                    by_key[k] = sp.pk

                m2m_objects = []
                for key, g in groups.items():
                    sp_pk = by_key.get(key)
                    if not sp_pk or not g['departements']:
                        continue
                    for did in g['departements']:
                        m2m_objects.append(
                            SuiviePointageDepartement(
                                suiviepointage_id=sp_pk,
                                departement_id=did,
                            )
                        )
                if m2m_objects:
                    SuiviePointageDepartement.objects.bulk_create(m2m_objects, ignore_conflicts=True)

                # Audit individuel par SuiviePointage cree. bulk_create() ne
                # declenche pas les signals -> sans ce log, l'horloge UI sur
                # un pointage donne afficherait toujours "Aucune trace".
                audit_items_sp = [
                    {
                        'object_id': sp.pk,
                        'institution_id': sp.institution_id,
                        'changes': {
                            'snapshot': {
                                'annee_universitaire': sp.annee_universitaire,
                                'numero_semaine':      sp.numero_semaine,
                                'type_semestre':       sp.type_semestre,
                                'date_suivie':         str(sp.date_suivie) if sp.date_suivie else None,
                                'commentaire':         sp.commentaire,
                                'duree_creneau':       sp.duree_creneau,
                                'taux_paiement':       sp.taux_paiement,
                                'prof_id':             sp.prof_id,
                                'em_id':               sp.em_id,
                                'salle_id':            sp.salle_id,
                                'semestre_id':         sp.semestre_id,
                                'creneau_fk_id':       sp.creneau_fk_id,
                                'type_seance_fk_id':   sp.type_seance_fk_id,
                                'jour_fk_id':          sp.jour_fk_id,
                            },
                        },
                    }
                    for sp in created_pointages
                ]
                _audit_bulk_safe(
                    'SuiviePointage', 'CREATE', audit_items_sp,
                    label=f'Generation suivi sem {numero_sem}',
                    request=request,
                )

            # Supprimer l'EDT courant apres archivage, SCOPE au perimetre du user.
            # Les emplois des autres responsables restent intacts pour qu'ils
            # puissent generer leur propre semaine.
            edt_del_qs = Emplois.objects.filter(
                annee_universitaire=annee, type_semestre=type_semestre,
            )
            if user_dept_ids is not None:
                edt_del_qs = edt_del_qs.filter(departement_id__in=user_dept_ids)
            edt_del_qs.delete()

            # Si on a utilise une autorisation de rattrapage, on la marque
            # consommee dans la meme transaction pour eviter qu'elle soit
            # reutilisee. used_at = timestamp + audit log.
            if consumed_authorization is not None:
                from core.audit_helpers import write_audit
                consumed_authorization.used_at = _tz.now()
                consumed_authorization.save(update_fields=['used_at'])
                write_audit(
                    action='UPDATE',
                    model_name='SuiviGenerationAuthorization',
                    object_id=str(consumed_authorization.pk),
                    changes={'used_at': consumed_authorization.used_at.isoformat()},
                    label=f'Rattrapage sem {numero_sem} consomme par user#{request.user.pk}',
                    keep_forever=True,
                )

        # IPGEI : horodater la semaine du calendrier. C'est cette date, comparee
        # a `SeanceReelle.modifiee_le`, qui permettra de savoir si l'emploi du
        # temps a bouge DEPUIS que le suivi en a ete tire — la divergence etait
        # jusqu'ici indetectable. Sans effet hors IPGEI.
        # IPGEI : figer l'emploi du temps qui vient de servir. Il sera modifié
        # plus tard — une correction, une permutation — et la version sur
        # laquelle les heures ont été pointées serait alors introuvable.
        try:
            from apps.ipgei.archive import archiver_semaine
            archiver_semaine(annee, type_semestre, numero_sem)
        except Exception:
            logger.exception('Archivage EDT IPGEI de la semaine %s impossible', numero_sem)

        # IPGEI : rendre aux epreuves leur VRAIE duree. La generation ci-dessus
        # lit `duree_creneau` sur le creneau — 1h30 — alors qu'un examen dure
        # 2h, 3h ou 4h et n'est ancre que sur le PREMIER creneau qu'il recouvre.
        # Sans cette reprise, il compterait 1h30 de surveillance, et l'ecran de
        # remplissage annoncerait un horaire qui n'est pas le sien.
        try:
            from apps.ipgei.miroir import corriger_durees_epreuves
            corriger_durees_epreuves(annee, type_semestre, numero_sem)
        except Exception:
            logger.exception('Duree des epreuves IPGEI, semaine %s : reprise '
                             'impossible', numero_sem)

        # IPGEI : reporter l'emargement des seances dans le registre du socle.
        # Une absence est souvent pointee AVANT que le suivi de la semaine
        # n'existe — c'est le cas d'une epreuve pointee le jour meme. Les
        # lignes de Suivi viennent d'etre creees : c'est l'instant ou ces
        # absences trouvent a quoi s'accrocher. Sans effet hors IPGEI.
        try:
            from apps.ipgei.miroir import refleter_absences
            reportees = refleter_absences(annee, type_semestre, numero_sem)
            if reportees:
                logger.info('IPGEI : %s absence(s) reportee(s) au registre du '
                            'socle pour la semaine %s', reportees, numero_sem)
        except Exception:
            logger.exception('Report des absences IPGEI de la semaine %s impossible',
                             numero_sem)

        semaine_ipgei = False
        try:
            from apps.ipgei.models import SemaineIPGEI
            from apps.ipgei.socle import SEMESTRES
            codes = [c for c, (_, t) in SEMESTRES.items() if t == type_semestre]
            semaine_ipgei = bool(SemaineIPGEI.objects.filter(
                numero=numero_sem,
                semestre__annee_universitaire=annee,
                semestre__code__in=codes,
            ).update(suivi_genere_le=_tz.now()))
        except Exception:
            logger.exception('Horodatage IPGEI de la semaine %s impossible', numero_sem)

        # Le message dit ce qui s'est passe DU POINT DE VUE DE L'UTILISATEUR.
        # « EDT archive et vide » decrit `Emplois`, une table de projection : a
        # l'IPGEI l'emploi du temps vit dans la grille et n'a pas bouge. Lu tel
        # quel, il laissait croire qu'il fallait tout ressaisir avant la
        # semaine suivante.
        if semaine_ipgei:
            message = (f"Suivi de la semaine {numero_sem} généré à partir de "
                       f"l'emploi du temps. Celui-ci reste en place.")
        else:
            message = f"Suivi semaine {numero_sem} généré. EDT archivé et vidé."

        return Response({
            'message': message,
            'suivies_created': len(suivie_objects),
            'pointages_created': len(pointage_objects),
            'authorization_used': consumed_authorization.pk if consumed_authorization else None,
        })

    def _get_default_institution_id(self) -> int:
        """Retourne l'ID de l'institution principale (NOT NULL pour Suivie/SuiviePointage)."""
        from apps.parametres.models import Institution
        principale = Institution.objects.filter(est_principale=True).first()
        if principale:
            return principale.pk
        first = Institution.objects.first()
        if first:
            return first.pk
        raise RuntimeError('Aucune institution definie. Creer au moins 1 institution avant.')

    # ── GET /api/v1/suivi/suivies/remplissage/ ────────────────────────────
    @action(detail=False, methods=['get'], url_path='remplissage')
    def remplissage(self, request):
        annee   = request.query_params.get('annee_universitaire')
        dept_id = request.query_params.get('departement')
        ts      = request.query_params.get('type_semestre')
        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)
        emplois_qs = Emplois.objects.filter(annee_universitaire=annee)
        suivies_qs = Suivie.objects.filter(annee_universitaire=annee, commentaire='Fait')
        if dept_id:
            emplois_qs = emplois_qs.filter(departement_id=dept_id)
            suivies_qs = suivies_qs.filter(departement_id=dept_id)
        if ts:
            emplois_qs = emplois_qs.filter(type_semestre=ts)
            suivies_qs = suivies_qs.filter(type_semestre=ts)
        total_emplois = emplois_qs.count()
        total_suivies = suivies_qs.values(
            'prof', 'em', 'departement', 'jour_fk', 'creneau_fk'
        ).distinct().count()
        pct = round(total_suivies / total_emplois * 100, 1) if total_emplois else 0
        return Response({
            'total_emplois': total_emplois,
            'total_suivies': total_suivies,
            'pourcentage': pct,
        })

    # ── GET /api/v1/suivi/suivies/avancement-semestres/ ───────────────────
    @action(detail=False, methods=['get'], url_path='avancement-semestres')
    def avancement_semestres(self, request):
        annee   = request.query_params.get('annee_universitaire')
        dept_id = request.query_params.get('departement')
        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)
        qs = Suivie.objects.filter(annee_universitaire=annee)
        if dept_id:
            qs = qs.filter(departement_id=dept_id)
        data = qs.values('prof__nom', 'semestre__semestre').annotate(
            heures=Sum('duree_creneau'),
            montant=Sum(F('duree_creneau') * F('taux_paiement'))
        ).order_by('prof__nom', 'semestre__semestre')
        return Response(list(data))

    # ── GET /api/v1/suivi/suivies/semaines/ ───────────────────────────────
    @action(detail=False, methods=['get'], url_path='semaines')
    def semaines(self, request):
        annee   = request.query_params.get('annee_universitaire')
        dept_id = request.query_params.get('departement')
        sem_id  = request.query_params.get('semestre')
        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)
        qs = Suivie.objects.filter(annee_universitaire=annee)
        if dept_id:
            qs = qs.filter(departement_id=dept_id)
        if sem_id:
            qs = qs.filter(semestre_id=sem_id)
        semaines = sorted(qs.values_list('numero_semaine', flat=True).distinct())
        return Response({'semaines': semaines})

    # ── GET /api/v1/suivi/suivies/grille/ ─────────────────────────────────
    @action(detail=False, methods=['get'], url_path='grille')
    def grille(self, request):
        annee   = request.query_params.get('annee_universitaire')
        dept_id = request.query_params.get('departement')
        sem_id  = request.query_params.get('semestre')
        semaine = request.query_params.get('numero_semaine')

        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)

        from apps.parametres.models import Creneau

        # Base queryset
        base_qs = Suivie.objects.filter(annee_universitaire=annee)
        if dept_id:
            base_qs = base_qs.filter(departement_id=dept_id)
        # Scope EDT : non-superuser ne voit que ses depts
        user_dept_ids = self.user_dept_ids()
        if user_dept_ids is not None:
            base_qs = base_qs.filter(departement_id__in=user_dept_ids)

        # Resoudre numero_semaine
        if semaine:
            semaine_num = int(semaine)
        else:
            latest = base_qs.aggregate(max=Max('numero_semaine'))['max']
            semaine_num = latest if latest is not None else None
        if semaine_num is None:
            return Response({'creneaux': [], 'grille': {}})

        # type_semestre du semestre selectionne
        type_semestre = None
        if sem_id:
            from apps.parametres.models import Semestre as SemestreModel
            try:
                type_semestre = SemestreModel.objects.values_list(
                    'type_semestre', flat=True).get(pk=sem_id)
            except SemestreModel.DoesNotExist:
                pass

        # Filtre semestre (inclut lignes vides semestre_id NULL si type_semestre match)
        if sem_id and type_semestre:
            filtre_semestre = (Q(semestre_id=sem_id, type_semestre=type_semestre) |
                               Q(semestre_id__isnull=True, type_semestre=type_semestre))
        elif sem_id:
            filtre_semestre = Q(semestre_id=sem_id) | Q(semestre_id__isnull=True)
        else:
            filtre_semestre = Q()

        # Creneaux colonnes : toutes les lignes de cette semaine (scope user)
        qs_cr = Suivie.objects.filter(annee_universitaire=annee, numero_semaine=semaine_num)
        if type_semestre:
            qs_cr = qs_cr.filter(type_semestre=type_semestre)
        if user_dept_ids is not None:
            qs_cr = qs_cr.filter(departement_id__in=user_dept_ids)
        cr_ids_all = set(qs_cr.exclude(creneau_fk__isnull=True)
                              .values_list('creneau_fk_id', flat=True))

        # Seances filtrees
        qs_seances = (base_qs
                      .filter(numero_semaine=semaine_num)
                      .filter(filtre_semestre)
                      .select_related(*SUIVIE_SELECT_RELATED))

        grille = defaultdict(lambda: defaultdict(list))
        for s in qs_seances:
            if not s.creneau_fk_id:
                continue
            is_spec    = bool(s.type_seance_fk and s.type_seance_fk.is_special)
            if not is_spec and not s.em_id and not s.prof_id:
                continue  # ligne vide (sauf types speciaux : pas de prof/em par design)
            jour_label = s.jour_fk.jour if s.jour_fk_id and s.jour_fk else ''
            type_label = s.type_seance_fk.type_seance if s.type_seance_fk_id and s.type_seance_fk else ''
            grille[jour_label][str(s.creneau_fk_id)].append({
                'id':                     s.pk,
                'type_seance':            type_label,
                'type_seance_is_special': is_spec,
                'prof_nom':               s.prof.nom if s.prof else None,
                'em_code':                s.em.code_em if s.em else None,
                'em_intitule':            s.em.intitule if s.em else None,
                'salle_nom':              s.salle.nom if s.salle else None,
                'commentaire':            s.commentaire,
                'numero_semaine':         s.numero_semaine,
            })

        creneaux_used = list(
            Creneau.objects.filter(pk__in=cr_ids_all)
                           .order_by('ordre').values('id', 'creneau', 'ordre')
        )

        return Response({
            'creneaux': creneaux_used,
            'grille':   {jour: dict(crs) for jour, crs in grille.items()},
        })

    # ── GET /api/v1/suivi/suivies/pdf/ ────────────────────────────────────
    @action(detail=False, methods=['get'], url_path='pdf')
    def pdf(self, request):
        """Genere le PDF de l'emploi du temps d'une filiere (format GesAFPED, via wkhtmltopdf)."""
        import pdfkit
        from apps.departement.models import Departement
        from apps.parametres.models import Semestre as SemestreModel, Creneau, Jour, Semaine
        from django.utils.formats import date_format as dformat

        try:
            locale.setlocale(locale.LC_TIME, 'fr_FR.UTF-8')
        except locale.Error:
            pass

        annee         = request.query_params.get('annee_universitaire', '')
        dept_id       = request.query_params.get('departement', '')
        semestre_id   = request.query_params.get('semestre', '')
        semaine_param = request.query_params.get('numero_semaine', '')

        if not all([annee, dept_id, semestre_id]):
            return Response(
                {'error': 'annee_universitaire, departement et semestre sont requis.'},
                status=400,
            )

        try:
            dept     = Departement.objects.get(pk=dept_id)
            semestre = SemestreModel.objects.get(pk=semestre_id)
        except (Departement.DoesNotExist, SemestreModel.DoesNotExist):
            return Response({'error': 'Departement ou semestre introuvable.'}, status=404)

        # Resolution numero_semaine (toujours en GLOBAL en BD)
        base_qs = Suivie.objects.filter(
            annee_universitaire=annee, departement_id=dept_id, semestre_id=semestre_id,
        )
        if semaine_param:
            semaine_num = int(semaine_param)
        else:
            semaine_num = base_qs.aggregate(max=Max('numero_semaine'))['max']
        if semaine_num is None:
            return Response({'error': 'Aucun suivi trouve pour ce departement.'}, status=404)

        # Calcul du numero pedagogique LOCAL au dept (applique le decalage L1)
        # Le decalage ne s'applique qu'au semestre Impair.
        type_semestre = semestre.type_semestre
        from apps.departement.semaine_helpers import numero_pedagogique_dept
        semaine_pedagogique = numero_pedagogique_dept(semaine_num, dept, type_semestre)
        if semaine_pedagogique is None:
            # Lire le decalage actif du semestre concerne pour message clair
            dec_actif = (dept.decalage_impair if type_semestre == 'I'
                         else dept.decalage_pair) or 0
            sem_label = 'Impair' if type_semestre == 'I' else 'Pair'
            return Response({
                'error': (
                    f"La semaine {semaine_num} n'est pas applicable au departement "
                    f"{dept.nom} : elle precede le demarrage des cours pour ce groupe "
                    f"(decalage {sem_label} = {dec_actif} semaines). "
                    f"Choisissez une semaine >= {dec_actif + 1}."
                )
            }, status=400)
        semaine_a_decalage = semaine_pedagogique != semaine_num

        # Dates debut/fin de la semaine
        semaine_dates  = Semaine.objects.filter(
            numero_semaine=semaine_num, annee_universitaire=annee, type_semestre=type_semestre,
        ).order_by('date')
        debut_formate = fin_formate = ''
        if semaine_dates.exists():
            try:
                debut_formate = dformat(semaine_dates.first().date, format='d F', use_l10n=True)
                fin_formate   = dformat(semaine_dates.last().date,  format='d F', use_l10n=True)
            except Exception:
                debut_formate = semaine_dates.first().date.strftime('%d %B')
                fin_formate   = semaine_dates.last().date.strftime('%d %B')

        # Seances de cette semaine
        qs_seances = (base_qs.filter(numero_semaine=semaine_num)
                            .select_related(*SUIVIE_SELECT_RELATED))

        # Creneaux colonnes (presents dans la semaine, tous depts)
        qs_cr = Suivie.objects.filter(
            annee_universitaire=annee, numero_semaine=semaine_num, type_semestre=type_semestre,
        )
        cr_ids = set(qs_cr.exclude(creneau_fk__isnull=True)
                          .values_list('creneau_fk_id', flat=True))
        creneaux = list(Creneau.objects.filter(pk__in=cr_ids).order_by('ordre'))
        jours    = list(Jour.objects.all().order_by('id'))

        # Construction de la grille
        grille_data = defaultdict(lambda: defaultdict(list))
        for s in qs_seances:
            if not s.creneau_fk_id:
                continue
            is_spec    = bool(s.type_seance_fk and s.type_seance_fk.is_special)
            if not is_spec and not s.em_id and not s.prof_id:
                continue
            jour_label = s.jour_fk.jour if s.jour_fk_id and s.jour_fk else ''
            type_label = s.type_seance_fk.type_seance if s.type_seance_fk_id and s.type_seance_fk else ''
            grille_data[jour_label][s.creneau_fk_id].append({
                'type_seance':            type_label,
                'type_seance_is_special': is_spec,
                'prof_nom':               s.prof.nom if s.prof else '',
                'em_code':                s.em.code_em if s.em else '',
                'em_intitule':            s.em.intitule if s.em else '',
                'salle_nom':              s.salle.nom if s.salle else '',
            })

        rows = [
            {'jour': j.jour, 'cells': [grille_data[j.jour][cr.id] for cr in creneaux]}
            for j in jours
        ]

        from django.conf import settings
        from core.pdf_utils import get_institution_context
        font_path = settings.BASE_DIR / 'static' / 'fonts' / 'Cairo.ttf'
        font_url  = 'file:///' + str(font_path).replace('\\', '/')
        _inst_ctx = get_institution_context()

        # Libelle du departement pour l'entete PDF : "Filiere Niveau dept.nom"
        # (ex. "Statistiques L1 SEA L1 - G1"). dept.nom contient deja l'info
        # groupe ("SEA L1 - G1"). Fallback sur dept.description ou dept.nom
        # si filiere/niveau absents (cas transversaux HE/ST).
        dept_parts = []
        if dept.filiere_id and dept.filiere:
            dept_parts.append(dept.filiere.intitule_fr or '')
        if dept.niveau_id and dept.niveau:
            dept_parts.append(dept.niveau.niveau or '')
        if dept.nom:
            dept_parts.append(dept.nom)
        dept_label = ' '.join(p for p in dept_parts if p) or dept.description or dept.nom

        context = {
            **_inst_ctx,
            'annee_universitaire': annee,
            'departement':        dept_label,
            'semestre_nom':       semestre.semestre,
            'semaine':            semaine_pedagogique,   # affichage principal (numero local)
            'semaine_globale':    semaine_num,            # numero global (info secondaire)
            'semaine_a_decalage': semaine_a_decalage,    # True si dept a un decalage
            'debut':              debut_formate,
            'fin':                fin_formate,
            'creneaux':           creneaux,
            'rows':               rows,
            'font_url':           font_url,
        }

        html_string = render_to_string('emploi_filiere_pdf.html', context)
        config = pdfkit.configuration(wkhtmltopdf=r'C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe')
        options = {
            'margin-top': '0.25in', 'margin-right': '0.25in',
            'margin-bottom': '0.25in', 'margin-left': '0.25in',
            'orientation': 'Landscape', 'page-size': 'A4',
            'dpi': '300', 'image-dpi': '300', 'image-quality': '100',
            'enable-local-file-access': '', 'print-media-type': '', 'no-stop-slow-scripts': '',
            'quiet': '',  # supprime les warnings wkhtmltopdf qui peuvent faire echouer pdfkit
        }

        try:
            pdf_bytes = pdfkit.from_string(html_string, False, configuration=config, options=options)
        except Exception as exc:
            logger.error('PDF generation failed: %s', exc)
            return Response({'error': 'Erreur lors de la generation du PDF.'}, status=500)

        # Nom du fichier : Emplois_{filiere}_{niveau}_{nom}_S{semaine}.pdf
        # (les portions vides sont filtrees, espaces et tirets normalises en _)
        import re
        def _slug(s: str) -> str:
            return re.sub(r'_+', '_', re.sub(r'[^\w]', '', re.sub(r'[\s\-]+', '_', s))).strip('_')
        fn_parts = ['Emplois']
        if dept.filiere_id and dept.filiere and dept.filiere.intitule_fr:
            fn_parts.append(_slug(dept.filiere.intitule_fr))
        if dept.niveau_id and dept.niveau and dept.niveau.niveau:
            fn_parts.append(_slug(dept.niveau.niveau))
        if dept.nom:
            fn_parts.append(_slug(dept.nom))
        fn_parts = [p for p in fn_parts if p]
        filename = '_'.join(fn_parts) + f"_S{semaine_num}.pdf"
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


#: Les types de seance qui sont des EPREUVES. Elles n'ont ni salle ni
#: surveillant : la repartition en salles se decide sur les fiches, et le
#: surveillant releve de la vacation. Les feuilles de presence les admettent
#: donc sans salle, ce qu'elles refusent a une seance de cours.
TYPES_EPREUVE = ('DS', 'EF', 'ER')


class SuiviePointageViewSet(InstitutionScopedMixin, DepartementScopedMixin, AuditMixin, viewsets.ModelViewSet):
    # SuiviePointage utilise M2M `departements`
    departement_filter_field  = 'departements'
    departement_filter_lookup = 'in'
    queryset = SuiviePointage.objects.select_related(
        *POINTAGE_SELECT_RELATED
    ).prefetch_related('departements').all()
    serializer_class   = SuiviePointageSerializer
    permission_classes = [RBACPermission, EDTDepartementPermission]
    required_module    = 'suivi_fiches'
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['annee_universitaire', 'prof', 'numero_semaine', 'type_semestre']
    ordering_fields    = ['numero_semaine', 'date_suivie']
    # Ordre PAR DÉFAUT : sans lui, PostgreSQL ne garantit rien d'une page à
    # l'autre — une séance apparaissait en page 1 et en page 2, une autre sur
    # aucune. `ordering_fields` n'offre que des options au client. Le `pk`
    # final départage les ex æquo.
    ordering           = ['-annee_universitaire', 'numero_semaine', 'pk']
    pagination_class   = StandardPagination

    def get_permissions(self):
        if self.action in ('grille', 'semaines', 'reclamer', 'ems_prof'):
            return [IsAuthenticated()]
        return super().get_permissions()

    # ── Nombre de semestres actifs (carte « Emplois » du tableau de bord) ────
    @action(detail=False, methods=['get'], url_path='semestres-actifs')
    def semestres_actifs(self, request):
        """
        Nombre de semestres distincts présents dans le pointage pour une année
        universitaire et une parité (type_semestre 'I'/'P' = session du contexte).
        Scopé institution/département via get_queryset. Ex. 2025-2026 Impairs → 3
        (S1/S3/S5), Pairs → 2 (S2/S4).
        """
        annee = request.query_params.get('annee_universitaire')
        ts    = request.query_params.get('type_semestre')
        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)
        qs = self.get_queryset().filter(annee_universitaire=annee)
        if ts:
            qs = qs.filter(type_semestre=ts)
        count = qs.values('semestre').distinct().count()
        return Response({'annee_universitaire': annee, 'type_semestre': ts or '', 'count': count})

    # ── GET /api/v1/suivi/pointages/semaines/ ─────────────────────────────
    @action(detail=False, methods=['get'], url_path='semaines')
    def semaines(self, request):
        from apps.parametres.models import Semaine as SemaineParam
        annee   = request.query_params.get('annee_universitaire')
        ts      = request.query_params.get('type_semestre')
        prof_id = request.query_params.get('prof')   # optionnel : portail enseignant
        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)

        qs = SuiviePointage.objects.filter(annee_universitaire=annee)
        if ts:
            qs = qs.filter(type_semestre=ts)
        # Scope optionnel par prof : le portail enseignant ne liste alors que les
        # semaines ou le prof a des seances (param absent ailleurs = inchange).
        if prof_id:
            qs = qs.filter(prof_id=prof_id)
        numeros = sorted(qs.values_list('numero_semaine', flat=True).distinct())

        dates_qs = SemaineParam.objects.filter(annee_universitaire=annee)
        if ts:
            dates_qs = dates_qs.filter(type_semestre=ts)
        dates_map = {}
        for row in dates_qs.values('numero_semaine').annotate(debut=Min('date'), fin=Max('date')):
            dates_map[row['numero_semaine']] = {
                'debut': row['debut'].strftime('%d/%m/%Y') if row['debut'] else '',
                'fin':   row['fin'].strftime('%d/%m/%Y')   if row['fin']   else '',
            }
        return Response({'semaines': numeros, 'semaines_dates': dates_map})

    # ── GET /api/v1/suivi/pointages/grille/ ───────────────────────────────
    @action(detail=False, methods=['get'], url_path='grille')
    def grille(self, request):
        annee    = request.query_params.get('annee_universitaire')
        salle_id = request.query_params.get('salle')
        prof_id  = request.query_params.get('prof')
        semaine  = request.query_params.get('numero_semaine')
        ts       = request.query_params.get('type_semestre')

        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)

        from apps.parametres.models import Creneau

        # Resolution numero_semaine
        if semaine:
            semaine_num = int(semaine)
        else:
            qs_latest = SuiviePointage.objects.filter(annee_universitaire=annee)
            if ts:
                qs_latest = qs_latest.filter(type_semestre=ts)
            # Consultation ciblee (portail enseignant / salle) : la "derniere
            # semaine" par defaut doit etre celle ou CETTE cible a effectivement
            # des seances. Sinon on tombe sur la derniere semaine globale (ex.
            # semaine d'examens) ou ce prof n'a rien -> premier ecran vide.
            if prof_id:
                qs_latest = qs_latest.filter(prof_id=prof_id)
            elif salle_id:
                qs_latest = qs_latest.filter(salle_id=salle_id)
            latest = qs_latest.aggregate(max=Max('numero_semaine'))['max']
            if latest is None:
                return Response({'creneaux': [], 'grille': {}})
            semaine_num = latest

        # Scope EDT : non-superuser ne voit que les pointages touchant ses depts.
        # EXCEPTION : un enseignant qui consulte SON PROPRE emploi (prof ==
        # son profil) est legitime a voir toutes ses seances, quels que soient
        # les departements. Il n'a aucun managed_departements, donc sans cette
        # exception sa grille serait toujours vide (departements__in=[] => 0
        # ligne). Le filtre `prof` constitue alors l'autorisation. Pour toute
        # autre cible (autre prof, salle, vue globale), le scope dept reste.
        user_dept_ids = self.user_dept_ids()
        own_prof_id = getattr(getattr(request.user, 'prof_profile', None), 'pk', None)
        is_self_view = bool(prof_id and own_prof_id and str(prof_id) == str(own_prof_id))
        scope_dept_ids = None if is_self_view else user_dept_ids

        # Creneaux colonnes
        qs_cr = SuiviePointage.objects.filter(annee_universitaire=annee, numero_semaine=semaine_num)
        if ts:
            qs_cr = qs_cr.filter(type_semestre=ts)
        if scope_dept_ids is not None:
            qs_cr = qs_cr.filter(departements__in=scope_dept_ids).distinct()
        cr_ids_all = set(qs_cr.exclude(creneau_fk__isnull=True)
                              .values_list('creneau_fk_id', flat=True))

        # Seances filtrees
        qs_seances = (SuiviePointage.objects
                      .filter(annee_universitaire=annee, numero_semaine=semaine_num)
                      .select_related(*POINTAGE_SELECT_RELATED)
                      .prefetch_related('departements'))
        if salle_id:
            qs_seances = qs_seances.filter(salle_id=salle_id)
        if prof_id:
            qs_seances = qs_seances.filter(prof_id=prof_id)
        if ts:
            qs_seances = qs_seances.filter(type_semestre=ts)
        if scope_dept_ids is not None:
            qs_seances = qs_seances.filter(departements__in=scope_dept_ids).distinct()

        grille = defaultdict(lambda: defaultdict(list))
        for sp in qs_seances:
            if not sp.creneau_fk_id:
                continue
            is_spec    = bool(sp.type_seance_fk and sp.type_seance_fk.is_special)
            if not is_spec and not sp.em_id and not sp.prof_id:
                continue
            jour_label = sp.jour_fk.jour if sp.jour_fk_id and sp.jour_fk else ''
            type_label = sp.type_seance_fk.type_seance if sp.type_seance_fk_id and sp.type_seance_fk else ''
            dept_noms = sorted(d.nom for d in sp.departements.all() if d.nom)
            grille[jour_label][str(sp.creneau_fk_id)].append({
                'id':                     sp.pk,
                'type_seance':            type_label,
                'type_seance_is_special': is_spec,
                'prof_nom':               sp.prof.nom if sp.prof else None,
                'em_code':                sp.em.code_em if sp.em else None,
                'em_intitule':            sp.em.intitule if sp.em else None,
                'salle_nom':              sp.salle.nom if sp.salle else None,
                'dept_noms':              dept_noms,
                'commentaire':            sp.commentaire,
                'numero_semaine':         sp.numero_semaine,
                'reclamation_statut':     sp.reclamation_statut,
            })

        creneaux_used = list(
            Creneau.objects.filter(pk__in=cr_ids_all)
                           .order_by('ordre').values('id', 'creneau', 'ordre')
        )
        return Response({
            'creneaux': creneaux_used,
            'grille':   {jour: dict(crs) for jour, crs in grille.items()},
        })

    # ── GET /api/v1/suivi/pointages/pdf-salle/ ────────────────────────────
    @action(detail=False, methods=['get'], url_path='pdf-salle')
    def pdf_salle(self, request):
        """PDF emploi du temps d'une salle (reuse template emploi_filiere_pdf.html)."""
        return self._pdf_pointage(request, mode='salle')

    # ── GET /api/v1/suivi/pointages/pdf-prof/ ─────────────────────────────
    @action(detail=False, methods=['get'], url_path='pdf-prof')
    def pdf_prof(self, request):
        """PDF emploi du temps d'un prof (reuse template emploi_filiere_pdf.html)."""
        return self._pdf_pointage(request, mode='prof')

    def _pdf_pointage(self, request, mode):
        """Helper partage : genere le PDF d'une grille SuiviePointage filtree par
        salle ou prof, en utilisant le meme template que emplois/filiere."""
        import pdfkit
        from apps.parametres.models import Creneau, Jour, Semaine
        from apps.salle.models import Salle
        from apps.prof.models import Prof
        from django.utils.formats import date_format as dformat

        try:
            locale.setlocale(locale.LC_TIME, 'fr_FR.UTF-8')
        except locale.Error:
            pass

        annee   = request.query_params.get('annee_universitaire', '')
        ts      = request.query_params.get('type_semestre')
        target  = request.query_params.get(mode, '')  # 'salle' ou 'prof'
        semaine = request.query_params.get('numero_semaine', '')

        if not annee or not target:
            return Response(
                {'error': f'annee_universitaire et {mode} sont requis.'},
                status=400,
            )

        # Resolution de l'objet cible (pour le titre)
        if mode == 'salle':
            try:
                obj = Salle.objects.get(pk=target)
                target_label = obj.nom
            except Salle.DoesNotExist:
                return Response({'error': 'Salle introuvable.'}, status=404)
        else:
            try:
                obj = Prof.objects.get(pk=target)
                target_label = obj.nom
            except Prof.DoesNotExist:
                return Response({'error': 'Professeur introuvable.'}, status=404)

        # Resolution numero_semaine
        base_qs = SuiviePointage.objects.filter(annee_universitaire=annee)
        if ts:
            base_qs = base_qs.filter(type_semestre=ts)
        if mode == 'salle':
            base_qs = base_qs.filter(salle_id=target)
        else:
            base_qs = base_qs.filter(prof_id=target)

        if semaine:
            semaine_num = int(semaine)
        else:
            semaine_num = base_qs.aggregate(max=Max('numero_semaine'))['max']
        if semaine_num is None:
            return Response({'error': 'Aucun pointage trouve pour cette cible.'}, status=404)

        # Type semestre pour les dates de la semaine
        if not ts:
            ts = base_qs.filter(numero_semaine=semaine_num).values_list(
                'type_semestre', flat=True).first() or ''

        # Dates debut/fin
        debut_formate = fin_formate = ''
        if ts:
            semaine_dates = Semaine.objects.filter(
                numero_semaine=semaine_num, annee_universitaire=annee, type_semestre=ts,
            ).order_by('date')
            if semaine_dates.exists():
                try:
                    debut_formate = dformat(semaine_dates.first().date, format='d F', use_l10n=True)
                    fin_formate   = dformat(semaine_dates.last().date,  format='d F', use_l10n=True)
                except Exception:
                    debut_formate = semaine_dates.first().date.strftime('%d %B')
                    fin_formate   = semaine_dates.last().date.strftime('%d %B')

        # Seances de cette semaine
        qs_seances = (base_qs.filter(numero_semaine=semaine_num)
                             .select_related(*POINTAGE_SELECT_RELATED))

        # Creneaux colonnes
        cr_qs = SuiviePointage.objects.filter(annee_universitaire=annee, numero_semaine=semaine_num)
        if ts:
            cr_qs = cr_qs.filter(type_semestre=ts)
        cr_ids = set(cr_qs.exclude(creneau_fk__isnull=True)
                          .values_list('creneau_fk_id', flat=True))
        creneaux = list(Creneau.objects.filter(pk__in=cr_ids).order_by('ordre'))
        jours    = list(Jour.objects.all().order_by('id'))

        # Construction de la grille — on inclut dept_noms pour afficher les filieres
        # concernees dans chaque cellule (utile pour les vues salle/prof partagees).
        qs_seances = qs_seances.prefetch_related('departements__filiere', 'departements__niveau')
        grille_data = defaultdict(lambda: defaultdict(list))

        def _dept_label(d):
            # « FILIERE - NIVEAU - GROUPE » (ex. LPSTAT - L1 - G1) au lieu du seul nom.
            return ' - '.join(x for x in [
                d.filiere.code   if d.filiere_id and d.filiere else None,
                d.niveau.niveau  if d.niveau_id  and d.niveau  else None,
                d.nom,
            ] if x)

        for sp in qs_seances:
            if not sp.creneau_fk_id:
                continue
            is_spec    = bool(sp.type_seance_fk and sp.type_seance_fk.is_special)
            if not is_spec and not sp.em_id and not sp.prof_id:
                continue
            jour_label = sp.jour_fk.jour if sp.jour_fk_id and sp.jour_fk else ''
            type_label = sp.type_seance_fk.type_seance if sp.type_seance_fk_id and sp.type_seance_fk else ''
            dept_noms  = sorted(_dept_label(d) for d in sp.departements.all() if d.nom)
            grille_data[jour_label][sp.creneau_fk_id].append({
                'type_seance':            type_label,
                'type_seance_is_special': is_spec,
                'prof_nom':               sp.prof.nom if sp.prof else '',
                'em_code':                sp.em.code_em if sp.em else '',
                'em_intitule':            sp.em.intitule if sp.em else '',
                'salle_nom':              sp.salle.nom if sp.salle else '',
                'dept_noms':              dept_noms,
            })

        rows = [
            {'jour': j.jour, 'cells': [grille_data[j.jour][cr.id] for cr in creneaux]}
            for j in jours
        ]

        from core.pdf_utils import get_institution_context
        _inst_ctx = get_institution_context()

        # Template + libelle de titre adaptes au mode
        if mode == 'salle':
            template_name = 'emploi_salle_pdf.html'
            title_label   = target_label  # nom de la salle
        else:
            template_name = 'emploi_prof_pdf.html'
            title_label   = target_label  # nom du prof

        context = {
            **_inst_ctx,
            'annee_universitaire': annee,
            'cible':        title_label,    # libelle dynamique (salle/prof)
            'departement':  title_label,    # compatibilite ascendante avec le template filiere
            'semestre_nom': '',
            'semaine':      semaine_num,
            'debut':        debut_formate,
            'fin':          fin_formate,
            'creneaux':     creneaux,
            'rows':         rows,
        }

        html_string = render_to_string(template_name, context)
        config = pdfkit.configuration(wkhtmltopdf=r'C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe')
        options = {
            'margin-top': '0.25in', 'margin-right': '0.25in',
            'margin-bottom': '0.25in', 'margin-left': '0.25in',
            'orientation': 'Landscape', 'page-size': 'A4',
            'dpi': '300', 'image-dpi': '300', 'image-quality': '100',
            'enable-local-file-access': '', 'print-media-type': '', 'no-stop-slow-scripts': '',
            'quiet': '',
        }

        try:
            pdf_bytes = pdfkit.from_string(html_string, False, configuration=config, options=options)
        except Exception as exc:
            logger.error('PDF generation failed (%s): %s', mode, exc)
            return Response({'error': 'Erreur lors de la generation du PDF.'}, status=500)

        slug = target_label.replace(' ', '_').replace('/', '_')
        filename = f"emploi_{mode}_{slug}_sem{semaine_num}.pdf"
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # ── PATCH /api/v1/suivi/pointages/{id}/toggle/ ────────────────────────
    @action(detail=True, methods=['patch'], url_path='toggle')
    def toggle_commentaire(self, request, pk=None):
        sp = self.get_object()
        sp.commentaire = 'Fait' if sp.commentaire == 'Non fait' else 'Non fait'
        sp.save(update_fields=['commentaire'])
        return Response({'id': sp.pk, 'commentaire': sp.commentaire})

    # ── POST /api/v1/suivi/pointages/bulk-update/ ─────────────────────────
    @action(detail=False, methods=['post'], url_path='bulk-update')
    @audit_aggregate(label='Mise à jour bulk pointages', action='BULK_UPDATE',
                     model_name='SuiviePointage')
    def bulk_update_commentaires(self, request):
        updates = request.data.get('updates', [])
        if not updates:
            return Response({'error': 'updates requis.'}, status=400)
        ids = [u['id'] for u in updates if 'id' in u and 'commentaire' in u]

        # Scope EDT : ne charger que les pointages dont AU MOINS UN dept
        # est dans le perimetre du user. Les ids hors perimetre sont
        # silencieusement ignores (pas de fuite info sur leur existence).
        sps_qs = SuiviePointage.objects.filter(pk__in=ids)
        user_dept_ids = self.user_dept_ids()
        if user_dept_ids is not None:   # non-superuser
            if not user_dept_ids:
                return Response(
                    {'error': "Aucun groupe ne vous est attribue."},
                    status=403,
                )
            # M2M sur departements : on garde les pointages touchant au moins
            # 1 dept du perimetre. .distinct() pour eviter les doublons dus
            # au join M2M.
            allowed_ids = list(
                sps_qs.filter(departements__in=user_dept_ids)
                      .values_list('pk', flat=True).distinct()
            )
            sps_qs = SuiviePointage.objects.filter(pk__in=allowed_ids)

        sps = {sp.pk: sp for sp in sps_qs}
        # Capture des anciens commentaires AVANT modification (pour audit per-row).
        old_commentaires = {pk: sp.commentaire for pk, sp in sps.items()}
        to_update = []
        for u in updates:
            sp = sps.get(u.get('id'))
            if sp and u.get('commentaire') in ('Fait', 'Non fait', 'Reporté'):
                sp.commentaire = u['commentaire']
                to_update.append(sp)
        if to_update:
            SuiviePointage.objects.bulk_update(to_update, ['commentaire'])
            # Audit per-row : bulk_update ne declenche pas de signal. Sans ce
            # log, le drawer historique de chaque pointage afficherait juste
            # l'aggregat BULK_UPDATE sans pouvoir remonter au detail.
            audit_items = [
                {
                    'object_id': sp.pk,
                    'institution_id': sp.institution_id,
                    'changes': {
                        'commentaire': {
                            'old': old_commentaires.get(sp.pk),
                            'new': sp.commentaire,
                        },
                    },
                }
                for sp in to_update
                if old_commentaires.get(sp.pk) != sp.commentaire
            ]
            _audit_bulk_safe(
                'SuiviePointage', 'UPDATE', audit_items,
                label='Pointage modifie (bulk)',
                request=request,
            )
        return Response({'updated': len(to_update)})

    # ── GET /api/v1/suivi/pointages/ems-prof/ ─────────────────────────────
    @action(detail=False, methods=['get'], url_path='ems-prof')
    def ems_prof(self, request):
        from apps.em.models import EM
        annee = request.query_params.get('annee_universitaire')
        ts    = request.query_params.get('type_semestre')
        try:
            prof = request.user.prof_profile
        except Exception:
            return Response({'error': 'Profil enseignant introuvable.'}, status=403)
        qs = SuiviePointage.objects.filter(prof_id=prof.pk)
        if annee:
            qs = qs.filter(annee_universitaire=annee)
        if ts:
            qs = qs.filter(type_semestre=ts)
        em_ids = qs.values_list('em_id', flat=True).distinct()
        ems = list(EM.objects.filter(pk__in=em_ids).order_by('code_em')
                   .values('id', 'code_em', 'intitule'))
        return Response(ems)

    # ── POST /api/v1/suivi/pointages/{id}/reclamer/ ───────────────────────
    @action(detail=True, methods=['post'], url_path='reclamer')
    def reclamer(self, request, pk=None):
        sp = self.get_object()
        try:
            prof = request.user.prof_profile
        except Exception:
            return Response({'detail': 'Profil enseignant introuvable.'}, status=status.HTTP_403_FORBIDDEN)
        if sp.prof_id != prof.pk:
            return Response({'detail': 'Acces non autorise.'}, status=status.HTTP_403_FORBIDDEN)

        motif = (request.data.get('motif') or '').strip()
        if not motif:
            return Response({'detail': 'Le motif est obligatoire.'}, status=status.HTTP_400_BAD_REQUEST)
        if sp.reclamation_statut in ('en_attente', 'acceptee'):
            return Response({'detail': 'Une reclamation est deja en cours ou acceptee.'},
                            status=status.HTTP_400_BAD_REQUEST)

        sp.reclamation_motif  = motif
        sp.reclamation_statut = 'en_attente'
        sp.save(update_fields=['reclamation_motif', 'reclamation_statut'])
        logger.info('Reclamation deposee par prof %s sur pointage %s', prof.pk, sp.pk)
        return Response({'detail': 'Reclamation envoyee avec succes.'})

    # ── GET /api/v1/suivi/pointages/fiches/ ───────────────────────────────
    @action(detail=False, methods=['get'], url_path='fiches')
    def fiches(self, request):
        """Fiches de presence pour une semaine/semestre donnes."""
        annee    = request.query_params.get('annee_universitaire')
        semaine  = request.query_params.get('numero_semaine')
        sem_code = request.query_params.get('id_semestre')        # optionnel
        ts       = request.query_params.get('type_semestre')

        if not all([annee, semaine]):
            return Response({'error': 'annee_universitaire et numero_semaine requis.'}, status=400)

        qs = (SuiviePointage.objects
              .filter(annee_universitaire=annee, numero_semaine=semaine)
              .filter(type_seance_fk__isnull=False)
              .select_related(*POINTAGE_SELECT_RELATED)
              .prefetch_related('departements__filiere', 'departements__niveau'))
        if ts:
            qs = qs.filter(type_semestre=ts)
        if sem_code:
            qs = qs.filter(semestre__code_semestre=sem_code)

        jours_order = {'Lundi': 1, 'Mardi': 2, 'Mercredi': 3, 'Jeudi': 4, 'Vendredi': 5, 'Samedi': 6}
        # Compte les filieres UNE fois pour tout le lot : la question ne depend
        # pas de la ligne, et la poser a chacune ferait une requete par seance.
        filiere_utile = filiere_distingue()
        result = []
        for sp in qs.order_by('date_suivie', 'creneau_fk__ordre'):
            ts_label = sp.type_seance_fk.type_seance if sp.type_seance_fk_id and sp.type_seance_fk else ''
            if not ts_label:
                continue
            # La salle reste EXIGÉE — sauf pour une épreuve, qui n'en a pas.
            #
            # Le surveillant relève de la vacation, et la répartition en salles
            # se décide sur les fiches de répartition, plus tard. Exiger une
            # salle faisait disparaître le devoir de la feuille où on le pointe,
            # alors que c'est celle qui circule le jour de l'examen.
            #
            # L'exception est nommément limitée aux trois types d'épreuve : une
            # SÉANCE sans salle reste écartée, comme avant. Une ligne de cours
            # incomplète n'a rien à faire sur une feuille de présence.
            #
            # Et les lignes vides que la génération pose sur chaque créneau
            # libre n'ont pas d'EM : elles ne passent toujours pas.
            if not sp.em_id:
                continue
            if not sp.salle_id and ts_label not in TYPES_EPREUVE:
                continue
            jour_label = sp.jour_fk.jour if sp.jour_fk_id and sp.jour_fk else ''
            result.append({
                'id':           sp.pk,
                'jour':         jour_label,
                'date_suivie':  str(sp.date_suivie) if sp.date_suivie else None,
                # L'horaire REEL : une epreuve est ancree sur le PREMIER creneau
                # qu'elle recouvre, et le creneau seul annoncerait « 08h00-09h30 »
                # pour un examen qui court jusqu'a midi.
                'creneau':      horaire_affiche(
                    sp.creneau_fk.creneau if sp.creneau_fk_id and sp.creneau_fk else '',
                    sp.duree_creneau),
                'prof_nom':     sp.prof.nom if sp.prof else '',
                'em_intitule':  sp.em.intitule if sp.em else '',
                'type_seance':  ts_label,
                'salle_nom':    sp.salle.nom if sp.salle else '',
                'commentaire':  sp.commentaire,
                'departement':  format_depts_compact(sp, filiere_utile),
                'semestre':     sp.semestre.semestre if sp.semestre else '',
            })

        by_jour = defaultdict(list)
        for r in result:
            by_jour[r['jour']].append(r)
        jours_sorted = sorted(by_jour.keys(), key=lambda j: jours_order.get(j, 99))

        return Response([
            {
                'jour': jour,
                'date': by_jour[jour][0]['date_suivie'] if by_jour[jour] else None,
                'fiches': sorted(by_jour[jour], key=lambda f: f['creneau']),
            }
            for jour in jours_sorted
        ])

    # ── Helpers PDF ──────────────────────────────────────────────────────
    def _build_fiches_par_jour(self, annee, semaine, ts, sem_code=None):
        """Liste [{jour, date_jour, fiches: [...]}, ...] triee par jour."""
        qs = (SuiviePointage.objects
              .filter(annee_universitaire=annee, numero_semaine=semaine)
              .filter(type_seance_fk__isnull=False)
              .select_related(*POINTAGE_SELECT_RELATED)
              .prefetch_related('departements__filiere', 'departements__niveau'))
        if ts:
            qs = qs.filter(type_semestre=ts)
        if sem_code:
            qs = qs.filter(semestre__code_semestre=sem_code)

        jours_order = {'Lundi': 1, 'Mardi': 2, 'Mercredi': 3, 'Jeudi': 4, 'Vendredi': 5, 'Samedi': 6}
        # Compte les filieres UNE fois pour tout le lot : la question ne depend
        # pas de la ligne, et la poser a chacune ferait une requete par seance.
        filiere_utile = filiere_distingue()
        rows = []
        for sp in qs.order_by('date_suivie', 'creneau_fk__ordre'):
            ts_label = sp.type_seance_fk.type_seance if sp.type_seance_fk_id and sp.type_seance_fk else ''
            if not ts_label:
                continue
            # La salle reste EXIGÉE — sauf pour une épreuve, qui n'en a pas.
            #
            # Le surveillant relève de la vacation, et la répartition en salles
            # se décide sur les fiches de répartition, plus tard. Exiger une
            # salle faisait disparaître le devoir de la feuille où on le pointe,
            # alors que c'est celle qui circule le jour de l'examen.
            #
            # L'exception est nommément limitée aux trois types d'épreuve : une
            # SÉANCE sans salle reste écartée, comme avant. Une ligne de cours
            # incomplète n'a rien à faire sur une feuille de présence.
            #
            # Et les lignes vides que la génération pose sur chaque créneau
            # libre n'ont pas d'EM : elles ne passent toujours pas.
            if not sp.em_id:
                continue
            if not sp.salle_id and ts_label not in TYPES_EPREUVE:
                continue
            jour_label = sp.jour_fk.jour if sp.jour_fk_id and sp.jour_fk else ''
            rows.append({
                'jour':        jour_label,
                'date_suivie': str(sp.date_suivie) if sp.date_suivie else None,
                # L'horaire REEL : une epreuve est ancree sur le PREMIER creneau
                # qu'elle recouvre, et le creneau seul annoncerait « 08h00-09h30 »
                # pour un examen qui court jusqu'a midi.
                'creneau':     horaire_affiche(
                    sp.creneau_fk.creneau if sp.creneau_fk_id and sp.creneau_fk else '',
                    sp.duree_creneau),
                'prof_nom':    sp.prof.nom if sp.prof else '',
                'tel_prof':    str(sp.prof.telephone) if (sp.prof and sp.prof.telephone) else '',
                'em_intitule': sp.em.intitule if sp.em else '',
                'type_seance': ts_label,
                'salle_nom':   sp.salle.nom if sp.salle else '',
                'departement': format_depts_compact(sp, filiere_utile),
                'semestre':    sp.semestre.semestre if sp.semestre else '',
            })

        by_jour = defaultdict(list)
        for r in rows:
            by_jour[r['jour']].append(r)

        result = []
        for jour in sorted(by_jour.keys(), key=lambda j: jours_order.get(j, 99)):
            fiches_jour = sorted(by_jour[jour], key=lambda f: f['creneau'])
            date_jour = fiches_jour[0]['date_suivie'] if fiches_jour else None
            if date_jour:
                try:
                    parts = date_jour.split('-')
                    date_jour = f"{parts[2]}/{parts[1]}/{parts[0]}"
                except Exception:
                    pass
            result.append({'jour': jour, 'date_jour': date_jour, 'fiches': fiches_jour})
        return result

    def _generate_pdf(self, template_name, context, filename):
        """Rend le template HTML et genere un PDF via pdfkit/wkhtmltopdf."""
        try:
            import pdfkit
        except ImportError:
            return Response({'error': 'pdfkit non installe. Lancez : pip install pdfkit'}, status=500)
        from django.template.loader import get_template
        from core.pdf_utils import get_institution_context
        context.update(get_institution_context())

        html_string = get_template(template_name).render(context)
        config = pdfkit.configuration(wkhtmltopdf=r'C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe')
        options = {
            'footer-center':            'Page [page] / [toPage]',
            'margin-top':    '0.50in', 'margin-right':  '0.50in',
            'margin-bottom': '0.75in', 'margin-left':   '0.50in',
            'orientation':   'Landscape',
            'enable-local-file-access': '',
        }

        try:
            pdf_bytes = pdfkit.from_string(html_string, False, configuration=config, options=options)
        except Exception as exc:
            logger.error('pdfkit error: %s', exc)
            return Response({'error': f'Erreur generation PDF : {exc}'}, status=500)

        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # ── GET /api/v1/suivi/pointages/pdf-individuel/ ───────────────────────
    @action(detail=False, methods=['get'], url_path='pdf-individuel')
    def pdf_individuel(self, request):
        """PDF individuel par semestre.

        numero_semaine est interprete comme GLOBAL. Le frontend traduit
        local -> global avant l'appel. On expose dans le contexte template
        les deux numeros + l'info de decalage pour affichage clair.
        """
        annee    = request.query_params.get('annee_universitaire')
        semaine  = request.query_params.get('numero_semaine')
        ts       = request.query_params.get('type_semestre')
        sem_code = request.query_params.get('id_semestre', '')

        if not all([annee, semaine]):
            return Response({'error': 'annee_universitaire et numero_semaine requis.'}, status=400)

        fiches_semaine = self._build_fiches_par_jour(annee, semaine, ts, sem_code or None)
        if not fiches_semaine:
            return Response({'error': 'Aucune donnee pour cette selection.'}, status=404)

        # Calcul du numero pedagogique local (si sem_code present + decalage)
        from apps.departement.semaine_helpers import decalage_for_code_semestre
        decalage = decalage_for_code_semestre(sem_code, ts) if sem_code else 0
        semaine_globale     = int(semaine)
        semaine_pedagogique = semaine_globale - decalage if decalage else semaine_globale

        semestre_label = sem_code if sem_code else 'Tous semestres'
        context = {
            'fiches_semaine':       fiches_semaine,
            'semaine':              semaine_pedagogique,    # affichage principal
            'semaine_globale':      semaine_globale,         # info secondaire
            'semaine_a_decalage':   bool(decalage),
            'semestre':             semestre_label,
            'annee_universitaire':  annee,
        }
        filename = f"fiche_presence_{semestre_label}_semaine_{semaine_pedagogique}_{annee}.pdf"
        return self._generate_pdf('fiche_suivi_individuelle.html', context, filename)

    # ── GET /api/v1/suivi/pointages/pdf-collectif/ ────────────────────────
    @action(detail=False, methods=['get'], url_path='pdf-collectif')
    def pdf_collectif(self, request):
        """PDF collectif (multi-dept). numero_semaine reste GLOBAL.

        Le header met en avant les DATES (debut-fin) pour eviter toute ambiguite
        liee au decalage des differents departements affiches.
        """
        annee   = request.query_params.get('annee_universitaire')
        semaine = request.query_params.get('numero_semaine')
        ts      = request.query_params.get('type_semestre')

        if not all([annee, semaine]):
            return Response({'error': 'annee_universitaire et numero_semaine requis.'}, status=400)

        fiches_tous_jours = self._build_fiches_par_jour(annee, semaine, ts)
        if not fiches_tous_jours:
            return Response({'error': 'Aucune donnee pour cette semaine.'}, status=404)

        # Dates min/max de la semaine (pour header "du XX au YY")
        # On les derive du premier et dernier fiche_jour si disponibles.
        date_debut = date_fin = ''
        for fj in fiches_tous_jours:
            if fj.get('date_jour'):
                if not date_debut:
                    date_debut = fj['date_jour']
                date_fin = fj['date_jour']

        context = {
            'fiches_tous_jours':   fiches_tous_jours,
            'semaine':             semaine,          # numero global
            'date_debut':          date_debut,
            'date_fin':            date_fin,
            'annee_universitaire': annee,
        }
        filename = f"fiche_presence_collective_semaine_{semaine}_{annee}.pdf"
        return self._generate_pdf('fiche_suivi_collective.html', context, filename)

    # ── GET /api/v1/suivi/pointages/rattrapage/ ───────────────────────────
    @action(detail=False, methods=['get'], url_path='rattrapage')
    def rattrapage(self, request):
        annee = request.query_params.get('annee_universitaire')
        prof  = request.query_params.get('prof')
        mode  = request.query_params.get('mode', 'Non fait')
        ts    = request.query_params.get('type_semestre')

        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)

        qs = SuiviePointage.objects.filter(annee_universitaire=annee, commentaire=mode)
        if prof:
            qs = qs.filter(prof_id=prof)
        if ts:
            qs = qs.filter(type_semestre=ts)
        qs = (qs.select_related(*POINTAGE_SELECT_RELATED)
                .prefetch_related('departements')
                .order_by('date_suivie', 'numero_semaine'))

        seen = set()
        result = []
        for sp in qs:
            dept_noms = sorted(d.nom for d in sp.departements.all() if d.nom)
            key = (sp.jour_fk_id, sp.creneau_fk_id, sp.type_seance_fk_id,
                   tuple(dept_noms), sp.semestre_id, sp.numero_semaine, str(sp.date_suivie))
            if key in seen:
                continue
            seen.add(key)
            ts_label   = sp.type_seance_fk.type_seance if sp.type_seance_fk_id and sp.type_seance_fk else ''
            jour_label = sp.jour_fk.jour if sp.jour_fk_id and sp.jour_fk else ''
            cren_label = sp.creneau_fk.creneau if sp.creneau_fk_id and sp.creneau_fk else ''
            result.append({
                'id':             sp.pk,
                'jour':           jour_label,
                'date_suivie':    str(sp.date_suivie) if sp.date_suivie else None,
                'numero_semaine': sp.numero_semaine,
                'creneau':        cren_label,
                'prof_nom':       sp.prof.nom if sp.prof else '',
                'em_intitule':    sp.em.intitule if sp.em else '',
                'type_seance':    ts_label,
                'salle_nom':      sp.salle.nom if sp.salle else '',
                'commentaire':    sp.commentaire,
                'departement':    ' / '.join(dept_noms),
            })
        return Response(result)


class ChargeInstitutionViewSet(AuditMixin, viewsets.ModelViewSet):
    queryset = ChargeInstitution.objects.select_related('institution', 'prof').all()
    serializer_class   = ChargeInstitutionSerializer
    permission_classes = [RBACPermission]
    required_module    = 'suivi_charges'
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['annee_universitaire', 'prof', 'institution']
    pagination_class   = StandardPagination


# ── Permissions de rattrapage (admin only) ───────────────────────────────────
class SuiviGenerationAuthorizationViewSet(viewsets.GenericViewSet):
    """Gestion des autorisations de rattrapage suivi (admin only).

    Endpoints :
      GET    /api/v1/suivi/rattrapages/pending/?annee=...&type_semestre=...
        Renvoie pour chaque user non-admin sa liste de semaines cloturees
        (passees, non encore generees pour lui, sans autorisation active).
      POST   /api/v1/suivi/rattrapages/grant/
        Body : {user_id, annee_universitaire, type_semestre, numero_semaine, note}
        Cree (ou rafraichit) une autorisation.
      DELETE /api/v1/suivi/rattrapages/<id>/
        Revoque une autorisation (si pas encore utilisee).
      GET    /api/v1/suivi/rattrapages/history/?annee=...
        Historique : liste complete des autorisations (utilisees, pending, revoquees).
    """
    from core.permissions import IsAdmin as _IsAdmin
    permission_classes = [_IsAdmin]

    @action(detail=False, methods=['get'], url_path='pending')
    def pending(self, request):
        from apps.authentication.models import CustomUser
        from apps.parametres.models import Semaine as SemaineParam
        from django.db.models import Max as _Max
        import datetime as _dt

        annee = request.query_params.get('annee_universitaire')
        ts    = request.query_params.get('type_semestre')
        if not annee or not ts:
            return Response(
                {'error': 'annee_universitaire et type_semestre requis.'},
                status=400,
            )

        today = _dt.date.today()

        # Semaines cloturees = celles dont date_fin < today
        # (on ignore grace_days ici car admin voit tout pour decision RH)
        weeks_calendar = list(
            SemaineParam.objects
            .filter(annee_universitaire=annee, type_semestre=ts, type_semaine='cours', numero_semaine__isnull=False)
            .values('numero_semaine').annotate(date_fin=_Max('date'))
            .order_by('numero_semaine')
        )
        cloturees = {w['numero_semaine']: w['date_fin'] for w in weeks_calendar if w['date_fin'] < today}
        if not cloturees:
            return Response({'users': []})

        # Tous les users non-admin avec au moins 1 dept attribue
        users_qs = (
            CustomUser.objects.filter(is_active=True)
            .exclude(role='admin').exclude(is_superuser=True)
            .exclude(role__in=['enseignant', 'etudiant'])
            .prefetch_related('managed_departements')
        )

        # Autorisations existantes (non utilisees) pour ces users
        auth_qs = SuiviGenerationAuthorization.objects.filter(
            annee_universitaire=annee, type_semestre=ts,
            used_at__isnull=True,
        )
        auths_by_user = defaultdict(set)
        for a in auth_qs:
            auths_by_user[a.user_id].add(a.numero_semaine)

        # Semaines deja generees par user (donc deja a jour, pas de rattrapage)
        generees_qs = Suivie.objects.filter(
            annee_universitaire=annee, type_semestre=ts,
        ).values('departement_id', 'numero_semaine').distinct()
        # Map dept -> ensemble des sems generees
        from collections import defaultdict as _dd
        sems_by_dept: dict = _dd(set)
        for r in generees_qs:
            sems_by_dept[r['departement_id']].add(r['numero_semaine'])

        result = []
        for u in users_qs:
            managed_depts = list(u.managed_departements.values_list('id', flat=True))
            if not managed_depts:
                continue
            # Semaines generees pour ce user = union des sems par dept
            sems_done = set()
            for d in managed_depts:
                sems_done |= sems_by_dept.get(d, set())
            # Semaines cloturees PAS encore generees pour ce user
            sems_a_rattraper = sorted(
                n for n in cloturees if n not in sems_done
            )
            if not sems_a_rattraper:
                continue
            already_authorized = sorted(auths_by_user.get(u.id, set()))
            result.append({
                'user_id':            u.id,
                'username':           u.username,
                'name':               u.name or u.username,
                'role':               u.role,
                'sems_a_rattraper':   sems_a_rattraper,
                'already_authorized': already_authorized,
            })

        return Response({'users': result, 'cloturees_dates': {n: d.isoformat() for n, d in cloturees.items()}})

    @action(detail=False, methods=['post'], url_path='grant')
    def grant(self, request):
        from apps.authentication.models import CustomUser
        from core.audit_helpers import write_audit
        try:
            user_id  = int(request.data.get('user_id'))
            annee    = request.data.get('annee_universitaire')
            ts       = request.data.get('type_semestre')
            numero_s = int(request.data.get('numero_semaine'))
        except (TypeError, ValueError):
            return Response({'error': 'Champs requis : user_id, annee_universitaire, type_semestre, numero_semaine.'}, status=400)
        if not annee or not ts:
            return Response({'error': 'annee_universitaire et type_semestre requis.'}, status=400)
        note = request.data.get('note', '') or ''

        try:
            target_user = CustomUser.objects.get(pk=user_id, is_active=True)
        except CustomUser.DoesNotExist:
            return Response({'error': 'Utilisateur introuvable.'}, status=404)

        obj, created = SuiviGenerationAuthorization.objects.update_or_create(
            user=target_user, annee_universitaire=annee,
            type_semestre=ts, numero_semaine=numero_s,
            defaults={
                'granted_by': request.user,
                'note':       note[:200],
                # Si une autorisation precedente etait utilisee, on remet a zero pour permettre un nouveau rattrapage
                'used_at':    None,
            },
        )
        write_audit(
            action='CREATE' if created else 'UPDATE',
            model_name='SuiviGenerationAuthorization',
            object_id=str(obj.pk),
            changes={
                'user_id': user_id, 'annee': annee, 'type_semestre': ts,
                'numero_semaine': numero_s, 'note': note,
            },
            label=f'Grant rattrapage sem {numero_s} a user#{user_id}',
            keep_forever=True,
        )
        return Response({
            'id': obj.pk, 'created': created,
            'user_id': obj.user_id, 'numero_semaine': obj.numero_semaine,
        })

    @action(detail=False, methods=['post'], url_path='revoke')
    def revoke(self, request):
        from core.audit_helpers import write_audit
        try:
            auth_id = int(request.data.get('auth_id'))
        except (TypeError, ValueError):
            return Response({'error': 'auth_id requis.'}, status=400)
        try:
            obj = SuiviGenerationAuthorization.objects.get(pk=auth_id)
        except SuiviGenerationAuthorization.DoesNotExist:
            return Response({'error': 'Autorisation introuvable.'}, status=404)
        if obj.used_at is not None:
            return Response({'error': 'Autorisation deja utilisee, impossible de revoquer.'}, status=400)
        snapshot = {
            'user_id': obj.user_id, 'annee': obj.annee_universitaire,
            'type_semestre': obj.type_semestre, 'numero_semaine': obj.numero_semaine,
        }
        obj.delete()
        write_audit(
            action='DELETE',
            model_name='SuiviGenerationAuthorization',
            object_id=str(auth_id),
            changes=snapshot,
            label=f'Revoke rattrapage sem {snapshot["numero_semaine"]}',
            keep_forever=True,
        )
        return Response({'revoked': True})

    @action(detail=False, methods=['get'], url_path='history')
    def history(self, request):
        annee = request.query_params.get('annee_universitaire')
        ts    = request.query_params.get('type_semestre')
        qs = SuiviGenerationAuthorization.objects.select_related('user', 'granted_by').order_by('-granted_at')
        if annee:
            qs = qs.filter(annee_universitaire=annee)
        if ts:
            qs = qs.filter(type_semestre=ts)
        data = [{
            'id':               a.pk,
            'user_id':          a.user_id,
            'user_username':    a.user.username,
            'user_name':        a.user.name or a.user.username,
            'annee_universitaire': a.annee_universitaire,
            'type_semestre':    a.type_semestre,
            'numero_semaine':   a.numero_semaine,
            'granted_by_username': a.granted_by.username,
            'granted_at':       a.granted_at.isoformat(),
            'used_at':          a.used_at.isoformat() if a.used_at else None,
            'note':             a.note,
            'status':           'used' if a.used_at else 'pending',
        } for a in qs[:200]]
        return Response({'history': data})
