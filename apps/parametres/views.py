import logging
from datetime import timedelta
from django.db import transaction
from django.db.models import F, Max, Min
from rest_framework import mixins, viewsets, generics, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from core.permissions import IsAdmin
from core.mixins import AuditMixin, SelectAllMixin
from core.pagination import StandardPagination, NoPagination
from .models import Year, Niveau, Semestre, Seance, Creneau, Jour, Semaine, Paiement, Ramadan, Institution
from .serializers import (
    YearSerializer, NiveauSerializer, SemestreSerializer, SeanceSerializer,
    CreneauSerializer, JourSerializer, SemaineSerializer, PaiementSerializer,
    RamadanSerializer, InstitutionSerializer, GenerateSemainesSerializer,
    AddBatchSemainesSerializer,
)

logger = logging.getLogger('siga')


class YearViewSet(AuditMixin, SelectAllMixin, viewsets.ModelViewSet):
    queryset           = Year.objects.all()
    serializer_class   = YearSerializer
    permission_classes = [IsAdmin]
    filter_backends    = [SearchFilter]
    search_fields      = ['annee']
    pagination_class   = StandardPagination

    def get_permissions(self):
        # Lecture (list/retrieve) ouverte aux authentifies — donnee de reference
        # consommee partout dans l'app. Ecriture reservee admin.
        # `all` a deja son propre override (AllowAny pour la page login).
        if self.action in ('list', 'retrieve'):
            return [IsAuthenticated()]
        return super().get_permissions()

    @action(detail=False, methods=['get'], url_path='all', pagination_class=None,
            permission_classes=[AllowAny])
    def all(self, request):
        """Liste complète des années – accessible sans authentification (page login).
        Mise en cache 5 minutes pour éviter une requête DB à chaque affichage du formulaire."""
        from django.core.cache import cache
        CACHE_KEY = 'parametres:years:all'
        data = cache.get(CACHE_KEY)
        if data is None:
            qs = Year.objects.all().order_by('-annee')
            data = self.get_serializer(qs, many=True).data
            cache.set(CACHE_KEY, data, 300)  # 5 minutes
        return Response(data)

    def _invalidate_years_cache(self):
        from django.core.cache import cache
        cache.delete('parametres:years:all')

    @action(detail=True, methods=['post'], url_path='activer')
    def activer(self, request, pk=None):
        """Rend cette annee active. Contrainte : une seule annee active a la fois
        -> desactive toutes les autres. Refuse une annee cloturee."""
        year = self.get_object()
        if year.est_cloturee:
            return Response(
                {'error': "Impossible d'activer une annee cloturee. Rouvrez-la d'abord."},
                status=400,
            )
        Year.objects.exclude(pk=year.pk).filter(est_active=True).update(est_active=False)
        if not year.est_active:
            year.est_active = True
            year.save(update_fields=['est_active'])
        self._invalidate_years_cache()
        return Response(self.get_serializer(year).data)

    @action(detail=True, methods=['post'], url_path='cloturer')
    def cloturer(self, request, pk=None):
        """Cloture cette annee. Une annee cloturee ne peut pas rester active."""
        year = self.get_object()
        changed = []
        if not year.est_cloturee:
            year.est_cloturee = True
            changed.append('est_cloturee')
        if year.est_active:
            year.est_active = False
            changed.append('est_active')
        if changed:
            year.save(update_fields=changed)
            self._invalidate_years_cache()
        return Response(self.get_serializer(year).data)

    @action(detail=True, methods=['post'], url_path='rouvrir')
    def rouvrir(self, request, pk=None):
        """Reouvre une annee cloturee (sans la rendre active)."""
        year = self.get_object()
        if year.est_cloturee:
            year.est_cloturee = False
            year.save(update_fields=['est_cloturee'])
            self._invalidate_years_cache()
        return Response(self.get_serializer(year).data)


class NiveauViewSet(AuditMixin, SelectAllMixin, viewsets.ModelViewSet):
    queryset           = Niveau.objects.all()
    serializer_class   = NiveauSerializer
    permission_classes = [IsAdmin]
    filter_backends    = [SearchFilter]
    search_fields      = ['niveau']

    def get_permissions(self):
        # Idem Year/Semestre : lecture libre auth, ecriture admin.
        if self.action in ('list', 'retrieve', 'all'):
            return [IsAuthenticated()]
        return super().get_permissions()
    pagination_class   = StandardPagination


