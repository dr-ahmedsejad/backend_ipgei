import logging
from collections import defaultdict
from django.db.models import Q
from django.http import HttpResponse
from django.template.loader import render_to_string
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import OrderingFilter
from core.permissions import RBACPermission, EDTDepartementPermission
from core.mixins import AuditMixin, InstitutionScopedMixin, DepartementScopedMixin
from core.pagination import StandardPagination
from .models import Emplois
from .serializers import EmploisSerializer, EmploisCreateSerializer, DisponibiliteCheckSerializer

logger = logging.getLogger('siga')


EMPLOIS_SELECT_RELATED = (
    'prof', 'em', 'departement', 'salle', 'semestre',
    'creneau_fk', 'type_seance_fk', 'jour_fk',
)


class EmploisViewSet(InstitutionScopedMixin, DepartementScopedMixin, AuditMixin, viewsets.ModelViewSet):
    queryset = Emplois.objects.select_related(*EMPLOIS_SELECT_RELATED).all()
    permission_classes = [RBACPermission, EDTDepartementPermission]
    required_module    = 'emplois'
    # DepartementScopedMixin : Emplois a FK simple `departement`
    departement_filter_field  = 'departement'
    departement_filter_lookup = 'in'
    filter_backends    = [DjangoFilterBackend, OrderingFilter]
    filterset_fields   = ['annee_universitaire', 'departement', 'prof', 'salle',
                          'semestre', 'type_semestre', 'jour_fk']
    ordering_fields    = ['jour_fk', 'creneau_fk']
    pagination_class   = StandardPagination

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return EmploisCreateSerializer
        return EmploisSerializer

    @action(detail=False, methods=['get'], url_path='grille')
    def grille(self, request):
        """Retourne la grille {jour: {creneau_id: [seances]}}."""
        qs = self.filter_queryset(self.get_queryset())
        all_data = EmploisSerializer(qs, many=True).data

        grille: dict = {}
        for data in all_data:
            data    = dict(data)
            jour    = data.get('jour_label') or ''
            cle     = str(data.get('creneau_fk') or '')
            slot    = grille.setdefault(jour, {}).setdefault(cle, [])

            # Fusion : meme prof + EM + type + salle, depts differents -> all_depts[]
            dup = next((
                s for s in slot
                if s.get('prof')           == data.get('prof')
                and s.get('em')            == data.get('em')
                and s.get('type_seance_fk') == data.get('type_seance_fk')
                and s.get('salle')         == data.get('salle')
            ), None)

            if dup:
                dup['all_depts'].append({'id': data.get('departement'), 'nom': data.get('dept_nom')})
            else:
                data['all_depts'] = [{'id': data.get('departement'), 'nom': data.get('dept_nom')}]
                slot.append(data)

        return Response(grille)

    @action(detail=False, methods=['post'], url_path='check-dispo')
    def check_dispo(self, request):
        """Verifie la disponibilite d'un creneau/jour pour un dept, prof, salle."""
        s = DisponibiliteCheckSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d          = s.validated_data
        exclude_id = d.get('exclude_id') or 0
        prof_id    = d.get('prof_id')
        salle_id   = d.get('salle_id')

        # Resoudre `jour` (label OU ID numerique) en jour_fk_id
        jour_raw = d['jour']
        jour_fk_id = self._resolve_jour_fk_id(jour_raw)
        if jour_fk_id is None:
            return Response({'error': f"Jour inconnu : '{jour_raw}'"}, status=400)

        # Filtre sur les axes demandes
        q_filter = Q(departement_id=d['departement_id'])
        if prof_id:
            q_filter |= Q(prof_id=prof_id)
        if salle_id:
            q_filter |= Q(salle_id=salle_id)

        rows = list(
            Emplois.objects
            .filter(
                jour_fk_id=jour_fk_id,
                creneau_fk_id=d['creneau_id'],
                annee_universitaire=d['annee_universitaire'],
            )
            .exclude(pk=exclude_id)
            .filter(q_filter)
            .values('departement_id', 'prof_id', 'salle_id')
        )

        conflicts = {}
        for row in rows:
            if row['departement_id'] == d['departement_id']:
                conflicts['departement'] = 'Ce creneau est deja occupe pour ce departement.'
            if prof_id and row['prof_id'] == prof_id:
                conflicts['prof'] = 'Ce professeur est deja affecte a ce creneau.'
            if salle_id and row['salle_id'] == salle_id:
                conflicts['salle'] = 'Cette salle est deja occupee a ce creneau.'

        return Response({'disponible': not conflicts, 'conflicts': conflicts})

    def _resolve_jour_fk_id(self, raw):
        """Accepte un libelle ('Lundi') ou un ID numerique. Retourne le pk Jour ou None."""
        from apps.parametres.models import Jour
        if raw is None:
            return None
        s = str(raw).strip()
        if not s:
            return None
        if s.isdigit():
            try:
                return Jour.objects.values_list('pk', flat=True).get(pk=int(s))
            except Jour.DoesNotExist:
                return None
        try:
            return Jour.objects.values_list('pk', flat=True).get(jour=s)
        except Jour.DoesNotExist:
            return None

    @action(detail=False, methods=['get'], url_path='grille-all')
    def grille_all(self, request):
        """Grilles de TOUS les departements en une seule requete.

        Scope EDT : pour un user non-superuser, ne renvoie que les grilles
        des departements presents dans ses managed_departements.
        """
        annee         = request.query_params.get('annee_universitaire', '')
        type_semestre = request.query_params.get('type_semestre', '')

        if not annee:
            return Response({'error': 'annee_universitaire est requis.'}, status=400)

        qs = (
            Emplois.objects
            .filter(annee_universitaire=annee)
            .select_related(*EMPLOIS_SELECT_RELATED)
        )
        if type_semestre:
            qs = qs.filter(type_semestre=type_semestre)
        # Scope user : non-superuser -> filtre sur ses depts
        user_dept_ids = self.user_dept_ids()
        if user_dept_ids is not None:
            qs = qs.filter(departement_id__in=user_dept_ids)

        all_data = EmploisSerializer(qs, many=True).data

        result: dict = {}
        for data in all_data:
            data     = dict(data)
            dept_id  = str(data.get('departement') or '')
            if not dept_id:
                continue
            jour    = data.get('jour_label') or ''
            cle     = str(data.get('creneau_fk') or '')
            dept_grille = result.setdefault(dept_id, {})
            slot        = dept_grille.setdefault(jour, {}).setdefault(cle, [])

            dup = next((
                s for s in slot
                if s.get('prof')           == data.get('prof')
                and s.get('em')            == data.get('em')
                and s.get('type_seance_fk') == data.get('type_seance_fk')
                and s.get('salle')         == data.get('salle')
            ), None)

            if dup:
                dup['all_depts'].append({'id': data.get('departement'), 'nom': data.get('dept_nom')})
            else:
                data['all_depts'] = [{'id': data.get('departement'), 'nom': data.get('dept_nom')}]
                slot.append(data)

        return Response(result)

    @action(detail=False, methods=['post'], url_path='bulk')
    def bulk_create(self, request):
        """Creation en masse d'un emploi du temps.

        Scope EDT : chaque item doit cibler un `departement` present dans
        `request.user.managed_departements`. Sinon l'item est rejete en bloc
        (la totalite du bulk doit etre dans le perimetre, pour eviter les
        rejets partiels silencieux). Superuser bypass conserve.
        """
        items = request.data if isinstance(request.data, list) else request.data.get('items', [])

        # Pre-validation du scope EDT : tous les depts cibles doivent etre autorises.
        user_dept_ids = self.user_dept_ids()
        if user_dept_ids is not None:   # non-superuser
            if not user_dept_ids:
                return Response(
                    {'error': "Aucun groupe ne vous est attribue. Contactez un administrateur."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            allowed = set(user_dept_ids)
            out_of_scope = []
            for idx, item in enumerate(items):
                dept = item.get('departement') or item.get('departement_id')
                try:
                    dept_int = int(dept) if dept is not None else None
                except (TypeError, ValueError):
                    dept_int = None
                if dept_int is None or dept_int not in allowed:
                    out_of_scope.append({'index': idx, 'departement': dept})
            if out_of_scope:
                return Response(
                    {
                        'error': "Certains items ciblent un departement hors de votre perimetre.",
                        'out_of_scope': out_of_scope,
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )

        created = []
        errors  = []
        for item in items:
            s = EmploisCreateSerializer(data=item)
            if s.is_valid():
                created.append(s.save())
            else:
                errors.append({'data': item, 'errors': s.errors})
        return Response({'created': len(created), 'errors': errors}, status=status.HTTP_207_MULTI_STATUS)

    # ─────────────────────────────────────────────────────────────────────────
    # Import EDT depuis une semaine de suivi (auto-remplissage)
    # ─────────────────────────────────────────────────────────────────────────

    @action(detail=False, methods=['get'], url_path='count-scoped')
    def count_scoped(self, request):
        """Compte des Emplois pour (annee, type_semestre) optionnellement
        scope a une liste de departements. Permet a l'UI d'import de savoir
        si l'EDT est vide pour les groupes selectionnes.

        Scope additionnel : pour un user non-admin, n'inclut que ses
        managed_departements (intersection si dept_ids fourni).
        """
        annee = request.query_params.get('annee_universitaire')
        ts    = request.query_params.get('type_semestre')
        if not annee or not ts:
            return Response(
                {'error': 'annee_universitaire et type_semestre requis.'},
                status=400
            )
        depts_raw = request.query_params.get('departements', '').strip()
        try:
            dept_ids = [int(d) for d in depts_raw.split(',') if d] if depts_raw else []
        except ValueError:
            return Response({'error': 'departements doit etre une liste d ids.'}, status=400)

        # Scoping user
        user_ids = self.user_dept_ids()
        if user_ids is not None:   # non-admin
            if not user_ids:
                return Response({'count': 0})
            dept_ids = list(set(dept_ids) & set(user_ids)) if dept_ids else user_ids

        qs = Emplois.objects.filter(annee_universitaire=annee, type_semestre=ts)
        if dept_ids:
            qs = qs.filter(departement_id__in=dept_ids)
        return Response({'count': qs.count()})

    @action(detail=False, methods=['get'], url_path='template-weeks')
    def template_weeks(self, request):
        """Liste les semaines de suivi disponibles comme template d'import.

        Retourne pour chaque semaine : numero, dates, nb de cours reguliers
        (filtre des evenements ponctuels), nb de profs/em distincts.
        Permet a l'UI d'aider l'utilisateur a choisir la meilleure source.
        """
        from apps.suivi.models import Suivie
        from apps.parametres.models import Semaine

        annee = request.query_params.get('annee_universitaire')
        ts    = request.query_params.get('type_semestre')
        if not annee or not ts:
            return Response(
                {'error': 'annee_universitaire et type_semestre requis.'},
                status=400
            )
        # Filtre optionnel par departement (multi via ?departements=1,2,3)
        depts_raw = request.query_params.get('departements', '').strip()
        try:
            dept_ids = [int(d) for d in depts_raw.split(',') if d] if depts_raw else []
        except ValueError:
            return Response({'error': 'departements doit etre une liste d ids.'}, status=400)

        # Scoping user : intersection avec managed_departements (admin bypass)
        user_ids = self.user_dept_ids()
        if user_ids is not None:
            if not user_ids:
                return Response([])   # aucune delegation -> aucune semaine
            dept_ids = list(set(dept_ids) & set(user_ids)) if dept_ids else user_ids

        # Filtre comme l'import : exclure uniquement les evenements vraiment
        # ponctuels (Surveillance d'examen, Mission). DS/EF/ER/Sport/
        # Instruction militaire restent importables car ils figurent dans
        # l'EDT hebdomadaire.
        excluded_types = ['Surveillance', 'Mission']

        from django.db.models import Count, Q

        # Etape 1 : Recuperer TOUTES les semaines distinctes ayant au moins
        # une trace dans suivi_suivie (meme si toutes les lignes sont vides),
        # pour que l'UI affiche le statut de chacune.
        all_weeks_qs = (
            Suivie.objects
            .filter(annee_universitaire=annee, type_semestre=ts)
            .values('numero_semaine')
            .distinct()
            .order_by('-numero_semaine')
        )
        all_weeks = [r['numero_semaine'] for r in all_weeks_qs]

        # Etape 2 : Compter les lignes IMPORTABLES par semaine (cours reguliers
        # avec prof/em/type, hors evenements ponctuels).
        importables_qs = (
            Suivie.objects
            .filter(
                annee_universitaire=annee,
                type_semestre=ts,
                prof_id__isnull=False,
                em_id__isnull=False,
                type_seance_fk__isnull=False,
            )
            .exclude(type_seance_fk__type_seance__in=excluded_types)
        )
        if dept_ids:
            importables_qs = importables_qs.filter(departement_id__in=dept_ids)
        importables = (
            importables_qs
            .values('numero_semaine')
            .annotate(
                nb_cours=Count('id', distinct=True),
                nb_profs=Count('prof', distinct=True),
                nb_em=Count('em', distinct=True),
            )
        )
        importables_map = {r['numero_semaine']: r for r in importables}

        # Etape 3 : Dates min/max de chaque semaine
        semaine_dates = {}
        for s in Semaine.objects.filter(
            annee_universitaire=annee, type_semestre=ts
        ).values('numero_semaine', 'date'):
            n = s['numero_semaine']
            d = s['date']
            if n not in semaine_dates:
                semaine_dates[n] = {'date_debut': d, 'date_fin': d}
            else:
                if d < semaine_dates[n]['date_debut']:
                    semaine_dates[n]['date_debut'] = d
                if d > semaine_dates[n]['date_fin']:
                    semaine_dates[n]['date_fin'] = d

        # Etape 4 : Construire la reponse pour TOUTES les semaines, en marquant
        # 'importable' selon qu'elle a des cours valides ou pas.
        return Response([
            {
                'numero_semaine': n,
                'nb_cours':       importables_map.get(n, {}).get('nb_cours', 0),
                'nb_profs':       importables_map.get(n, {}).get('nb_profs', 0),
                'nb_em':          importables_map.get(n, {}).get('nb_em', 0),
                'date_debut':     str(semaine_dates.get(n, {}).get('date_debut', '')),
                'date_fin':       str(semaine_dates.get(n, {}).get('date_fin', '')),
                'importable':     n in importables_map,
            }
            for n in all_weeks
        ])

    @action(detail=False, methods=['post'], url_path='import-from-suivi')
    def import_from_suivi(self, request):
        """Cree Emplois depuis une semaine de suivi (auto-remplissage).

        Body :
          - annee_universitaire, type_semestre, numero_semaine (requis)
          - departements: [ids] (optionnel) — n'importe que ces departements
            et n'exige que ces departements soient vides cote Emplois.

        Pre-condition : Emplois doit etre vide pour le scope (annee, ts,
        departements optionnels). Permet un import par groupe sans ecraser
        les autres departements deja remplis.

        Filtres a la copie :
          - prof_id, em_id, type_seance_fk NOT NULL (skip lignes vides)
          - type_seance NOT IN (Surveillance/Mission) — seuls les evenements
            vraiment ponctuels sont exclus
          - Skip lignes avec FK orphelines (prof/em/salle supprimes)

        Audit : un audit CREATE par ligne Emplois cree.
        Rollback : possible via POST /emplois/vider/ (memes parametres).
        """
        from apps.suivi.models import Suivie
        from apps.prof.models import Prof
        from apps.em.models import EM
        from apps.salle.models import Salle
        from core.audit_helpers import write_audit_bulk
        from django.db import transaction as _tx

        annee  = request.data.get('annee_universitaire')
        ts     = request.data.get('type_semestre')
        num_s  = request.data.get('numero_semaine')
        depts  = request.data.get('departements') or []
        if not all([annee, ts, num_s]):
            return Response(
                {'error': 'annee_universitaire, type_semestre et numero_semaine requis.'},
                status=400
            )
        try:
            num_s_i = int(num_s)
        except (TypeError, ValueError):
            return Response({'error': 'numero_semaine invalide.'}, status=400)
        try:
            dept_ids = [int(d) for d in depts] if depts else []
        except (TypeError, ValueError):
            return Response({'error': 'departements doit etre une liste d ids.'}, status=400)

        # Scoping user : intersection avec managed_departements (admin bypass).
        # Si user n'a aucun dept attribue -> 403.
        # Si user a passe des dept_ids hors perimetre -> filtrage silencieux
        # a l'intersection (on n'importe QUE ce qu'il a le droit d'importer).
        user_ids = self.user_dept_ids()
        if user_ids is not None:
            if not user_ids:
                return Response(
                    {'error': "Aucun groupe ne vous est attribue. Contactez un administrateur."},
                    status=403,
                )
            if dept_ids:
                dept_ids = list(set(dept_ids) & set(user_ids))
                if not dept_ids:
                    return Response(
                        {'error': "Aucun des groupes selectionnes n'est dans votre perimetre."},
                        status=403,
                    )
            else:
                # User n'a pas precise de dept -> on force a son perimetre
                dept_ids = list(user_ids)

        # Pre-check : Emplois doit etre vide (scope dept si fourni)
        existing_qs = Emplois.objects.filter(
            annee_universitaire=annee, type_semestre=ts,
        )
        if dept_ids:
            existing_qs = existing_qs.filter(departement_id__in=dept_ids)
        existing = existing_qs.count()
        if existing > 0:
            scope_msg = f' pour les departements selectionnes' if dept_ids else ''
            return Response(
                {'error': f"EDT non vide ({existing} cours présents{scope_msg}). Videz d'abord via /emplois/vider/."},
                status=400,
            )

        # Recuperer les lignes valides depuis suivi_suivie
        # On exclut uniquement les evenements vraiment ponctuels
        # (DS/EF/ER/Sport/Instruction militaire restent importables)
        excluded_types = ['Surveillance', 'Mission']
        source_qs = (
            Suivie.objects
            .filter(
                annee_universitaire=annee,
                type_semestre=ts,
                numero_semaine=num_s_i,
                prof_id__isnull=False,
                em_id__isnull=False,
                type_seance_fk__isnull=False,
            )
            .exclude(type_seance_fk__type_seance__in=excluded_types)
        )
        if dept_ids:
            source_qs = source_qs.filter(departement_id__in=dept_ids)
        source_rows = list(
            source_qs.values(
                'prof_id', 'em_id', 'salle_id', 'semestre_id', 'departement_id',
                'creneau_fk_id', 'type_seance_fk_id', 'jour_fk_id',
                'institution_id', 'taux_paiement',
            ).distinct()
        )

        if not source_rows:
            return Response(
                {'error': f"Aucun cours regulier trouve pour la semaine {num_s_i}."},
                status=404,
            )

        # Validation FK : skip les lignes avec FK orphelines
        profs_actifs  = set(Prof.objects.values_list('id', flat=True))
        ems_actifs    = set(EM.objects.values_list('id', flat=True))
        salles_actives = set(Salle.objects.values_list('id', flat=True))

        valid_rows = []
        warnings   = []
        for row in source_rows:
            if row['prof_id'] and row['prof_id'] not in profs_actifs:
                warnings.append(f"Skip: prof_id={row['prof_id']} introuvable")
                continue
            if row['em_id'] and row['em_id'] not in ems_actifs:
                warnings.append(f"Skip: em_id={row['em_id']} introuvable")
                continue
            if row['salle_id'] and row['salle_id'] not in salles_actives:
                warnings.append(f"Skip: salle_id={row['salle_id']} introuvable")
                continue
            valid_rows.append(row)

        if not valid_rows:
            return Response(
                {'error': "Aucune ligne valide apres validation des FK.", 'warnings': warnings},
                status=400,
            )

        # Creer les Emplois en bulk dans une transaction
        new_emplois = [
            Emplois(
                annee_universitaire=annee,
                type_semestre=ts,
                **row,
            )
            for row in valid_rows
        ]

        with _tx.atomic():
            Emplois.objects.bulk_create(new_emplois)
            # Re-fetch pour avoir les IDs (MySQL ne les remplit pas via bulk_create)
            created = list(
                Emplois.objects
                .filter(annee_universitaire=annee, type_semestre=ts)
                .order_by('-id')[:len(new_emplois)]
            )

        # Audit individuel par ligne creee
        audit_items = [
            {
                'object_id': e.pk,
                'institution_id': e.institution_id,
                'changes': {
                    'snapshot': {
                        'annee_universitaire': e.annee_universitaire,
                        'type_semestre':       e.type_semestre,
                        'prof_id':             e.prof_id,
                        'em_id':               e.em_id,
                        'salle_id':            e.salle_id,
                        'semestre_id':         e.semestre_id,
                        'departement_id':      e.departement_id,
                        'creneau_fk_id':       e.creneau_fk_id,
                        'type_seance_fk_id':   e.type_seance_fk_id,
                        'jour_fk_id':          e.jour_fk_id,
                        'taux_paiement':       e.taux_paiement,
                    },
                    'imported_from': {
                        'numero_semaine_source': num_s_i,
                        'annee_universitaire':   annee,
                        'type_semestre':         ts,
                    },
                },
            }
            for e in created
        ]
        write_audit_bulk(
            'Emplois', 'CREATE', audit_items,
            label=f'Import EDT depuis suivi sem {num_s_i}',
            request=request,
        )

        return Response({
            'created':       len(created),
            'warnings':      warnings,
            'source_week':   num_s_i,
            'rollback_url':  '/api/v1/emplois/vider/',
            'rollback_body': {'annee_universitaire': annee, 'type_semestre': ts},
        })

    @action(detail=False, methods=['post'], url_path='vider')
    def vider(self, request):
        """Vide l'EDT pour (annee, type_semestre) — optionnellement scope a
        une liste de departements. Sert de rollback pour annuler un
        import-from-suivi recent. Suppression auditee par les signals
        globaux (post_delete) sur Emplois (deja dans TRACKED_MODELS).

        Body :
          - annee_universitaire, type_semestre (requis)
          - departements: [ids] (optionnel) — ne vide que ces departements
        """
        from django.db import transaction as _tx

        annee = request.data.get('annee_universitaire')
        ts    = request.data.get('type_semestre')
        depts = request.data.get('departements') or []
        if not all([annee, ts]):
            return Response(
                {'error': 'annee_universitaire et type_semestre requis.'},
                status=400,
            )
        try:
            dept_ids = [int(d) for d in depts] if depts else []
        except (TypeError, ValueError):
            return Response({'error': 'departements doit etre une liste d ids.'}, status=400)

        # Scoping user : intersection avec managed_departements (admin bypass)
        user_ids = self.user_dept_ids()
        if user_ids is not None:
            if not user_ids:
                return Response(
                    {'error': "Aucun groupe ne vous est attribue."},
                    status=403,
                )
            if dept_ids:
                dept_ids = list(set(dept_ids) & set(user_ids))
                if not dept_ids:
                    return Response(
                        {'error': "Aucun des groupes selectionnes n'est dans votre perimetre."},
                        status=403,
                    )
            else:
                dept_ids = list(user_ids)

        qs = Emplois.objects.filter(annee_universitaire=annee, type_semestre=ts)
        if dept_ids:
            qs = qs.filter(departement_id__in=dept_ids)

        with _tx.atomic():
            count = qs.count()
            if count == 0:
                return Response({'deleted': 0, 'message': 'EDT deja vide.'})
            # delete() declenche post_delete -> audit par ligne via TRACKED_MODELS
            qs.delete()

        logger.info('Vider EDT : %d lignes supprimees pour %s/%s depts=%s',
                    count, annee, ts, dept_ids or 'tous')
        return Response({'deleted': count})

    @action(detail=False, methods=['get'], url_path='pdf', permission_classes=[RBACPermission])
    def pdf(self, request):
        """Genere le PDF de l'emploi du temps d'une filiere (wkhtmltopdf)."""
        import pdfkit
        from apps.departement.models import Departement
        from apps.parametres.models import Semestre, Creneau, Jour

        annee       = request.query_params.get('annee_universitaire', '')
        dept_id     = request.query_params.get('departement', '')
        semestre_id = request.query_params.get('semestre', '')

        if not all([annee, dept_id, semestre_id]):
            return Response(
                {'error': 'annee_universitaire, departement et semestre sont requis.'},
                status=400,
            )

        try:
            dept     = Departement.objects.get(pk=dept_id)
            semestre = Semestre.objects.get(pk=semestre_id)
        except (Departement.DoesNotExist, Semestre.DoesNotExist):
            return Response({'error': 'Departement ou semestre introuvable.'}, status=404)

        jours    = list(Jour.objects.all().order_by('id'))
        creneaux = list(Creneau.objects.filter(is_actif=True).order_by('ordre', 'creneau'))

        emplois_qs = Emplois.objects.filter(
            annee_universitaire=annee,
            departement_id=dept_id,
            semestre_id=semestre_id,
        ).select_related(*EMPLOIS_SELECT_RELATED)

        # Grille {jour_label: {creneau_id: [emplois]}}
        grille = defaultdict(lambda: defaultdict(list))
        for e in emplois_qs:
            jour_label = e.jour_fk.jour if e.jour_fk_id and e.jour_fk else ''
            cr_id = e.creneau_fk_id or 0
            grille[jour_label][cr_id].append(e)

        rows = [
            {'jour': j.jour, 'cells': [grille[j.jour][cr.id] for cr in creneaux]}
            for j in jours
        ]

        # Label dept enrichi : "Filiere - Niveau - Nom" + indication decalage
        # informative si Impair et dept avec formation militaire.
        dept_label_parts = []
        if dept.filiere_id and dept.filiere:
            dept_label_parts.append(dept.filiere.intitule_fr or dept.filiere.code or '')
        if dept.niveau_id and dept.niveau:
            dept_label_parts.append(dept.niveau.niveau or '')
        if dept.nom:
            dept_label_parts.append(dept.nom)
        dept_label = ' '.join(p for p in dept_label_parts if p) or dept.nom

        # Note decalage : utile pour rappeler le decalage du dept (visible dans
        # le titre du PDF recurrent). On lit le champ correspondant au semestre.
        dec_actif = (dept.decalage_impair if semestre.type_semestre == 'I'
                     else dept.decalage_pair)
        if dec_actif:
            dept_label += f' (décalage : {dec_actif} semaine{"s" if dec_actif > 1 else ""})'

        context = {
            'annee_universitaire': annee,
            'departement':         dept_label,
            'semestre_nom':        semestre.semestre,
            'creneaux':            creneaux,
            'rows':                rows,
        }

        html_string = render_to_string('emploi_filiere_pdf.html', context)

        config = pdfkit.configuration(wkhtmltopdf=r'C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe')
        options = {
            'margin-top':    '0.50in', 'margin-right':  '0.50in',
            'margin-bottom': '0.50in', 'margin-left':   '0.50in',
            'orientation':   'Landscape',
            'enable-local-file-access': '',
        }

        try:
            pdf_bytes = pdfkit.from_string(html_string, False, configuration=config, options=options)
        except Exception as exc:
            logger.error('PDF generation failed: %s', exc)
            return Response({'error': 'Erreur lors de la generation du PDF.'}, status=500)

        filename = f"emploi_{dept.nom}_{semestre.semestre}.pdf".replace(' ', '_')
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