class SemestreViewSet(AuditMixin, SelectAllMixin, viewsets.ModelViewSet):
    queryset           = Semestre.objects.select_related('niveau_semestre').all()
    serializer_class   = SemestreSerializer
    filter_backends    = [DjangoFilterBackend, SearchFilter]
    filterset_fields   = ['niveau_semestre', 'type_semestre']
    search_fields      = ['semestre', 'code_semestre']
    pagination_class   = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        # Accord niveau filière ↔ semestres : ?filiere=<id> restreint aux semestres
        # dont le niveau est dans [niveau_debut, niveau_fin] de la filière. Le modèle
        # Semestre n'a pas de FK filière → on mappe via le NUMÉRO du niveau
        # (Niveau.niveau = 'L1'/'L2'…/'E5' → 1/2/…/5 ; 'Transversal' sans chiffre exclu).
        filiere_id = self.request.query_params.get('filiere')
        if filiere_id:
            import re
            from apps.scolarite.models import Filiere
            fil = Filiere.objects.filter(pk=filiere_id).first()
            if fil:
                niveau_ids = [
                    n.id for n in Niveau.objects.all()
                    if (m := re.search(r'(\d+)', n.niveau or ''))
                    and fil.niveau_debut <= int(m.group(1)) <= fil.niveau_fin
                ]
                qs = qs.filter(niveau_semestre_id__in=niveau_ids)
        return qs

    def get_permissions(self):
        # Lecture libre pour tous les utilisateurs authentifiés (données de référence) ;
        # `all` est l'action custom ajoutée par SelectAllMixin — sans elle dans le
        # tuple, le fallback IsAdmin bloquait DE/scolarite/etc. (cf. parametres/jours
        # qui souffrait du même bug).
        if self.action in ('list', 'retrieve', 'all'):
            return [IsAuthenticated()]
        return [IsAdmin()]


class SeanceViewSet(AuditMixin, SelectAllMixin, viewsets.ModelViewSet):
    queryset           = Seance.objects.all()
    serializer_class   = SeanceSerializer
    permission_classes = [IsAdmin]
    filter_backends    = [SearchFilter]
    search_fields      = ['type_seance']
    pagination_class   = StandardPagination

    def get_permissions(self):
        # Lecture (list/retrieve/all) ouverte aux utilisateurs authentifies :
        # le tableau des seances est une donnee de reference utilisee par
        # toutes les pages emplois/suivi/notes — sans cet override, les
        # comptes non-admin (DE, scolarite, etc.) recoivent 403.
        if self.action in ('list', 'retrieve', 'all'):
            return [IsAuthenticated()]
        return super().get_permissions()


class CreneauViewSet(AuditMixin, SelectAllMixin, mixins.UpdateModelMixin,
                     viewsets.ReadOnlyModelViewSet):
    """
    Creneaux horaires — LECTURE SEULE sur IPGEI.

    La LISTE des creneaux est celle de SIGA : IPGEI en tient un miroir alimente
    par `manage.py sync_creneaux_siga` (lecture seule sur `siga_pg`). On ne peut
    donc ni en creer, ni en supprimer ici — sans quoi les deux produits
    divergeraient sans que personne ne s'en apercoive.

    Seule exception : la **duree**, saisie creneau par creneau cote IPGEI. Le
    PATCH est donc ouvert, mais le serializer verrouille tous les autres champs.
    """
    queryset           = Creneau.objects.all()
    serializer_class   = CreneauSerializer
    permission_classes = [IsAuthenticated]
    filter_backends    = [DjangoFilterBackend, SearchFilter]
    filterset_fields   = ['is_actif', 'type_creneau']
    search_fields      = ['creneau']
    pagination_class   = StandardPagination

    def get_permissions(self):
        # Lecture pour tout utilisateur connecte ; saisie de la duree reservee
        # a l'admin, la valeur alimentant les calculs de charge.
        if self.action in ('update', 'partial_update'):
            return [IsAdmin()]
        return super().get_permissions()


class JourViewSet(AuditMixin, SelectAllMixin, viewsets.ModelViewSet):
    queryset           = Jour.objects.all()
    serializer_class   = JourSerializer
    permission_classes = [IsAdmin]
    filter_backends    = [SearchFilter]
    search_fields      = ['jour']
    pagination_class   = StandardPagination

    def get_permissions(self):
        # L'action `all` est utilisée par les emplois du temps (portail enseignant inclus)
        if self.action == 'all':
            return [IsAuthenticated()]
        return super().get_permissions()


class SemaineViewSet(AuditMixin, viewsets.ModelViewSet):
    queryset           = Semaine.objects.select_related('jour_fk').all()
    serializer_class   = SemaineSerializer
    permission_classes = [IsAdmin]
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields   = ['annee_universitaire', 'type_semestre', 'numero_semaine', 'type_semaine']
    search_fields      = ['annee_universitaire', 'jour_fk__jour']
    ordering_fields    = ['numero_semaine', 'date']
    pagination_class   = StandardPagination

    def get_permissions(self):
        # Lecture libre auth — utilise par avancement, suivi, statistiques.
        # 'actif' = endpoint public (appele par la page de login avant auth).
        # Ecriture (creer/modifier/supprimer/generer) reservee admin.
        if self.action == 'actif':
            from rest_framework.permissions import AllowAny
            return [AllowAny()]
        if self.action in ('list', 'retrieve'):
            return [IsAuthenticated()]
        return super().get_permissions()

    @action(detail=False, methods=['get'], url_path='actif')
    def actif(self, request):
        """Retourne le type_semestre actif aujourd'hui d'apres la table Semaine.
        Si aucune semaine ne match la date du jour, fallback sur la derniere
        semaine passee. Utilise par la page de login pour pre-cocher le bon
        semestre dans le selecteur."""
        from datetime import date as _date
        today = _date.today()

        # 1. Match exact sur la date du jour
        match = Semaine.objects.filter(date=today).order_by('-date').first()
        # 2. Fallback : derniere semaine <= today (inter-semestres)
        if not match:
            match = Semaine.objects.filter(date__lte=today).order_by('-date').first()
        # 3. Ultime fallback : la 1ere semaine future (debut d'annee)
        if not match:
            match = Semaine.objects.filter(date__gt=today).order_by('date').first()

        if not match:
            return Response({'type_semestre': 'I', 'annee_universitaire': '', 'source': 'default'})
        return Response({
            'type_semestre':       match.type_semestre,
            'annee_universitaire': match.annee_universitaire,
            'numero_semaine':      match.numero_semaine,
            'date_reference':      str(match.date),
            'source':              'exact' if match.date == today else 'fallback',
        })

    @action(detail=False, methods=['post'], url_path='generer')
    def generer(self, request):
        """Genere automatiquement les semaines pour une annee universitaire."""
        s = GenerateSemainesSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        # Supprimer les semaines existantes pour cette annee/semestre
        Semaine.objects.filter(
            annee_universitaire=d['annee_universitaire'],
            type_semestre=d['type_semestre'],
        ).delete()

        # Pre-charger les jours (FR) : map weekday() -> Jour
        jours_fr = ['Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi', 'Dimanche']
        jour_pk_by_label = {j.jour: j.pk for j in Jour.objects.all()}

        semaines_creees = []
        current = d['date_debut']
        num = 1
        while current <= d['date_fin']:
            label = jours_fr[current.weekday()]
            jour_pk = jour_pk_by_label.get(label)
            if jour_pk is None:
                continue  # jour inconnu : skip cette ligne
            semaines_creees.append(Semaine(
                numero_semaine=num,
                jour_fk_id=jour_pk,
                date=current,
                annee_universitaire=d['annee_universitaire'],
                type_semestre=d['type_semestre'],
            ))
            current += timedelta(days=7)
            num += 1

        Semaine.objects.bulk_create(semaines_creees)
        logger.info('Generated %d semaines for %s', len(semaines_creees), d['annee_universitaire'])
        return Response({'created': len(semaines_creees)}, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='ajouter-batch')
    def ajouter_batch(self, request):
        """Ajoute N semaines complètes (1 ligne par jour de la table Jour) en
        repartant après les semaines déjà créées pour (annee_universitaire,
        type_semestre). Réplique la sémantique du legacy add_semaine :
        date_debut recalée au lundi, offset auto sur le dernier lundi existant."""
        s = AddBatchSemainesSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        annee = d['annee_universitaire']
        typ   = d['type_semestre']
        nb    = d['nombre_semaines']

        debut_monday = d['date_debut'] - timedelta(days=d['date_debut'].weekday())

        qs = Semaine.objects.filter(annee_universitaire=annee, type_semestre=typ)
        if qs.exists():
            numero_max = qs.order_by('-numero_semaine').first().numero_semaine
            dernier_lundi = (qs.filter(jour_fk__jour__iexact='Lundi')
                              .order_by('-date')
                              .values_list('date', flat=True)
                              .first())
            next_from_existing = (dernier_lundi + timedelta(weeks=1)
                                  if dernier_lundi else debut_monday)
            start_of_week = max(debut_monday, next_from_existing)
        else:
            numero_max = 0
            start_of_week = debut_monday

        jours = list(Jour.objects.order_by('id'))
        if not jours:
            return Response({'detail': "Aucun jour défini dans la table Jour."},
                            status=status.HTTP_400_BAD_REQUEST)

        to_create = []
        for i in range(nb):
            num_sem = numero_max + 1 + i
            for j, jour in enumerate(jours):
                jour_date = start_of_week + timedelta(weeks=i, days=j)
                to_create.append(Semaine(
                    numero_semaine=num_sem,
                    jour_fk_id=jour.pk,
                    date=jour_date,
                    annee_universitaire=annee,
                    type_semestre=typ,
                ))

        Semaine.objects.bulk_create(to_create)
        logger.info('Added batch of %d weeks (%d rows) for %s/%s starting %s',
                    nb, len(to_create), annee, typ, start_of_week)
        return Response({
            'created':       len(to_create),
            'weeks':         nb,
            'start_of_week': str(start_of_week),
            'numero_debut':  numero_max + 1,
            'numero_fin':    numero_max + nb,
        }, status=status.HTTP_201_CREATED)

    # ── POST /api/v1/parametres/semaines/grouped/ ─────────────────────────
    @action(detail=False, methods=['get'], url_path='grouped')
    def grouped(self, request):
        """Retourne les semaines regroupees par 'cle de semaine' avec leurs dates.

        Sortie : liste triee chronologiquement de semaines, chacune avec :
            { numero_semaine, type_semaine, type_semaine_display, description,
              date_debut, date_fin, annee_universitaire, type_semestre,
              ids: [pk des 5-7 lignes Jour de cette semaine] }

        Une 'semaine' est identifiee par :
          - (annee, type_semestre, numero_semaine) si type=cours et numero non null
          - (annee, type_semestre, min(date), max(date)) si type != cours (numero NULL)

        Permet a la page parametres/semaines d'afficher un tableau lisible :
        une ligne par semaine reelle au lieu de 5-7 lignes par jour.
        """
        annee = request.query_params.get('annee_universitaire')
        ts    = request.query_params.get('type_semestre')
        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)

        qs = Semaine.objects.filter(annee_universitaire=annee).select_related('jour_fk')
        if ts:
            qs = qs.filter(type_semestre=ts)

        # Regrouper en memoire (volume limite : ~200-300 lignes par annee/semestre)
        groups: dict = {}
        for s in qs.order_by('date', 'jour_fk_id'):
            # Cle : si type=cours -> par numero_semaine, sinon par (date_debut_semaine)
            if s.type_semaine == Semaine.TYPE_COURS and s.numero_semaine is not None:
                key = ('cours', s.type_semestre, s.numero_semaine)
            else:
                # Aligner sur le lundi de la semaine ISO pour regrouper les 5-7 jours
                week_anchor = s.date - timedelta(days=s.date.weekday())
                key = ('autre', s.type_semestre, week_anchor.isoformat())
            g = groups.setdefault(key, {
                'numero_semaine':       s.numero_semaine,
                'type_semaine':         s.type_semaine,
                'type_semaine_display': s.get_type_semaine_display(),
                'description':          s.description,
                'date_debut':           s.date,
                'date_fin':             s.date,
                'annee_universitaire':  s.annee_universitaire,
                'type_semestre':        s.type_semestre,
                'ids':                  [],
            })
            g['ids'].append(s.pk)
            if s.date < g['date_debut']:
                g['date_debut'] = s.date
            if s.date > g['date_fin']:
                g['date_fin'] = s.date

        # Trier chronologiquement par date_debut
        result = sorted(groups.values(), key=lambda x: (x['type_semestre'], x['date_debut']))
        for r in result:
            r['date_debut'] = r['date_debut'].isoformat()
            r['date_fin']   = r['date_fin'].isoformat()
        return Response(result)

    # ── POST /api/v1/parametres/semaines/marquer-type/ ────────────────────
    @action(detail=False, methods=['post'], url_path='marquer-type')
    def marquer_type(self, request):
        """Marque une semaine entiere avec un type (cours/ferie/vacances/examen)
        et propage la renumerotation aux semaines suivantes.

        Body attendu :
            {
              "annee_universitaire": "2025-2026",
              "type_semestre":       "I" ou "P",
              "date_debut":          "2025-10-14"  (date du lundi de la semaine cible),
              "nouveau_type":        "ferie"/"cours"/"vacances"/"examen",
              "description":         "Mawlid"  (optionnel)
            }

        Regle de cascade :
            - Vers non-cours : numero_semaine <- NULL, type <- nouveau_type ;
              toutes les semaines 'cours' au-dela voient numero_semaine -= 1.
            - Vers cours : reinsertion a la position chronologique correcte ;
              les semaines 'cours' suivantes voient numero_semaine += 1.

        Garde-fous (refus si non respectes) :
            1. La semaine cible ne doit pas avoir de Suivie generee.
            2. Aucune semaine au-dela (par numero ou date) ne doit avoir de
               Suivie generee (sinon la renumerotation casserait les FK).
            3. Le type_semestre doit etre coherent.
        """
        # ── Validation des inputs ─────────────────────────────────────────
        annee         = request.data.get('annee_universitaire')
        type_semestre = request.data.get('type_semestre')
        date_debut    = request.data.get('date_debut')
        nouveau_type  = request.data.get('nouveau_type')
        description   = (request.data.get('description') or '').strip()

        if not all([annee, type_semestre, date_debut, nouveau_type]):
            return Response({'error': 'Champs requis : annee_universitaire, '
                                       'type_semestre, date_debut, nouveau_type.'},
                            status=400)

        types_valides = [t[0] for t in Semaine.TYPES_SEMAINE]
        if nouveau_type not in types_valides:
            return Response({'error': f'nouveau_type invalide. Valeurs : {types_valides}.'},
                            status=400)

        from datetime import date as _date
        try:
            date_debut_obj = _date.fromisoformat(date_debut)
        except (TypeError, ValueError):
            return Response({'error': 'date_debut doit etre au format YYYY-MM-DD.'},
                            status=400)

        # Aligner sur le lundi (semaine ISO) pour identifier la semaine entiere
        week_anchor = date_debut_obj - timedelta(days=date_debut_obj.weekday())
        week_end    = week_anchor + timedelta(days=6)

        # ── Identifier la semaine cible (5-7 rows Jour) ───────────────────
        target_rows = list(Semaine.objects.filter(
            annee_universitaire=annee,
            type_semestre=type_semestre,
            date__gte=week_anchor,
            date__lte=week_end,
        ))

        if not target_rows:
            return Response({
                'error': f"Aucune semaine trouvee pour {annee} / semestre "
                         f"{type_semestre} commencant le {week_anchor}.",
            }, status=404)

        # Tous les rows de la semaine cible doivent partager le meme type/numero
        # actuel (invariant). Sinon, etat incoherent, on refuse.
        types_actuels = {r.type_semaine for r in target_rows}
        if len(types_actuels) > 1:
            return Response({
                'error': "Etat incoherent : les jours de cette semaine ont des "
                         "types differents. Contactez un administrateur.",
            }, status=409)

        type_actuel        = target_rows[0].type_semaine
        numero_actuel      = target_rows[0].numero_semaine
        description_actuel = target_rows[0].description

        # Idempotence : pas de changement effectif demande
        if type_actuel == nouveau_type and description_actuel == description:
            return Response({
                'changed':      False,
                'message':      'Aucun changement (type et description identiques).',
                'numero_semaine': numero_actuel,
            })

        # ── Import differe pour eviter cycle d'import ─────────────────────
        from apps.suivi.models import Suivie

        # ── Garde-fou 1 : la semaine cible ne doit pas avoir de Suivie ────
        if numero_actuel is not None and Suivie.objects.filter(
            annee_universitaire=annee,
            type_semestre=type_semestre,
            numero_semaine=numero_actuel,
        ).exists():
            return Response({
                'error': (
                    f"Modification impossible : la semaine {numero_actuel} a deja "
                    f"un suivi genere. Supprimez d'abord ce suivi dans "
                    f"/dashboard/suivi/ajouter (suppression LIFO)."
                ),
            }, status=409)

        # ── Garde-fou 2 : aucune semaine au-dela ne doit avoir de Suivie ──
        # On determine "au-dela" :
        #   - Si on PASSE vers non-cours : toutes les cours dont num > numero_actuel
        #   - Si on PASSE vers cours    : toutes les cours dont num >= numero_insertion
        #   Dans les 2 cas, on regarde aussi les rows futures par date pour les
        #   semaines non-cours (qui n'ont pas de numero).
        if type_actuel == Semaine.TYPE_COURS and numero_actuel is not None:
            # Vers non-cours : verifier les Suivie au-dela du numero_actuel
            max_suivie_after = Suivie.objects.filter(
                annee_universitaire=annee,
                type_semestre=type_semestre,
                numero_semaine__gt=numero_actuel,
            ).aggregate(m=Max('numero_semaine'))['m']
            if max_suivie_after is not None:
                return Response({
                    'error': (
                        f"Modification impossible : des suivis ont deja ete "
                        f"generes pour les semaines suivantes (jusqu'a S{max_suivie_after}). "
                        f"Supprimez-les d'abord en LIFO via /dashboard/suivi/ajouter."
                    ),
                }, status=409)
        else:
            # Vers cours (reinsertion) : verifier les Suivie posterieures par date.
            # On determine le numero d'insertion : c'est le numero de la 1ere
            # semaine cours dont date_debut > week_anchor.
            cours_apres = (Semaine.objects
                           .filter(annee_universitaire=annee,
                                   type_semestre=type_semestre,
                                   type_semaine=Semaine.TYPE_COURS,
                                   date__gt=week_end)
                           .order_by('date')
                           .first())
            num_insertion = (cours_apres.numero_semaine
                             if cours_apres and cours_apres.numero_semaine is not None
                             else None)
            if num_insertion is not None and Suivie.objects.filter(
                annee_universitaire=annee,
                type_semestre=type_semestre,
                numero_semaine__gte=num_insertion,
            ).exists():
                return Response({
                    'error': (
                        f"Reinsertion impossible : des suivis ont deja ete "
                        f"generes pour les semaines a partir de S{num_insertion}. "
                        f"Supprimez-les d'abord en LIFO via /dashboard/suivi/ajouter."
                    ),
                }, status=409)

        # ── Cascade atomique ──────────────────────────────────────────────
        with transaction.atomic():
            target_ids = [r.pk for r in target_rows]

            if type_actuel == Semaine.TYPE_COURS and nouveau_type != Semaine.TYPE_COURS:
                # CAS A : cours -> non-cours
                #   1. Sortir la semaine cible de la sequence (numero=NULL)
                #   2. Decrementer toutes les cours dont num > numero_actuel
                Semaine.objects.filter(pk__in=target_ids).update(
                    numero_semaine=None,
                    type_semaine=nouveau_type,
                    description=description,
                )
                Semaine.objects.filter(
                    annee_universitaire=annee,
                    type_semestre=type_semestre,
                    type_semaine=Semaine.TYPE_COURS,
                    numero_semaine__gt=numero_actuel,
                ).update(numero_semaine=F('numero_semaine') - 1)
                renumerotation = 'decrement'

            elif type_actuel != Semaine.TYPE_COURS and nouveau_type == Semaine.TYPE_COURS:
                # CAS B : non-cours -> cours (reinsertion chronologique)
                #   1. Trouver la position d'insertion (numero de la 1ere cours apres)
                #   2. Incrementer toutes les cours >= ce numero
                #   3. Assigner ce numero a la semaine cible
                cours_apres = (Semaine.objects
                               .filter(annee_universitaire=annee,
                                       type_semestre=type_semestre,
                                       type_semaine=Semaine.TYPE_COURS,
                                       date__gt=week_end)
                               .order_by('date')
                               .first())
                if cours_apres and cours_apres.numero_semaine is not None:
                    num_insertion = cours_apres.numero_semaine
                    Semaine.objects.filter(
                        annee_universitaire=annee,
                        type_semestre=type_semestre,
                        type_semaine=Semaine.TYPE_COURS,
                        numero_semaine__gte=num_insertion,
                    ).update(numero_semaine=F('numero_semaine') + 1)
                else:
                    # Pas de cours apres : prendre max+1
                    max_num = (Semaine.objects.filter(
                        annee_universitaire=annee,
                        type_semestre=type_semestre,
                        type_semaine=Semaine.TYPE_COURS,
                    ).aggregate(m=Max('numero_semaine'))['m']) or 0
                    num_insertion = max_num + 1
                Semaine.objects.filter(pk__in=target_ids).update(
                    numero_semaine=num_insertion,
                    type_semaine=Semaine.TYPE_COURS,
                    description=description,
                )
                renumerotation = 'increment'

            elif type_actuel == Semaine.TYPE_COURS and nouveau_type == Semaine.TYPE_COURS:
                # CAS C : cours -> cours (changement de description uniquement)
                Semaine.objects.filter(pk__in=target_ids).update(description=description)
                renumerotation = 'none'

            else:
                # CAS D : non-cours -> non-cours (changement de type ou description)
                Semaine.objects.filter(pk__in=target_ids).update(
                    type_semaine=nouveau_type,
                    description=description,
                )
                renumerotation = 'none'

            logger.info(
                'Semaine type change [%s/%s, anchor=%s]: %s (S%s) -> %s (renum=%s)',
                annee, type_semestre, week_anchor, type_actuel, numero_actuel,
                nouveau_type, renumerotation,
            )

        # Recharger pour retourner l'etat final
        target_rows = list(Semaine.objects.filter(pk__in=target_ids))
        return Response({
            'changed':           True,
            'ancien_type':       type_actuel,
            'nouveau_type':      nouveau_type,
            'ancien_numero':     numero_actuel,
            'nouveau_numero':    target_rows[0].numero_semaine,
            'renumerotation':    renumerotation,
            'description':       description,
            'lignes_modifiees':  len(target_ids),
        })


class PaiementViewSet(AuditMixin, viewsets.ModelViewSet):
    queryset           = Paiement.objects.all()
    serializer_class   = PaiementSerializer
    permission_classes = [IsAdmin]
    filter_backends    = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields   = ['type']
    search_fields      = ['type']
    ordering_fields    = ['type', 'date_debut']
    pagination_class   = StandardPagination

    def get_permissions(self):
        # Lecture libre auth — taux paiement consultes par avancement/vacations.
        # Ecriture admin uniquement.
        if self.action in ('list', 'retrieve', 'taux_actuel'):
            return [IsAuthenticated()]
        return super().get_permissions()

    @action(detail=False, methods=['get'], url_path='taux-actuel')
    def taux_actuel(self, request):
        """Retourne le taux actuel par type de séance."""
        from django.utils import timezone
        today = timezone.now().date()
        result = {}
        for p in Paiement.objects.order_by('type', '-date_debut'):
            if p.type not in result:
                result[p.type] = p.taux
        return Response(result)


class RamadanViewSet(AuditMixin, viewsets.ModelViewSet):
    queryset           = Ramadan.objects.all()
    serializer_class   = RamadanSerializer
    permission_classes = [IsAdmin]
    pagination_class   = StandardPagination

    def get_permissions(self):
        # Lecture libre auth — periodes ramadan consultees par les emplois (creneaux speciaux).
        if self.action in ('list', 'retrieve'):
            return [IsAuthenticated()]
        return super().get_permissions()


# class InstitutionViewSet(AuditMixin, SelectAllMixin, viewsets.ModelViewSet):
#     queryset           = Institution.objects.all()
#     serializer_class   = InstitutionSerializer
#     permission_classes = [IsAdmin]
#     pagination_class   = NoPagination

from rest_framework.decorators import action
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from rest_framework import viewsets

class InstitutionViewSet(AuditMixin, SelectAllMixin, viewsets.ModelViewSet):
    # On donne un vrai QuerySet pour que le ViewSet fonctionne correctement
    queryset           = Institution.objects.all()
    serializer_class   = InstitutionSerializer
    permission_classes = [IsAdmin]
    pagination_class   = NoPagination

    def get_permissions(self):
        # Lecture libre auth — utilisee pour le branding, headers PDF, etc.
        # Ecriture (modifier infos institution, logos) reservee admin.
        if self.action in ('list', 'retrieve', 'all'):
            return [IsAuthenticated()]
        return super().get_permissions()

    # Cette action crée une route /institutions/active/ — publique (page login)
    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def active(self, request):
        institution = get_object_or_404(Institution, est_principale=True)
        serializer = self.get_serializer(institution, context={'request': request})
        return Response(serializer.data)