"""
Avancement — vues de statistiques agrégées (pas de modèles propres).
Toutes les données viennent de Suivie + Emplois.
"""
import logging
import os
from datetime import date
from collections import defaultdict

from django.db.models import Sum, Count, F, Q, FloatField, ExpressionWrapper, Min
from django.db.models.functions import TruncMonth
from django.http import HttpResponse
from django.template.loader import get_template
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.vacation.models import Vacation
from core.libelle_dept import filiere_distingue, format_depts_compact
from core.types_seance import TYPES_SURVEILLANCE
from core.permissions import RBACPermission
from apps.suivi.models import Suivie, SuiviePointage, ChargeInstitution
from apps.emplois.models import Emplois
from apps.prof.models import Prof, TYPES_CHARGE_REG
from apps.departement.models import Departement
from apps.em.models import EM as EMModel, EM
from apps.em.volumes import volumes_par_em
from apps.parametres.models import Seance, Paiement

logger = logging.getLogger('siga')


# ── Helpers partages ────────────────────────────────────────────────────────

def _ts_label(s) -> str:
    """Libelle type_seance pour un Suivie/SuiviePointage via FK type_seance_fk."""
    if getattr(s, 'type_seance_fk_id', None) and getattr(s, 'type_seance_fk', None):
        return s.type_seance_fk.type_seance or ''
    return ''


def _dates_epreuves_declarees(em, annee) -> dict:
    """
    Les dates des épreuves DÉCLARÉES de cet EM qui sont passées, par type.

    Depuis que l'épreuve est un objet à part — sa durée ne tombe pas sur la
    grille de 1h30, elle convoque plusieurs classes, elle n'a ni salle ni
    enseignant —, elle ne produit plus de séance, donc plus de pointage. Sans
    cette source, un DS programmé et passé compterait zéro dans l'avancement.

    Ce qui la rend tenue est sa DATE PASSÉE : le pointage « Fait » ne vaut que
    pour une séance, et l'attendre ici n'en rendrait jamais aucune.

    Les semestres saisis avant ce modèle n'ont, eux, que des séances — d'où la
    cohabitation des deux sources chez l'appelant. La clé étant la date dans
    les deux cas, une épreuve présente des deux côtés n'est comptée qu'une fois.
    """
    resultat = {'DS': set(), 'EF': set(), 'ER': set()}

    # `matiere_ipgei` est l'envers du miroir `Matiere.em_miroir`. Absent sur un
    # EM du socle qui ne reflète aucune matière de la maquette — CHIM, EPS,
    # hérités d'avant le fork : il n'a alors aucune épreuve, et c'est juste.
    matiere = getattr(em, 'matiere_ipgei', None)
    if matiere is None:
        return resultat

    from django.utils import timezone

    from apps.ipgei.models import EpreuveIPGEI

    # Pas de filtre sur le semestre : une matière n'appartient qu'à un seul, il
    # est donc déjà fixé par `matiere_id`. L'ajouter ne pourrait que retrancher
    # à tort, les deux couches ne nommant pas leurs semestres de la même façon.
    qs = EpreuveIPGEI.objects.filter(
        matiere_id=matiere.pk,
        date__lte=timezone.localdate(),
        session__semestre__annee_universitaire=annee,
    )

    for type_epreuve, date_epreuve in qs.values_list('type_epreuve', 'date'):
        if type_epreuve in resultat:
            resultat[type_epreuve].add(date_epreuve)
    return resultat


def _dep_key(s):
    """Cle departement non vide pour un Suivie (FK uniquement)."""
    if s.departement_id:
        return str(s.departement_id)
    return '_global_'


def _compute_avancement_em(annee, type_semestre, semestre_id=None, ems=None):
    """
    Calcule l'avancement complet (planification / realisation / %) pour tous les EMs.
    Utilise Suivie (toutes les seances = realisees) et MAX inter-departements atomiques.
    """
    # EMs filtres par type de semestre et annee
    # Le rattachement d'un EM a un departement sert ici a le dater. Une matiere
    # IPGEI, elle, n'a pas d'annee : la maquette de prepa vaut pour toutes les
    # promotions, seul son ancrage de niveau a un sens. La filtrer sur l'annee
    # de son ancrage viderait donc l'ecran des qu'une classe est creee pour
    # l'annee suivante. Les heures REALISEES restent, elles, filtrees sur
    # l'annee demandee, plus bas.
    #
    # Et une matiere qui a PERDU son ancre — la classe qui la portait a ete
    # supprimee, `SET_NULL` a fait le reste — ne doit pas disparaitre pour
    # autant : elle reste dans la maquette. On la retient des lors que son
    # NIVEAU a une classe active cette annee ; sans classe, elle ne concerne
    # personne et reste hors ecran. C'est ce qui a vide l'ecran « pour tous,
    # administrateur compris » apres une purge des annees precedentes.
    base_em = (
        EMModel.objects
        .filter(semestre__type_semestre=type_semestre)
        .filter(Q(departement__annee_universitaire=annee)
                | Q(departement__classe_ipgei__isnull=False)
                | Q(departement__isnull=True,
                    # …et seulement si c'est bien le MIROIR d'une matiere IPGEI
                    # active : le catalogue socle garde des EM orphelins (CHIM,
                    # EPS…) sans matiere derriere, qui surgiraient sinon.
                    matiere_ipgei__isnull=False, matiere_ipgei__actif=True,
                    semestre__niveau_semestre__departements__annee_universitaire=annee,
                    semestre__niveau_semestre__departements__classe_ipgei__isnull=False,
                    semestre__niveau_semestre__departements__classe_ipgei__actif=True))
        .select_related('semestre')
    )
    if semestre_id:
        base_em = base_em.filter(semestre_id=semestre_id)
    # Perimetre disciplinaire : un chef de departement ou un coordinateur ne voit
    # que l'avancement de SES matieres. `ems` vaut None pour tous les autres —
    # direction, scolarite —, qui gardent la vue complete.
    if ems is not None:
        base_em = base_em.filter(id__in=ems)

    # Dedoublonner par code_em (prendre le premier ID)
    first_ids = (
        base_em.values('code_em')
        .annotate(first_id=Min('id'))
        .values_list('first_id', flat=True)
    )
    liste_em = (
        EMModel.objects
        .filter(id__in=list(first_ids))
        .select_related('semestre')
        .order_by('semestre__code_semestre', 'code_em')
    )

    result = []
    FAMILY_FAM = {
        'CM': 'CM', 'CM2': 'CM',
        'TD': 'TD', 'TD2': 'TD',
        'TP': 'TP', 'TP2': 'TP',
        'PR': 'PR', 'PR2': 'PR',
    }
    # Le volume PREVU est celui gele pour l'annee regardee, pas celui de la
    # maquette d'aujourd'hui : une retouche de maquette ne doit pas reecrire le
    # pourcentage d'une annee revolue. Repli sur l'EM quand rien n'est gele.
    volumes = volumes_par_em(annee)

    for em in liste_em:
        vol = volumes.get(em.id) or em
        # Source de verite = SuiviePointage avec commentaire='Fait'.
        # Filtre commentaire='Fait' : on compte uniquement ce que l'utilisateur
        # a explicitement marque comme realise dans /suivi/remplissage.
        sp_qs = SuiviePointage.objects.filter(
            annee_universitaire=annee, em_id=em.id, commentaire='Fait',
        ).select_related('type_seance_fk').prefetch_related('departements')
        if semestre_id:
            sp_qs = sp_qs.filter(semestre_id=semestre_id)

        # COMBIEN d'épreuves de chaque type ont eu lieu, et non « y en a-t-il
        # eu une ». Un semestre porte couramment deux ou trois DS ; la croix
        # posée dès le premier laissait croire l'évaluation faite, et rien ne
        # disait qu'il en manquait deux.
        #
        # Ce qui identifie UNE épreuve est le couple (type, DATE), non la
        # séance : une épreuve de niveau se tient dans plusieurs classes le
        # même jour, donc sur plusieurs séances et plusieurs pointages. C'est
        # déjà la clé retenue par `ipgei.secretariat.epreuves_tenues`, et
        # compter les séances afficherait ici « 5 DS » là où il y en a eu un.
        dates_epreuve = {'DS': set(), 'EF': set(), 'ER': set()}

        # L'avancement d'une matiere est la progression du groupe LE PLUS
        # AVANCE, pas la somme des groupes : un TP dedouble sur G1 et G2 fait
        # avancer le programme d'une seance, pas de deux.
        #
        # Deux niveaux de regroupement, car deux formes de dedoublement :
        #   * DANS une unite de planification, une seance pedagogique est
        #     identifiee par (semaine, jour, creneau, type) — deux profs ou deux
        #     salles sur le meme creneau ne comptent qu'une fois ;
        #   * ENTRE unites, on prend le maximum. C'est ce qui rattrape le
        #     dedoublement SEQUENTIEL (G1 le mardi, G2 le jeudi), que la seule
        #     dedup par creneau laissait passer : il affichait 200 %.
        #
        # Ce second niveau n'etait pas calculable avant que les sous-groupes de
        # TP ne deviennent des departements a part entiere : les deux moitiees
        # d'un TP portaient la meme unite, donc rien ne les distinguait de deux
        # seances reellement differentes.
        heures = defaultdict(lambda: defaultdict(float))   # famille -> unite -> heures
        vues   = set()
        for s in sp_qs:
            label = _ts_label(s)
            duree = s.duree_creneau or 0

            if label in dates_epreuve and s.date_suivie:
                dates_epreuve[label].add(s.date_suivie)

            # DS, EF et ER ne DÉDUISENT RIEN de la charge de la matière.
            #
            # Le devoir surveillé comptait dans les heures de TD — il se tient
            # sur un créneau de TD, et l'assimilation venait de là. Mais un
            # devoir n'avance pas le programme : la matière paraissait plus
            # loin qu'elle ne l'était, et le volume de TD restant à faire
            # diminuait d'autant. Deux devoirs suffisaient à annoncer un
            # semestre bouclé.
            #
            # Les trois types sont désormais comptés à part, dans les colonnes
            # « Évaluations » qui en donnent le NOMBRE. Les laisser aussi dans
            # les heures serait les compter deux fois.
            if label in dates_epreuve:
                continue
            if label in FAMILY_FAM:
                famille = FAMILY_FAM[label]
            else:
                continue

            # Une seance peut couvrir plusieurs unites (cours commun a deux
            # groupes) : chacune progresse d'autant. Le sentinelle 0 couvre les
            # pointages sans departement, comptes comme une unite a part.
            for unite in [d.id for d in s.departements.all()] or [0]:
                cle = (unite, s.numero_semaine, s.jour_fk_id, s.creneau_fk_id, label)
                if cle in vues:
                    continue
                vues.add(cle)
                heures[famille][unite] += duree

        real = {
            famille: max(heures[famille].values(), default=0.0)
            for famille in ('CM', 'TD', 'TP', 'PR')
        }

        # Les épreuves DÉCLARÉES au calendrier, en plus de celles déduites du
        # pointage. Depuis qu'une épreuve n'est plus une séance, elle ne
        # produit aucun pointage : sans cette seconde source, un DS programmé
        # et passé compterait zéro. La clé est la même — la date —, si bien
        # qu'une épreuve présente des deux côtés n'est comptée qu'une fois.
        for type_epreuve, dates in _dates_epreuves_declarees(em, annee).items():
            dates_epreuve[type_epreuve].update(dates)

        def pct(real_val, plan_val):
            return round((real_val / plan_val) * 100) if plan_val > 0 else 0

        result.append({
            'code_em':   em.code_em,
            'intitule':  em.intitule,
            'semestre':  em.semestre.semestre if em.semestre else '—',
            'plan_CM':   vol.CM,
            'plan_TD':   vol.TD,
            'plan_TP':   vol.TP,
            'plan_PR':   vol.PR,
            'real_CM':   real['CM'],
            'real_TD':   real['TD'],
            'real_TP':   real['TP'],
            'real_PR':   real['PR'],
            'pct_CM':    pct(real['CM'], vol.CM),
            'pct_TD':    pct(real['TD'], vol.TD),
            'pct_TP':    pct(real['TP'], vol.TP),
            'pct_PR':    pct(real['PR'], vol.PR),
            # Le NOMBRE d'épreuves tenues. Les anciens drapeaux restent
            # servis : le PDF et les écrans se lisent aussi bien avec, et
            # `> 0` reste la question qu'on leur posait.
            'nb_ds':     len(dates_epreuve['DS']),
            'nb_exam':   len(dates_epreuve['EF']),
            'nb_rat':    len(dates_epreuve['ER']),
            'ds_fait':   bool(dates_epreuve['DS']),
            'exam_fait': bool(dates_epreuve['EF']),
            'rat_fait':  bool(dates_epreuve['ER']),
        })

    return result


def _compute_avancement_prof(annee, prof_id, semestre_id=None):
    """
    Calcule l'avancement par EM pour un professeur (Suivie + Vacation).
    Retourne (liste_em_data, totaux_globaux).
    """
    from apps.vacation.models import Vacation
    from apps.suivi.models import SuiviePointage

    FAMILY_FAM = {
        'CM': 'CM', 'CM2': 'CM',
        'TD': 'TD', 'TD2': 'TD',
        'TP': 'TP', 'TP2': 'TP',
        'PR': 'PR', 'PR2': 'PR',
    }

    # EMs enseignes par ce prof (SuiviePointage + Vacation). On part des pointages
    # — et non de la table Suivie — pour rester coherent avec le portail
    # (emploi/suivi), le detail seance/seance et le calcul de charge.
    point_em_ids = set(
        SuiviePointage.objects.filter(annee_universitaire=annee, prof_id=prof_id)
        .values_list('em_id', flat=True).distinct()
    )
    vac_em_ids = set(
        Vacation.objects.filter(prof_id=prof_id, annee_univ=annee)
        .values_list('em_id', flat=True).distinct()
    )
    all_em_ids = (point_em_ids | vac_em_ids) - {None}

    em_qs = EMModel.objects.filter(id__in=all_em_ids).select_related('semestre')
    if semestre_id:
        em_qs = em_qs.filter(semestre_id=semestre_id)
    em_qs = em_qs.order_by('semestre__code_semestre', 'code_em')

    totaux_globaux = {'CM': 0.0, 'TD': 0.0, 'TP': 0.0, 'PR': 0.0, 'total_eq_CM': 0.0}
    result = []

    volumes = volumes_par_em(annee)

    for em in em_qs:
        vol = volumes.get(em.id) or em
        # Realisation = seances effectivement POINTEES 'Fait' (coherent avec le
        # detail seance/seance et le calcul de charge). Avant : table Suivie
        # (toutes lignes, y compris planifiees/non faites) -> sur-comptage.
        sp_qs = SuiviePointage.objects.filter(
            annee_universitaire=annee, em_id=em.id, prof_id=prof_id, commentaire='Fait',
        ).select_related('type_seance_fk')
        if semestre_id:
            sp_qs = sp_qs.filter(semestre_id=semestre_id)

        vac_qs = Vacation.objects.filter(prof_id=prof_id, em_id=em.id, annee_univ=annee).select_related('type')
        if semestre_id:
            vac_qs = vac_qs.filter(em__semestre_id=semestre_id)

        totaux = {'CM': 0.0, 'TD': 0.0, 'TP': 0.0, 'PR': 0.0}
        # Ici on compte ce que CE professeur a assuré, non ce que la matière a
        # connu : les épreuves déclarées au calendrier n'y entrent pas — elles
        # n'ont pas de surveillant, qui relève de la vacation. Clé par date,
        # comme ailleurs : une épreuve tenue dans cinq classes reste une.
        dates_epreuve = {'DS': set(), 'EF': set(), 'ER': set()}

        for s in sp_qs:
            label = _ts_label(s)
            duree = s.duree_creneau or 0
            if label in dates_epreuve and s.date_suivie:
                dates_epreuve[label].add(s.date_suivie)
            if label in FAMILY_FAM:
                totaux[FAMILY_FAM[label]] += duree
            elif label == 'DS':
                totaux['TD'] += duree

        for v in vac_qs:
            label = v.type.type_seance if v.type else ''
            duree = v.duree or 0
            if label in dates_epreuve and v.date:
                dates_epreuve[label].add(v.date)
            if label in FAMILY_FAM:
                totaux[FAMILY_FAM[label]] += duree
            elif label == 'DS':
                totaux['TD'] += duree

        def pct(real_val, plan_val):
            return round((real_val / plan_val) * 100) if plan_val > 0 else 0

        eq_cm = totaux['CM'] + (totaux['TD'] + totaux['TP'] + totaux['PR']) * 2 / 3
        for fam in ['CM', 'TD', 'TP', 'PR']:
            totaux_globaux[fam] += totaux[fam]
        totaux_globaux['total_eq_CM'] += eq_cm

        result.append({
            'code_em':     em.code_em,
            'intitule':    em.intitule,
            'semestre':    em.semestre.semestre if em.semestre else '—',
            'plan_CM':     vol.CM,
            'plan_TD':     vol.TD,
            'plan_TP':     vol.TP,
            'plan_PR':     vol.PR,
            'real_CM':     totaux['CM'],
            'real_TD':     totaux['TD'],
            'real_TP':     totaux['TP'],
            'real_PR':     totaux['PR'],
            'pct_CM':      pct(totaux['CM'], vol.CM),
            'pct_TD':      pct(totaux['TD'], vol.TD),
            'pct_TP':      pct(totaux['TP'], vol.TP),
            'pct_PR':      pct(totaux['PR'], vol.PR),
            'total_eq_CM': round(eq_cm, 2),
            'nb_ds':       len(dates_epreuve['DS']),
            'nb_exam':     len(dates_epreuve['EF']),
            'nb_rat':      len(dates_epreuve['ER']),
            'ds_fait':     bool(dates_epreuve['DS']),
            'exam_fait':   bool(dates_epreuve['EF']),
            'rat_fait':    bool(dates_epreuve['ER']),
        })

    return result, totaux_globaux


# ── Utilitaire PDF partagé ────────────────────────────────────────────────────
def _render_pdf(template_name, context, filename, orientation='Portrait'):
    """Délègue au renderer PDF partagé (core/pdf_renderer)."""
    from core.pdf_renderer import render_pdf_response
    return render_pdf_response(template_name, context, filename, orientation)


class AvancementEMView(APIView):
    permission_classes = [RBACPermission]
    required_module    = 'avancement'

    def get(self, request):
        annee         = request.query_params.get('annee_universitaire')
        type_semestre = request.query_params.get('type_semestre')  # 'P' ou 'I'
        semestre_id   = request.query_params.get('semestre_id')    # optionnel
        if not annee or not type_semestre:
            return Response({'error': 'annee_universitaire et type_semestre requis.'}, status=400)
        from apps.ipgei.perimetre import ems_autorises
        return Response(_compute_avancement_em(
            annee, type_semestre, semestre_id, ems_autorises(request.user)))


class AvancementProfsView(APIView):
    permission_classes = [RBACPermission]
    required_module    = 'avancement'

    def get(self, request):
        annee   = request.query_params.get('annee_universitaire')
        dept_id = request.query_params.get('departement')
        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)
        qs = Suivie.objects.filter(annee_universitaire=annee)
        if dept_id:
            qs = qs.filter(departement_id=dept_id)
        data = qs.values('prof__id', 'prof__nom', 'type_seance_fk__type_seance').annotate(
            heures=Sum('duree_creneau'),
            montant=Sum(ExpressionWrapper(F('duree_creneau') * F('taux_paiement'), output_field=FloatField())),
        ).order_by('prof__nom', 'type_seance_fk__type_seance')
        # Renommer la cle pour la compat frontend (`type_seance`).
        return Response([{**r, 'type_seance': r.pop('type_seance_fk__type_seance')} for r in list(data)])


class AvancementProfDetailView(APIView):
    permission_classes = [RBACPermission]
    required_module    = 'avancement'

    def get_permissions(self):
        from rest_framework.permissions import IsAuthenticated
        # Un enseignant peut consulter son propre avancement
        if getattr(self.request.user, 'role', None) == 'enseignant':
            return [IsAuthenticated()]
        return super().get_permissions()

    def get(self, request):
        annee       = request.query_params.get('annee_universitaire')
        prof_id     = request.query_params.get('prof')
        semestre_id = request.query_params.get('semestre_id')

        # Si enseignant : forcer son propre prof_id
        if getattr(request.user, 'role', None) == 'enseignant':
            try:
                prof_id = str(request.user.prof_profile.pk)
            except Exception:
                return Response({'error': 'Profil enseignant introuvable.'}, status=403)

        if not annee or not prof_id:
            return Response({'error': 'annee_universitaire et prof requis.'}, status=400)
        items, totaux_globaux = _compute_avancement_prof(annee, prof_id, semestre_id)
        return Response({'items': items, 'totaux_globaux': totaux_globaux})


def _compute_charge_permanents(annee, prof_ids=None):
    """
    Calcule la charge reelle des profs a charge reglementaire :
    permanents, contractuels et enseignants militaires.
    Source : SuiviePointage (commentaire='Fait') + Vacation + ChargeInstitution.
    Retourne (data, totaux_globaux).
    Si prof_ids est fourni, filtre uniquement ces profs.
    """
    from apps.vacation.models import Vacation
    from apps.parametres.models import Paiement as _Paiement, Creneau as _Creneau

    creneau_duree = {c.pk: c.duree for c in _Creneau.objects.all()}

    def get_taux(t):
        obj = _Paiement.objects.filter(type__iexact=t).first()
        return obj.taux if obj else 0

    taux_CM          = get_taux('CM') or 1
    factor_surv      = get_taux('Surveillance') / taux_CM
    factor_enc       = get_taux('Encadrement')  / taux_CM
    factor_miss      = get_taux('Mission')      / taux_CM

    profs = Prof.objects.filter(type__in=TYPES_CHARGE_REG).order_by('nom')
    if prof_ids:
        profs = profs.filter(pk__in=prof_ids)

    # Anti-N+1 : pre-charge les 3 sources en 3 requetes (au lieu de 3 par prof),
    # groupees par prof_id. La logique de calcul par prof (ci-dessous) est inchangee.
    from collections import defaultdict
    profs = list(profs)
    _pids = [p.id for p in profs]

    _sp_by_prof = defaultdict(list)
    for s in SuiviePointage.objects.filter(
        prof_id__in=_pids, annee_universitaire=annee, commentaire='Fait',
    ).select_related('type_seance_fk').distinct():
        _sp_by_prof[s.prof_id].append(s)

    _vac_by_prof = defaultdict(list)
    for v in Vacation.objects.filter(prof_id__in=_pids, annee_univ=annee).select_related('type'):
        _vac_by_prof[v.prof_id].append(v)

    _ci_by_prof = defaultdict(list)
    for ci in ChargeInstitution.objects.filter(
        prof_id__in=_pids, annee_universitaire=annee,
    ).select_related('institution'):
        _ci_by_prof[ci.prof_id].append(ci)

    totaux_globaux = {
        'CM_total': 0.0, 'TD_total': 0.0, 'TP_total': 0.0, 'PR_total': 0.0,
        'Surveillance_total': 0.0, 'Encadrement_total': 0.0, 'Mission_total': 0.0,
        'total_eq_CM': 0.0,
        'total_heures_supp': 0.0,
    }
    data = []

    for prof in profs:
        totaux = {
            'CM_total': 0.0, 'TD_total': 0.0, 'TP_total': 0.0, 'PR_total': 0.0,
            'Surveillance_total': 0.0, 'Encadrement_total': 0.0, 'Mission_total': 0.0,
            'charges_institution': {}, 'total_charge_institution_cm': 0.0,
        }

        # ── SuiviePointage ───────────────────────────────────────────────────
        for s in _sp_by_prof[prof.id]:
            label = _ts_label(s).upper()
            if not label:
                continue

            if s.duree_creneau is not None:
                duree = float(s.duree_creneau)
            elif s.creneau_fk_id:
                duree = float(creneau_duree.get(s.creneau_fk_id, 0))
            else:
                duree = 0.0

            if duree <= 0:
                continue

            if   label == 'CM':   totaux['CM_total']            += duree
            elif label == 'TD':   totaux['TD_total']            += duree
            elif label == 'TP':   totaux['TP_total']            += duree
            elif label == 'PR':   totaux['PR_total']            += duree
            elif label in TYPES_SURVEILLANCE:
                                  totaux['Surveillance_total']  += duree
            elif label == 'ENCADREMENT': totaux['Encadrement_total'] += duree
            elif label in ('MISSION', 'MISSIONS'): totaux['Mission_total'] += duree

        # ── Vacation ─────────────────────────────────────────────────────────
        for v in _vac_by_prof[prof.id]:
            lib   = (v.type.type_seance if v.type else '').lower()
            duree = float(v.duree or 0)
            if   lib == 'cm':           totaux['CM_total']           += duree
            elif lib == 'td':           totaux['TD_total']           += duree
            elif lib == 'tp':           totaux['TP_total']           += duree
            elif lib == 'pr':           totaux['PR_total']           += duree
            elif lib == 'surveillance': totaux['Surveillance_total'] += duree
            elif lib == 'encadrement':  totaux['Encadrement_total']  += duree
            elif lib in ('mission', 'missions'): totaux['Mission_total'] += duree

        # ── ChargeInstitution ─────────────────────────────────────────────────
        charges_par_inst = {}
        total_ci = 0.0
        for ci in _ci_by_prof[prof.id]:
            acro = ci.institution.acronyme
            charges_par_inst[acro] = ci.charge_cm
            total_ci += ci.charge_cm
        totaux['charges_institution']        = charges_par_inst
        totaux['total_charge_institution_cm'] = total_ci

        # ── Équivalent CM ─────────────────────────────────────────────────────
        eq_base = totaux['CM_total'] + (totaux['TD_total'] + totaux['TP_total'] + totaux['PR_total']) * 2 / 3
        total_eq_CM = (
            eq_base
            + totaux['Surveillance_total'] * factor_surv
            + totaux['Encadrement_total']  * factor_enc
            + totaux['Mission_total']      * factor_miss
            + total_ci
        )
        totaux['total_eq_CM'] = round(total_eq_CM, 2)

        charge_nette = (prof.charge or 0) - (prof.decharge or 0)
        difference   = round(total_eq_CM - charge_nette, 2)

        for k in ('CM_total', 'TD_total', 'TP_total', 'PR_total',
                  'Surveillance_total', 'Encadrement_total', 'Mission_total', 'total_eq_CM'):
            totaux_globaux[k] += totaux.get(k, 0)

        # Heures supp = somme des differences positives uniquement (un prof en deficit
        # n'a pas d'heures supp et ne compense pas un autre prof)
        if difference > 0:
            totaux_globaux['total_heures_supp'] += difference

        data.append({
            'prof_nom':             prof.nom,
            'grade':                prof.grade or '',
            'type':                 prof.type,
            'charge':               prof.charge or 0,
            'decharge':             prof.decharge or 0,
            'charge_apres_decharge': charge_nette,
            'totaux':               totaux,
            'difference':           difference,
        })

    return data, totaux_globaux


def _compute_charge_permanents_mensuel(annee, year, month, prof_ids=None):
    """
    Heures supplementaires PAR MOIS (pas en cumul) pour les profs a charge
    reglementaire (permanent / contractuel / militaire).

    Affichage : charge faite dans le mois M uniquement (CM/TD/TP/PR/Surv/Enc/Miss
    et eq_CM du mois). Filtre obligatoire : institution principale (est_principale).
    Les heures faites a ESP/ISE/autres sont exclues — la charge annuelle se remplit
    a l'institution principale, le reste est paye via fiches vacations.

    Heures supp du mois : calcul incremental respectant la charge annuelle.
        hs_avant = max(0, cumul_jusqu'a_fin_du_mois_precedent - charge_nette)
        hs_apres = max(0, cumul_jusqu'a_fin_du_mois          - charge_nette)
        hs_mois  = hs_apres - hs_avant
    Cela garantit que Sum(hs_mois) = hs_annuel exactement.

    La decharge est appliquee : charge_nette = charge - decharge.

    On filtre les profs : on ne garde que ceux qui ont une activite ce mois-la
    (eq_CM_mois > 0) — meme s'ils n'ont pas encore atteint leur charge annuelle.

    Retourne (data, totaux_globaux, acronyme_institution_principale).
    """
    from datetime import date as _date, timedelta as _td
    from calendar import monthrange as _monthrange
    from apps.vacation.models import Vacation
    from apps.parametres.models import (
        Paiement as _Paiement,
        Creneau as _Creneau,
        Institution as _Institution,
    )

    last_day = _monthrange(year, month)[1]
    first_day_of_month = _date(year, month, 1)
    end_date           = _date(year, month, last_day)
    end_prev_month     = first_day_of_month - _td(days=1)

    principale = _Institution.objects.filter(est_principale=True).first()
    empty_totaux = {
        'CM_total': 0.0, 'TD_total': 0.0, 'TP_total': 0.0, 'PR_total': 0.0,
        'Surveillance_total': 0.0, 'Encadrement_total': 0.0, 'Mission_total': 0.0,
        'total_eq_CM': 0.0, 'total_heures_supp': 0.0,
    }
    if not principale:
        return [], empty_totaux, None

    creneau_duree = {c.pk: c.duree for c in _Creneau.objects.all()}

    def get_taux(t):
        obj = _Paiement.objects.filter(type__iexact=t).first()
        return obj.taux if obj else 0

    taux_CM     = get_taux('CM') or 1
    factor_surv = get_taux('Surveillance') / taux_CM
    factor_enc  = get_taux('Encadrement')  / taux_CM
    factor_miss = get_taux('Mission')      / taux_CM

    profs = (
        Prof.objects.filter(type__in=TYPES_CHARGE_REG)
        .select_related('banque')
        .order_by('nom')
    )
    if prof_ids:
        profs = profs.filter(pk__in=prof_ids)

    # Anti-N+1 : pre-charge SP(Fait) + Vacation de l'institution principale pour
    # tous les profs en 2 requetes ; _collect filtre les plages de dates en memoire
    # (le filtre <= end_date exclut deja les dates NULL, comme en SQL).
    from collections import defaultdict
    profs = list(profs)
    _pids = [p.id for p in profs]

    _sp_by_prof = defaultdict(list)
    for s in SuiviePointage.objects.filter(
        prof_id__in=_pids, annee_universitaire=annee,
        commentaire='Fait', institution=principale,
    ).select_related('type_seance_fk'):
        _sp_by_prof[s.prof_id].append(s)

    _vac_by_prof = defaultdict(list)
    for v in Vacation.objects.filter(
        prof_id__in=_pids, annee_univ=annee, institution=principale,
    ).select_related('type'):
        _vac_by_prof[v.prof_id].append(v)

    totaux_globaux = dict(empty_totaux)
    totaux_globaux['taux_CM']       = taux_CM
    totaux_globaux['montant_global'] = 0.0
    data = []

    def _accumulate(totaux, label, duree):
        label_u = (label or '').upper()
        if   label_u == 'CM':   totaux['CM_total']            += duree
        elif label_u == 'TD':   totaux['TD_total']            += duree
        elif label_u == 'TP':   totaux['TP_total']            += duree
        elif label_u == 'PR':   totaux['PR_total']            += duree
        elif label_u in TYPES_SURVEILLANCE:
                                totaux['Surveillance_total']  += duree
        elif label_u == 'ENCADREMENT': totaux['Encadrement_total'] += duree
        elif label_u in ('MISSION', 'MISSIONS'): totaux['Mission_total'] += duree

    def _eq_CM(totaux):
        eq_base = totaux['CM_total'] + (totaux['TD_total'] + totaux['TP_total'] + totaux['PR_total']) * 2 / 3
        return round(
            eq_base
            + totaux['Surveillance_total'] * factor_surv
            + totaux['Encadrement_total']  * factor_enc
            + totaux['Mission_total']      * factor_miss,
            2
        )

    def _collect(prof, date_start, date_end):
        """Aggrege SP(Fait) + Vacation pour ce prof sur une plage de dates,
        institution principale uniquement. Retourne (totaux, eq_CM)."""
        totaux = {
            'CM_total': 0.0, 'TD_total': 0.0, 'TP_total': 0.0, 'PR_total': 0.0,
            'Surveillance_total': 0.0, 'Encadrement_total': 0.0, 'Mission_total': 0.0,
        }
        for s in _sp_by_prof.get(prof.id, []):
            d = s.date_suivie
            if date_start is not None and (d is None or d < date_start):
                continue
            if date_end is not None and (d is None or d > date_end):
                continue
            label = _ts_label(s)
            if not label:
                continue
            if s.duree_creneau is not None:
                duree = float(s.duree_creneau)
            elif s.creneau_fk_id:
                duree = float(creneau_duree.get(s.creneau_fk_id, 0))
            else:
                duree = 0.0
            if duree <= 0:
                continue
            _accumulate(totaux, label, duree)

        for v in _vac_by_prof.get(prof.id, []):
            d = v.date
            if date_start is not None and (d is None or d < date_start):
                continue
            if date_end is not None and (d is None or d > date_end):
                continue
            lib = (v.type.type_seance if v.type else '')
            duree = float(v.duree or 0)
            if duree <= 0:
                continue
            _accumulate(totaux, lib, duree)

        return totaux, _eq_CM(totaux)

    for prof in profs:
        # 1) Charge faite DANS le mois M uniquement (pour affichage)
        totaux_mois, eq_CM_mois = _collect(prof, first_day_of_month, end_date)

        if eq_CM_mois <= 0:
            continue  # Aucune activite ce mois -> on ne l'affiche pas

        # 2) Cumuls (pour calculer la part incrementale des heures supp du mois)
        _, eq_CM_cumul_apres = _collect(prof, None, end_date)
        _, eq_CM_cumul_avant = _collect(prof, None, end_prev_month)

        charge_nette = (prof.charge or 0) - (prof.decharge or 0)
        hs_avant = max(0.0, eq_CM_cumul_avant - charge_nette)
        hs_apres = max(0.0, eq_CM_cumul_apres - charge_nette)
        hs_mois  = round(max(0.0, hs_apres - hs_avant), 2)

        # On n'affiche QUE les profs avec des heures supp ce mois.
        # Un prof qui a fait du travail mais n'a pas (encore) depasse sa charge
        # n'apparait pas.
        if hs_mois <= 0:
            continue

        montant_a_payer = round(hs_mois * taux_CM, 2)

        for k in ('CM_total', 'TD_total', 'TP_total', 'PR_total',
                  'Surveillance_total', 'Encadrement_total', 'Mission_total'):
            totaux_globaux[k] += totaux_mois[k]
        totaux_globaux['total_eq_CM']       += eq_CM_mois
        totaux_globaux['total_heures_supp'] += hs_mois
        totaux_globaux['montant_global']    += montant_a_payer

        data.append({
            'prof_nom':              prof.nom,
            'grade':                 prof.grade or '',
            'type':                  prof.type,
            'charge':                prof.charge or 0,
            'decharge':              prof.decharge or 0,
            'charge_apres_decharge': charge_nette,
            'totaux':                totaux_mois,
            'eq_CM_mois':            eq_CM_mois,
            'heures_supp_mois':      hs_mois,
            'montant_a_payer':       montant_a_payer,
            'numero_de_compte':      prof.numero_de_compte or '',
            'banque_nom':            prof.banque.nom if prof.banque_id else '',
        })

    return data, totaux_globaux, principale.acronyme


class ChargeProfsPermanantsView(APIView):
    permission_classes = [RBACPermission]
    required_module    = 'avancement'

    def get_permissions(self):
        from rest_framework.permissions import IsAuthenticated
        if getattr(self.request.user, 'role', None) == 'enseignant':
            return [IsAuthenticated()]
        return super().get_permissions()

    def get(self, request):
        annee = request.query_params.get('annee_universitaire')
        if not annee:
            try:
                annee = request.user.contexte.annee_universitaire
            except Exception:
                pass
        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)

        # Enseignant : retourner uniquement ses propres données
        if getattr(request.user, 'role', None) == 'enseignant':
            try:
                prof = request.user.prof_profile
            except Exception:
                return Response({'error': 'Profil enseignant introuvable.'}, status=404)
            if prof.type.lower() not in TYPES_CHARGE_REG:
                return Response({'error': 'Réservé aux permanents/contractuels/militaires.'}, status=403)
            data, _ = _compute_charge_permanents(annee, prof_ids=[prof.pk])
            return Response({'data': data})

        data, totaux_globaux = _compute_charge_permanents(annee)
        return Response({'data': data, 'totaux_globaux': totaux_globaux})


class ChargePermanentsMensuelView(APIView):
    """Heures supp cumulees jusqu'a la fin d'un mois donne, filtre sur
    l'institution principale uniquement. Cf _compute_charge_permanents_mensuel."""
    permission_classes = [RBACPermission]
    required_module    = 'avancement'

    def get(self, request):
        annee = request.query_params.get('annee_universitaire')
        year  = request.query_params.get('year')
        month = request.query_params.get('month')
        if not all([annee, year, month]):
            return Response(
                {'error': 'annee_universitaire, year et month requis.'},
                status=400
            )
        try:
            year_i  = int(year)
            month_i = int(month)
            if not (1 <= month_i <= 12):
                raise ValueError
        except (TypeError, ValueError):
            return Response({'error': 'year/month invalides.'}, status=400)

        data, totaux_globaux, inst_acro = _compute_charge_permanents_mensuel(
            annee, year_i, month_i
        )
        return Response({
            'data':                   data,
            'totaux_globaux':         totaux_globaux,
            'institution_principale': inst_acro,
            'annee_universitaire':    annee,
            'year':                   year_i,
            'month':                  month_i,
        })


class RepartitionChargesView(APIView):
    """
    Répartition des charges réalisées (en éq.CM et heures brutes), groupée par
    permanent / vacataire et par type_semestre (I / P).

    Permanent  = prof.type IN TYPES_CHARGE_REG (permanent, contractuel,
                 militaire, agrege, technologue)
    Vacataire  = prof.type = 'vacataire' (UNIQUEMENT)
    Exclus     = personnel_admin, personnel_militaire (ne font pas de cours)

    Types comptés : CM, TD, TP, PR, Encadrement
    Exclus        : Surveillance, Mission, DS, EF, ER, etc.

    Sources :
      - SuiviePointage(commentaire='Fait') : type_semestre direct
      - Vacation : type_semestre via Semaine.date

    Conversion eq_CM : CM × 1, TD/TP/PR × 2/3, Encadrement × 1.

    Retourne data par (type_semestre, groupe) + pourcentages + totaux globaux.
    """
    permission_classes = [RBACPermission]
    required_module    = 'statistiques'

    def get(self, request):
        annee = request.query_params.get('annee_universitaire')
        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)

        from apps.parametres.models import Semaine, Creneau

        TYPES_COMPTES = ('CM', 'TD', 'TP', 'PR', 'Encadrement')
        FACTORS = {'CM': 1.0, 'TD': 2/3, 'TP': 2/3, 'PR': 2/3, 'Encadrement': 1.0}

        def get_groupe(prof_type):
            if prof_type in TYPES_CHARGE_REG:
                return 'permanent'
            if prof_type == 'vacataire':
                return 'vacataire'
            return None  # personnel_admin / personnel_militaire / autres → exclus

        # Buckets initialisation
        buckets = {
            'I': {'permanent': {'eq_cm': 0.0, 'h_brutes': 0.0},
                  'vacataire': {'eq_cm': 0.0, 'h_brutes': 0.0}},
            'P': {'permanent': {'eq_cm': 0.0, 'h_brutes': 0.0},
                  'vacataire': {'eq_cm': 0.0, 'h_brutes': 0.0}},
        }

        # Fallback pour duree manquante
        creneau_duree = {c.pk: c.duree for c in Creneau.objects.all()}

        # 1) SuiviePointage (Fait, types compt., type_semestre direct)
        sp_qs = SuiviePointage.objects.filter(
            annee_universitaire=annee,
            commentaire='Fait',
            type_seance_fk__type_seance__in=TYPES_COMPTES,
        ).select_related('prof', 'type_seance_fk')

        for sp in sp_qs:
            if not sp.prof_id:
                continue
            groupe = get_groupe(sp.prof.type)
            if not groupe:
                continue
            ts = sp.type_semestre
            if ts not in ('I', 'P'):
                continue
            if sp.duree_creneau is not None:
                duree = float(sp.duree_creneau)
            elif sp.creneau_fk_id:
                duree = float(creneau_duree.get(sp.creneau_fk_id, 0))
            else:
                duree = 0.0
            if duree <= 0:
                continue
            type_seance = sp.type_seance_fk.type_seance if sp.type_seance_fk else ''
            factor = FACTORS.get(type_seance, 0)
            buckets[ts][groupe]['eq_cm']    += duree * factor
            buckets[ts][groupe]['h_brutes'] += duree

        # 2) Vacation - dériver type_semestre via Semaine.date
        semaine_map = {
            s.date: s.type_semestre
            for s in Semaine.objects.filter(annee_universitaire=annee)
        }
        v_qs = Vacation.objects.filter(
            annee_univ=annee,
            type__type_seance__in=TYPES_COMPTES,
        ).select_related('prof', 'type')

        for v in v_qs:
            if not v.prof_id:
                continue
            groupe = get_groupe(v.prof.type)
            if not groupe:
                continue
            ts = semaine_map.get(v.date)
            if ts not in ('I', 'P'):
                continue
            duree = float(v.duree or 0)
            if duree <= 0:
                continue
            type_seance = v.type.type_seance if v.type else ''
            factor = FACTORS.get(type_seance, 0)
            buckets[ts][groupe]['eq_cm']    += duree * factor
            buckets[ts][groupe]['h_brutes'] += duree

        # 3) Structurer la sortie avec pourcentages
        def _pct(num, denom):
            return round(num / denom * 100, 1) if denom > 0 else 0.0

        data = []
        for ts in ('I', 'P'):
            perm  = buckets[ts]['permanent']
            vac   = buckets[ts]['vacataire']
            tot_e = perm['eq_cm']    + vac['eq_cm']
            tot_h = perm['h_brutes'] + vac['h_brutes']
            data.append({
                'type_semestre':       ts,
                'type_semestre_label': 'Semestres Impairs' if ts == 'I' else 'Semestres Pairs',
                'permanent': {
                    'eq_cm':        round(perm['eq_cm'], 2),
                    'h_brutes':     round(perm['h_brutes'], 2),
                    'pct_eq_cm':    _pct(perm['eq_cm'], tot_e),
                    'pct_h_brutes': _pct(perm['h_brutes'], tot_h),
                },
                'vacataire': {
                    'eq_cm':        round(vac['eq_cm'], 2),
                    'h_brutes':     round(vac['h_brutes'], 2),
                    'pct_eq_cm':    _pct(vac['eq_cm'], tot_e),
                    'pct_h_brutes': _pct(vac['h_brutes'], tot_h),
                },
                'total_eq_cm':    round(tot_e, 2),
                'total_h_brutes': round(tot_h, 2),
            })

        # Totaux globaux
        glob_perm_e = sum(d['permanent']['eq_cm']    for d in data)
        glob_perm_h = sum(d['permanent']['h_brutes'] for d in data)
        glob_vac_e  = sum(d['vacataire']['eq_cm']    for d in data)
        glob_vac_h  = sum(d['vacataire']['h_brutes'] for d in data)
        glob_tot_e  = glob_perm_e + glob_vac_e
        glob_tot_h  = glob_perm_h + glob_vac_h

        totaux_globaux = {
            'permanent_eq_cm':    round(glob_perm_e, 2),
            'vacataire_eq_cm':    round(glob_vac_e, 2),
            'permanent_h_brutes': round(glob_perm_h, 2),
            'vacataire_h_brutes': round(glob_vac_h, 2),
            'permanent_pct_eq':   _pct(glob_perm_e, glob_tot_e),
            'vacataire_pct_eq':   _pct(glob_vac_e, glob_tot_e),
            'permanent_pct_h':    _pct(glob_perm_h, glob_tot_h),
            'vacataire_pct_h':    _pct(glob_vac_h, glob_tot_h),
            'global_eq_cm':       round(glob_tot_e, 2),
            'global_h_brutes':    round(glob_tot_h, 2),
        }

        return Response({
            'annee':          annee,
            'data':           data,
            'totaux_globaux': totaux_globaux,
        })


class SuiviProfView(APIView):
    permission_classes = [RBACPermission]
    required_module    = 'avancement'

    def get(self, request):
        annee   = request.query_params.get('annee_universitaire')
        prof_id = request.query_params.get('prof')
        if not annee or not prof_id:
            return Response({'error': 'annee_universitaire et prof requis.'}, status=400)

        # ── Source 1 : SuiviePointage 'Fait' (planning realise)
        pointages = SuiviePointage.objects.filter(
            annee_universitaire=annee,
            prof_id=prof_id,
            commentaire='Fait',
        ).select_related('em', 'type_seance_fk').prefetch_related(
            'departements__filiere', 'departements__niveau'
        )

        # ── Source 2 : Vacation (saisies manuelles, hors planning)
        vacations = Vacation.objects.filter(
            annee_univ=annee,
            prof_id=prof_id,
        ).select_related('em', 'type').prefetch_related(
            'departements__filiere', 'departements__niveau'
        )

        # ── Map (date -> numero_semaine, type_semestre) pour les vacations
        # qui n'ont pas ces champs stockes. 1 query Semaine pour toute l'annee.
        from apps.parametres.models import Semaine
        sem_map = {}
        for s in Semaine.objects.filter(annee_universitaire=annee):
            sem_map[s.date] = (s.numero_semaine, s.type_semestre)

        # Compte les filieres UNE fois pour tout le lot : la question ne depend
        # pas de la ligne, et la poser a chacune ferait une requete par seance.
        filiere_utile = filiere_distingue()

        rows = []
        # Lignes SuiviePointage
        for p in pointages:
            rows.append({
                'numero_semaine':   p.numero_semaine,
                'date_suivie':      p.date_suivie.isoformat() if p.date_suivie else None,
                'em__intitule':     p.em.intitule if p.em_id and p.em else '',
                'type_seance':      p.type_seance_fk.type_seance if p.type_seance_fk_id and p.type_seance_fk else '',
                'departement__nom': format_depts_compact(p, filiere_utile),
                'duree_creneau':    p.duree_creneau or 0,
                'taux_paiement':    p.taux_paiement or 0,
                'type_semestre':    p.type_semestre or '',
            })
        # Lignes Vacation : derivation numero_semaine via Semaine, EM null = type_seance
        for v in vacations:
            type_lib = v.type.type_seance if v.type_id and v.type else ''
            num_sem, ts = sem_map.get(v.date, (0, ''))
            rows.append({
                'numero_semaine':   num_sem,
                'date_suivie':      v.date.isoformat() if v.date else None,
                'em__intitule':     v.em.intitule if v.em_id and v.em else type_lib,
                'type_seance':      type_lib,
                'departement__nom': format_depts_compact(v, filiere_utile),
                'duree_creneau':    float(v.duree or 0),
                'taux_paiement':    float(v.taux_paiement or 0),
                'type_semestre':    ts,
            })
        # Tri global pour rendu coherent (semestre puis semaine puis date)
        rows.sort(key=lambda r: (r['type_semestre'], r['numero_semaine'], r['date_suivie'] or ''))
        return Response(rows)


class SuiviPointageProfDetailView(APIView):
    """
    Détail séance par séance d'un enseignant :
    SuiviePointage (commentaire='Fait') + Vacation, triés par semaine puis date.
    Accessible aux enseignants (lecture de leurs propres données).
    """
    permission_classes = [RBACPermission]
    required_module    = 'avancement'

    def get_permissions(self):
        from rest_framework.permissions import IsAuthenticated
        if getattr(self.request.user, 'role', None) == 'enseignant':
            return [IsAuthenticated()]
        return super().get_permissions()

    def get(self, request):
        from django.db.models import Min, Max
        from apps.suivi.models import SuiviePointage
        from apps.vacation.models import Vacation
        from apps.parametres.models import Semaine as SemaineParam, Seance

        annee   = request.query_params.get('annee_universitaire')
        prof_id = request.query_params.get('prof')
        ts      = request.query_params.get('type_semestre')  # 'I' ou 'P'

        if getattr(request.user, 'role', None) == 'enseignant':
            try:
                prof_id = str(request.user.prof_profile.pk)
            except Exception:
                return Response({'error': 'Profil enseignant introuvable.'}, status=403)

        if not annee or not prof_id:
            return Response({'error': 'annee_universitaire et prof requis.'}, status=400)

        # Map date → numero semaine (filtre par type_semestre si fourni)
        sem_qs = SemaineParam.objects.filter(annee_universitaire=annee)
        if ts:
            sem_qs = sem_qs.filter(type_semestre=ts)

        date_to_sem = {}
        semaine_dates = {}
        for row in sem_qs.values('numero_semaine').annotate(debut=Min('date'), fin=Max('date')):
            semaine_dates[row['numero_semaine']] = {
                'debut': row['debut'].strftime('%d/%m/%Y') if row['debut'] else '',
                'fin':   row['fin'].strftime('%d/%m/%Y')   if row['fin']   else '',
            }
        for s in sem_qs.values('numero_semaine', 'date'):
            date_to_sem[s['date']] = s['numero_semaine']

        rows = []

        # 1. SuiviePointage (Fait) — filtre par type_semestre
        sp_filter = dict(prof_id=prof_id, annee_universitaire=annee, commentaire='Fait')
        if ts:
            sp_filter['type_semestre'] = ts
        for sp in SuiviePointage.objects.filter(
            **sp_filter
        ).select_related('em', 'type_seance_fk').prefetch_related('departements').order_by('numero_semaine', 'date_suivie'):
            type_label = sp.type_seance_fk.type_seance if sp.type_seance_fk_id and sp.type_seance_fk else ''
            dept_noms  = sorted(d.nom for d in sp.departements.all() if d.nom)
            rows.append({
                'numero_semaine': sp.numero_semaine,
                'date_suivie':    sp.date_suivie.strftime('%Y-%m-%d') if sp.date_suivie else None,
                'em_code':        sp.em.code_em  if sp.em else '',
                'em_intitule':    sp.em.intitule  if sp.em else '—',
                'type_seance':    type_label,
                'departements':   dept_noms,
                'duree_creneau':  sp.duree_creneau or 0,
                'taux_paiement':  sp.taux_paiement or 0,
                'source':         'Suivi',
            })

        # 2. Vacations — restreintes aux dates du semestre si type_semestre fourni
        valid_dates = set(date_to_sem.keys()) if ts else None
        vac_filter = dict(prof_id=prof_id, annee_univ=annee)
        for v in Vacation.objects.filter(
            **vac_filter
        ).select_related('em', 'type').prefetch_related('departements').order_by('date'):
            if valid_dates is not None and v.date not in valid_dates:
                continue
            sem_num    = date_to_sem.get(v.date)
            type_label = v.type.type_seance if v.type else '—'
            dept_noms  = [d.nom for d in v.departements.all()]
            rows.append({
                'numero_semaine': sem_num,
                'date_suivie':    v.date.strftime('%Y-%m-%d') if v.date else None,
                'em_code':        v.em.code_em  if v.em else '',
                'em_intitule':    v.em.intitule  if v.em else '—',
                'type_seance':    type_label,
                'departements':   dept_noms,
                'duree_creneau':  v.duree or 0,
                'taux_paiement':  v.taux_paiement or 0,
                'source':         'Vacation',
            })

        rows.sort(key=lambda r: (r['numero_semaine'] or 0, r['date_suivie'] or ''))

        total_heures  = sum(r['duree_creneau'] for r in rows)
        total_montant = sum(r['duree_creneau'] * r['taux_paiement'] for r in rows)

        return Response({
            'rows':           rows,
            'semaine_dates':  semaine_dates,
            'total_heures':   round(total_heures, 2),
            'total_montant':  round(total_montant, 2),
        })


class StatistiquesProfsView(APIView):
    permission_classes = [RBACPermission]
    required_module    = 'statistiques'

    def get(self, request):
        annee = request.query_params.get('annee_universitaire')
        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)
        data = Suivie.objects.filter(annee_universitaire=annee).values(
            'prof__id', 'prof__nom', 'prof__type'
        ).annotate(
            total_heures=Sum('duree_creneau'),
            total_montant=Sum(ExpressionWrapper(F('duree_creneau') * F('taux_paiement'), output_field=FloatField())),
            nb_seances=Count('id'),
        ).order_by('-total_heures')
        return Response(list(data))


def _ts_label_via_id(seance_id_map, ts_fk_id):
    """Helper pour StatistiquesSemestresView : map type_seance_fk_id -> libelle."""
    return seance_id_map.get(ts_fk_id, '') if ts_fk_id else ''


# class StatistiquesSemestresView(APIView):
#     permission_classes = [RBACPermission]
#     required_module    = 'statistiques'
#
#     def get(self, request):
#         annee = request.query_params.get('annee_universitaire')
#         if not annee:
#             return Response({'error': 'annee_universitaire requis.'}, status=400)
#         data = Suivie.objects.filter(annee_universitaire=annee).values(
#             'semestre__semestre', 'departement__nom'
#         ).annotate(
#             heures=Sum('duree_creneau'),
#             montant=Sum(ExpressionWrapper(F('duree_creneau') * F('taux_paiement'), output_field=FloatField())),
#         ).order_by('semestre__semestre')
#         return Response(list(data))
#
#
# class StatistiquesVacationsView(APIView):
#     permission_classes = [RBACPermission]
#     required_module    = 'statistiques'
#
#     def get(self, request):
#         annee = request.query_params.get('annee_universitaire')
#         if not annee:
#             return Response({'error': 'annee_universitaire requis.'}, status=400)
#         from apps.vacation.models import Vacation
#         from django.db.models.functions import TruncMonth
#         data = Vacation.objects.filter(annee_univ=annee).annotate(
#             mois=TruncMonth('date')
#         ).values('mois', 'prof__nom').annotate(
#             total=Sum(ExpressionWrapper(F('duree') * F('taux_paiement'), output_field=FloatField())),
#         ).order_by('mois', 'prof__nom')
#         return Response(list(data))



from apps.suivi.models import SuiviePointage
from apps.parametres.models import Creneau


class StatistiquesSemestresView(APIView):
    permission_classes = [RBACPermission] # Décommente si tu utilises tes permissions personnalisées
    required_module = 'statistiques'

    def get(self, request):
        # 1. Récupération des paramètres envoyés par le frontend via l'URL
        annee = request.query_params.get('annee_universitaire')
        ts = request.query_params.get('semestres', 'Pairs')

        # Sécurité : vérifier que l'année a bien été transmise
        if not annee:
            return Response(
                {'error': "L'année universitaire est manquante dans la requête."},
                status=400
            )

        # 2. Formatage des variables
        type_semestre = 'P' if ts == "Pairs" else 'I'
        libelle_semestres = "pairs" if ts == "Pairs" else "impairs"

        # 3. Récupération des codes semestres (S1, S2, S3...)
        #
        # Le rattachement d'un EM à un département sert ici à le DATER. Une
        # matière IPGEI, elle, n'a pas d'année : la maquette de prépa vaut pour
        # toutes les promotions, et son ancrage ne lui donne qu'un niveau — il
        # reste sur la première classe créée. La filtrer sur l'année de cet
        # ancrage vidait donc l'écran dès l'ouverture de l'année suivante :
        # aucun label, aucune barre, alors que des séances étaient pointées.
        # Les heures RÉALISÉES restent filtrées sur l'année demandée, plus bas.
        # Même correctif que `_compute_avancement_em`, qui l'avait déjà reçu —
        # y compris la matière SANS ancre dont le niveau a une classe cette année.
        base_em = EM.objects.filter(semestre__type_semestre=type_semestre).filter(
            Q(departement__annee_universitaire=annee)
            | Q(departement__classe_ipgei__isnull=False)
            | Q(departement__isnull=True,
                matiere_ipgei__isnull=False, matiere_ipgei__actif=True,
                semestre__niveau_semestre__departements__annee_universitaire=annee,
                semestre__niveau_semestre__departements__classe_ipgei__isnull=False,
                semestre__niveau_semestre__departements__classe_ipgei__actif=True)
        )
        sem_codes = (
            base_em
            .values_list('semestre__code_semestre', flat=True)
            .distinct()
            .order_by('semestre__code_semestre')
        )

        # Optimisation : dictionnaires de correspondance via FK
        seance_map = {s.id: s.type_seance for s in Seance.objects.all()}
        duree_map = {c.id: c.duree for c in Creneau.objects.all()}

        labels, progress_cm, progress_td, progress_tp, progress_pr = [], [], [], [], []

        # 4. Calculs pour chaque semestre
        for code in sem_codes:
            # Dédoublonnage par code : une même matière ancrée sur deux classes
            # compterait deux fois son volume, et le prévu doublerait.
            ems = EM.objects.filter(
                id__in=list(
                    base_em.filter(semestre__code_semestre=code)
                    .values('code_em').annotate(premier=Min('id'))
                    .values_list('premier', flat=True)
                )
            )

            # Volume horaire PREVU
            planned_cm = ems.aggregate(s=Sum('CM'))['s'] or 0
            planned_td = ems.aggregate(s=Sum('TD'))['s'] or 0
            planned_tp = ems.aggregate(s=Sum('TP'))['s'] or 0
            planned_pr = ems.aggregate(s=Sum('PR'))['s'] or 0

            # Volume horaire REALISE
            em_ids = list(ems.values_list('id', flat=True))
            suivis = SuiviePointage.objects.filter(
                em_id__in=em_ids,
                annee_universitaire=annee,
                type_semestre=type_semestre,
                commentaire="Fait"
            )

            prof_totals = {}
            for s in suivis:
                d = duree_map.get(s.creneau_fk_id, 0)
                lib = (seance_map.get(s.type_seance_fk_id, '') or '').strip().upper()
                if lib not in ['CM', 'TD', 'DS', 'TP', 'PR']:
                    continue
                key = (s.em_id, lib, s.prof_id)
                prof_totals[key] = prof_totals.get(key, 0) + d

            max_totals = {}
            for (em_id, seance_type, prof), total in prof_totals.items():
                k = (em_id, seance_type)
                if k not in max_totals or total > max_totals[k]:
                    max_totals[k] = total

            real_cm = sum(v for (eid, st), v in max_totals.items() if st == 'CM')
            real_td = sum(v for (eid, st), v in max_totals.items() if st in ('TD', 'DS'))
            real_tp = sum(v for (eid, st), v in max_totals.items() if st == 'TP')
            real_pr = sum(v for (eid, st), v in max_totals.items() if st == 'PR')

            # Calcul des pourcentages (sans dépasser 100% si tu le souhaites, mais ici on garde le ratio réel)
            def pct(r, p):
                return round((r / p) * 100, 1) if p and p > 0 else 0

            labels.append(code)
            progress_cm.append(pct(real_cm, planned_cm))
            progress_td.append(pct(real_td, planned_td))
            progress_tp.append(pct(real_tp, planned_tp))
            progress_pr.append(pct(real_pr, planned_pr))

        # 5. Renvoi des données au frontend
        return Response({
            'labels': labels,
            'progress_cm': progress_cm,
            'progress_td': progress_td,
            'progress_tp': progress_tp,
            'progress_pr': progress_pr,
            'libelle_semestres': libelle_semestres,
            'annee_univ': annee,  # Renvoyé en confirmation
        })



# class StatistiquesVacationsView(APIView):
#     permission_classes = [RBACPermission]
#     required_module = 'statistiques'
#
#     def get(self, request):
#         annee = request.query_params.get('annee_universitaire')
#         if not annee:
#             return Response({'error': 'annee_universitaire requis.'}, status=400)
#
#         data = Vacation.objects.filter(annee_univ=annee).annotate(
#             mois=TruncMonth('date')
#         ).values('mois', 'prof__nom').annotate(
#             total=Sum(ExpressionWrapper(F('duree') * F('taux_paiement'), output_field=FloatField())),
#         ).order_by('mois', 'prof__nom')
#
#         return Response(list(data))
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from apps.suivi.models import SuiviePointage
MOIS_FR = {
    1: 'Janvier', 2: 'Février', 3: 'Mars', 4: 'Avril', 5: 'Mai', 6: 'Juin',
    7: 'Juillet', 8: 'Août', 9: 'Septembre', 10: 'Octobre', 11: 'Novembre', 12: 'Décembre'
}


# Helpers feature-flag prof_type_history : voir apps/prof/services.py
from apps.prof.services import (
    use_prof_type_history as _use_prof_type_history,
    payes_a_lheure_ids_for_month as _payes_lheure_ids_for_month,
    TYPES_PAYES_A_LHEURE,
)


class StatistiquesVacationsView(APIView):
    permission_classes = [RBACPermission]  # Ou IsAuthenticated selon ta config
    required_module = 'statistiques'

    def get(self, request):
        annee_univ = request.query_params.get('annee_universitaire')
        if not annee_univ:
            return Response({'error': 'Le paramètre annee_universitaire est requis.'}, status=400)

        # Duree des creneaux : fallback si SuiviePointage.duree_creneau est null.
        duree_map = {c.id: c.duree for c in Creneau.objects.all()}

        # Montant calcule via le taux_paiement STOCKE dans chaque ligne
        # (suivi_suivie_pointage.taux_paiement et vacation_vacation.taux_paiement)
        # — source unique de verite, deja synchronisée par les UPDATE SQL et le
        # code de creation. Plus de re-lookup dans la table paiement.

        # Feature flag : si actif, on calcule les ids PAR MOIS via prof_type_history.
        # Sinon on prend un snapshot statique du statut courant.
        # Inclut TOUS les types payes a l'heure (vacataire + personnel_admin
        # + personnel_militaire), aligne avec /payement/details (fiches mensuelles).
        use_history = _use_prof_type_history()
        vac_ids_static = (
            list(Prof.objects.filter(type__in=TYPES_PAYES_A_LHEURE).values_list('id', flat=True))
            if not use_history else None
        )

        # Mois presents dans Suivie_pointage (on enleve d'office les mois sans suivi)
        mois_rows = (
            SuiviePointage.objects
            .filter(annee_universitaire=annee_univ)
            .values_list('date_suivie__year', 'date_suivie__month')
            .distinct()
            .order_by('date_suivie__year', 'date_suivie__month')
        )

        labels = []
        values = []

        for y, m in mois_rows:
            vac_ids = _payes_lheure_ids_for_month(y, m) if use_history else vac_ids_static

            suivis = SuiviePointage.objects.filter(
                annee_universitaire=annee_univ,
                date_suivie__year=y,
                date_suivie__month=m,
                prof_id__in=vac_ids,
                commentaire="Fait",
            )

            if not suivis.exists():
                continue

            # Montant SuiviePointage : duree × taux_paiement stocke par ligne
            montant_suivis = 0.0
            for s in suivis:
                d = s.duree_creneau if s.duree_creneau is not None else (duree_map.get(s.creneau_fk_id, 0) or 0)
                montant_suivis += float(d or 0) * float(s.taux_paiement or 0)

            # Vacations du même mois — meme logique de filtrage que pour les suivis
            if use_history:
                vacs = Vacation.objects.filter(
                    annee_univ=annee_univ,
                    date__year=y,
                    date__month=m,
                    prof_id__in=vac_ids,
                )
            else:
                vacs = Vacation.objects.filter(
                    annee_univ=annee_univ,
                    date__year=y,
                    date__month=m,
                    prof__type__in=TYPES_PAYES_A_LHEURE,
                )

            # Montant Vacation : duree × taux_paiement stocke par ligne
            # (idem que les fiches paie : la valeur figee dans la BD est la
            # source de verite, indépendamment du type de seance)
            montant_vac = 0.0
            for v in vacs:
                d = v.duree or 0
                montant_vac += float(d) * float(v.taux_paiement or 0)

            montant_total = montant_suivis + montant_vac

            labels.append(f"{MOIS_FR.get(m, str(m))} {y}")
            values.append(round(montant_total, 2))

        return Response({
            'labels': labels,
            'values': values
        })
# ══════════════════════════════════════════════════════════════════════════════
# PDF VIEWS
# ══════════════════════════════════════════════════════════════════════════════

class AvancementEMPDFView(APIView):
    permission_classes = [RBACPermission]
    required_module    = 'avancement'

    def get(self, request):
        from apps.parametres.models import Semestre as SemestreModel
        annee         = request.query_params.get('annee_universitaire')
        type_semestre = request.query_params.get('type_semestre')
        semestre_id   = request.query_params.get('semestre_id')
        if not annee or not type_semestre:
            return Response({'error': 'annee_universitaire et type_semestre requis.'}, status=400)

        from apps.ipgei.perimetre import ems_autorises
        items = _compute_avancement_em(annee, type_semestre, semestre_id,
                                       ems_autorises(request.user))
        if not items:
            return Response({'error': 'Aucune donnée.'}, status=404)

        semestre_label = 'Tous les semestres'
        if semestre_id:
            try:
                sem = SemestreModel.objects.get(pk=semestre_id)
                semestre_label = sem.semestre
            except SemestreModel.DoesNotExist:
                pass
        elif type_semestre == 'P':
            semestre_label = 'Semestres pairs'
        elif type_semestre == 'I':
            semestre_label = 'Semestres impairs'

        context = {
            'items':          items,
            'semestre_label': semestre_label,
            'type_semestre':  type_semestre,
        }
        suffix   = semestre_label.replace(' ', '_')
        filename = f"avancement_em_{suffix}.pdf"
        return _render_pdf('avancement_em_pdf.html', context, filename, orientation='Landscape')


class AvancementProfsPDFView(APIView):
    permission_classes = [RBACPermission]
    required_module    = 'avancement'

    def get(self, request):
        annee   = request.query_params.get('annee_universitaire')
        dept_id = request.query_params.get('departement')
        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)

        qs = Suivie.objects.filter(annee_universitaire=annee)
        if dept_id:
            qs = qs.filter(departement_id=dept_id)

        rows_raw = list(qs.values('prof__id', 'prof__nom', 'type_seance_fk__type_seance').annotate(
            heures=Sum('duree_creneau'),
            montant=Sum(ExpressionWrapper(F('duree_creneau') * F('taux_paiement'), output_field=FloatField())),
        ).order_by('prof__nom', 'type_seance_fk__type_seance'))
        rows = [{**r, 'type_seance': r.pop('type_seance_fk__type_seance')} for r in rows_raw]

        if not rows:
            return Response({'error': 'Aucune donnée.'}, status=404)

        # Grouper par prof
        profs_dict = defaultdict(lambda: {'nom': '', 'rows': [], 'total_heures': 0, 'total_montant': 0})
        for r in rows:
            pid = r['prof__id']
            profs_dict[pid]['nom'] = r['prof__nom'] or '—'
            profs_dict[pid]['rows'].append(r)
            profs_dict[pid]['total_heures']  += r['heures']  or 0
            profs_dict[pid]['total_montant'] += r['montant'] or 0
        profs = list(profs_dict.values())

        total_heures  = sum(r['heures']  or 0 for r in rows)
        total_montant = sum(r['montant'] or 0 for r in rows)

        dept_nom = ''
        if dept_id:
            try:
                dept_nom = Departement.objects.get(pk=dept_id).nom
            except Departement.DoesNotExist:
                pass

        context = {
            'profs':           profs,
            'annee_universitaire': annee,
            'departement_nom': dept_nom,
            'total_heures':    total_heures,
            'total_montant':   total_montant,
        }
        filename = f"avancement_profs_{annee}.pdf".replace(' ', '_').replace('/', '-')
        return _render_pdf('avancement_profs_pdf.html', context, filename, orientation='Landscape')


class AvancementProfDetailPDFView(APIView):
    permission_classes = [RBACPermission]
    required_module    = 'avancement'

    def get(self, request):
        from apps.parametres.models import Semestre as SemestreModel
        annee       = request.query_params.get('annee_universitaire')
        prof_id     = request.query_params.get('prof')
        semestre_id = request.query_params.get('semestre_id')
        if not annee or not prof_id:
            return Response({'error': 'annee_universitaire et prof requis.'}, status=400)

        items, totaux_globaux = _compute_avancement_prof(annee, prof_id, semestre_id)
        if not items:
            return Response({'error': 'Aucune donnée.'}, status=404)

        try:
            prof_nom = Prof.objects.get(pk=prof_id).nom
        except Prof.DoesNotExist:
            prof_nom = f'Professeur #{prof_id}'

        semestre_label = 'Tous les semestres'
        if semestre_id:
            try:
                semestre_label = SemestreModel.objects.get(pk=semestre_id).semestre
            except SemestreModel.DoesNotExist:
                pass

        context = {
            'items':          items,
            'prof_nom':       prof_nom,
            'semestre_label': semestre_label,
            'totaux_globaux': totaux_globaux,
        }
        filename = f"avancement_prof_{prof_nom}_{annee}.pdf".replace(' ', '_').replace('/', '-')
        return _render_pdf('avancement_prof_detail_pdf.html', context, filename, orientation='Landscape')


class ChargePermanentsPDFView(APIView):
    permission_classes = [RBACPermission]
    required_module    = 'avancement'

    def get(self, request):
        annee = request.query_params.get('annee_universitaire')
        if not annee:
            return Response({'error': 'annee_universitaire requis.'}, status=400)

        data, totaux_globaux = _compute_charge_permanents(annee)
        if not data:
            return Response({'error': 'Aucun professeur permanent/contractuel/militaire.'}, status=404)

        # Collect all institution acronyms for dynamic columns
        all_acronyms = sorted({
            acro
            for item in data
            for acro in item['totaux']['charges_institution'].keys()
        })
        # Add ordered charges_list per item for template iteration
        for item in data:
            item['charges_list'] = [
                item['totaux']['charges_institution'].get(acro, 0)
                for acro in all_acronyms
            ]

        # Sommes verticales par institution (pour la ligne TOTAL du tfoot)
        totaux_par_institution = [
            sum(item['totaux']['charges_institution'].get(acro, 0) for item in data)
            for acro in all_acronyms
        ]

        context = {
            'data':                   data,
            'totaux_globaux':         totaux_globaux,
            'annee_universitaire':    annee,
            'all_acronyms':           all_acronyms,
            'totaux_par_institution': totaux_par_institution,
            'nb_profs':               len(data),
        }
        filename = f"charge_permanents_{annee}.pdf".replace(' ', '_').replace('/', '-')
        return _render_pdf('avancement_permanents_pdf.html', context, filename, orientation='Landscape')


class ChargePermanentsMensuelPDFView(APIView):
    """PDF des heures supp cumulees jusqu'a la fin d'un mois. Filtre institution
    principale uniquement. Cf _compute_charge_permanents_mensuel."""
    permission_classes = [RBACPermission]
    required_module    = 'avancement'

    def get(self, request):
        annee = request.query_params.get('annee_universitaire')
        year  = request.query_params.get('year')
        month = request.query_params.get('month')
        if not all([annee, year, month]):
            return Response(
                {'error': 'annee_universitaire, year et month requis.'},
                status=400
            )
        try:
            year_i  = int(year)
            month_i = int(month)
            if not (1 <= month_i <= 12):
                raise ValueError
        except (TypeError, ValueError):
            return Response({'error': 'year/month invalides.'}, status=400)

        data, totaux_globaux, inst_acro = _compute_charge_permanents_mensuel(
            annee, year_i, month_i
        )
        if not data:
            return Response(
                {'error': "Pas d'heures supplémentaires pour ce mois."},
                status=404,
            )

        MOIS_FR = {
            1: 'Janvier', 2: 'Février', 3: 'Mars',     4: 'Avril',
            5: 'Mai',     6: 'Juin',    7: 'Juillet',  8: 'Août',
            9: 'Septembre', 10: 'Octobre', 11: 'Novembre', 12: 'Décembre',
        }
        context = {
            'data':                   data,
            'totaux_globaux':         totaux_globaux,
            'annee_universitaire':    annee,
            'institution_principale': inst_acro or '',
            'mois_label':             f"{MOIS_FR.get(month_i, str(month_i))} {year_i}",
            'year':                   year_i,
            'month':                  month_i,
            'nb_profs':               len(data),
        }
        filename = f"Heures_supp_{MOIS_FR.get(month_i, str(month_i))}_{year_i}.pdf".replace(' ', '_').replace('/', '-')
        return _render_pdf('avancement_permanents_mensuel_pdf.html', context, filename, orientation='Landscape')


class ChargePermanentsMensuelExcelView(APIView):
    """Export Excel des heures supp d'un mois donne. Meme calcul que la version
    PDF, meme filtre (institution principale uniquement)."""
    permission_classes = [RBACPermission]
    required_module    = 'avancement'

    def get(self, request):
        annee = request.query_params.get('annee_universitaire')
        year  = request.query_params.get('year')
        month = request.query_params.get('month')
        if not all([annee, year, month]):
            return Response(
                {'error': 'annee_universitaire, year et month requis.'},
                status=400
            )
        try:
            year_i  = int(year)
            month_i = int(month)
            if not (1 <= month_i <= 12):
                raise ValueError
        except (TypeError, ValueError):
            return Response({'error': 'year/month invalides.'}, status=400)

        data, totaux_globaux, inst_acro = _compute_charge_permanents_mensuel(
            annee, year_i, month_i
        )
        if not data:
            return Response(
                {'error': "Pas d'heures supplémentaires pour ce mois."},
                status=404,
            )

        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        import io

        MOIS_FR = {
            1: 'Janvier', 2: 'Février', 3: 'Mars',     4: 'Avril',
            5: 'Mai',     6: 'Juin',    7: 'Juillet',  8: 'Août',
            9: 'Septembre', 10: 'Octobre', 11: 'Novembre', 12: 'Décembre',
        }
        mois_label = f"{MOIS_FR.get(month_i, str(month_i))} {year_i}"

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = mois_label[:31]  # Excel limite a 31 chars

        header_font = Font(bold=True, color='FFFFFF', size=11)
        header_fill = PatternFill('solid', fgColor='006633')
        total_fill  = PatternFill('solid', fgColor='FFD966')
        total_font  = Font(bold=True, size=12)
        center      = Alignment(horizontal='center', vertical='center')
        left        = Alignment(horizontal='left',   vertical='center')
        thin        = Side(style='thin')
        border      = Border(left=thin, right=thin, top=thin, bottom=thin)

        # Titre
        ws.merge_cells('A1:F1')
        title_cell = ws['A1']
        title_cell.value     = f"État des heures supplémentaires — {mois_label}"
        title_cell.font      = Font(bold=True, size=13)
        title_cell.alignment = center
        ws.row_dimensions[1].height = 22

        # En-tete colonnes
        headers = [
            'Enseignant',
            'Heures supp (éq. CM)',
            'Somme due (MRU)',
            'Compte bancaire',
            'Banque',
            'Émargement',
        ]
        for ci, h in enumerate(headers, 1):
            cell           = ws.cell(row=2, column=ci, value=h)
            cell.font      = header_font
            cell.fill      = header_fill
            cell.alignment = center
            cell.border    = border
        ws.row_dimensions[2].height = 18

        # Donnees
        for ri, item in enumerate(data, 3):
            row_data = [
                item['prof_nom'],
                item['heures_supp_mois'],
                item['montant_a_payer'],
                item['numero_de_compte'] or '',
                item['banque_nom'] or '',
                '',
            ]
            for ci, val in enumerate(row_data, 1):
                cell           = ws.cell(row=ri, column=ci, value=val)
                cell.alignment = left if ci == 1 else center
                cell.border    = border
                if ci in (2, 3):
                    cell.number_format = '#,##0.00'

        # Total
        total_row = len(data) + 3
        total_cells = [
            (1, 'TOTAL'),
            (2, totaux_globaux.get('total_heures_supp', 0)),
            (3, totaux_globaux.get('montant_global', 0)),
            (4, ''), (5, ''), (6, ''),
        ]
        for ci, val in total_cells:
            cell           = ws.cell(row=total_row, column=ci, value=val)
            cell.font      = total_font
            cell.fill      = total_fill
            cell.alignment = center
            cell.border    = border
            if ci in (2, 3):
                cell.number_format = '#,##0.00'
        ws.row_dimensions[total_row].height = 20

        # Largeurs colonnes
        ws.column_dimensions['A'].width = 35
        ws.column_dimensions['B'].width = 20
        ws.column_dimensions['C'].width = 20
        ws.column_dimensions['D'].width = 24
        ws.column_dimensions['E'].width = 18
        ws.column_dimensions['F'].width = 18

        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)

        filename = f"Heures_supp_{MOIS_FR.get(month_i, str(month_i))}_{year_i}.xlsx".replace(' ', '_').replace('/', '-')
        response = HttpResponse(
            buffer.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


class SuiviProfPDFView(APIView):
    permission_classes = [RBACPermission]
    required_module    = 'avancement'

    def get(self, request):
        annee   = request.query_params.get('annee_universitaire')
        prof_id = request.query_params.get('prof')
        if not annee or not prof_id:
            return Response({'error': 'annee_universitaire et prof requis.'}, status=400)

        # ── Source 1 : SuiviePointage 'Fait' (planning realise)
        pointages = SuiviePointage.objects.filter(
            annee_universitaire=annee,
            prof_id=prof_id,
            commentaire='Fait',
        ).select_related('em', 'type_seance_fk').prefetch_related(
            'departements__filiere', 'departements__niveau'
        )

        # ── Source 2 : Vacation (saisies manuelles, hors planning)
        vacations = Vacation.objects.filter(
            annee_univ=annee,
            prof_id=prof_id,
        ).select_related('em', 'type').prefetch_related(
            'departements__filiere', 'departements__niveau'
        )

        # Map (date -> numero_semaine, type_semestre) pour les vacations
        from apps.parametres.models import Semaine
        sem_map = {}
        for s in Semaine.objects.filter(annee_universitaire=annee):
            sem_map[s.date] = (s.numero_semaine, s.type_semestre)

        # Une fois pour tout le lot, cf. plus haut.
        filiere_utile = filiere_distingue()

        rows = []
        for p in pointages:
            rows.append({
                'numero_semaine':   p.numero_semaine,
                'date_suivie':      p.date_suivie,
                'em__intitule':     p.em.intitule if p.em_id and p.em else '',
                'type_seance':      p.type_seance_fk.type_seance if p.type_seance_fk_id and p.type_seance_fk else '',
                'departement__nom': format_depts_compact(p, filiere_utile),
                'duree_creneau':    p.duree_creneau or 0,
                'taux_paiement':    p.taux_paiement or 0,
                'type_semestre':    p.type_semestre or '',
            })
        for v in vacations:
            type_lib = v.type.type_seance if v.type_id and v.type else ''
            num_sem, ts = sem_map.get(v.date, (0, ''))
            rows.append({
                'numero_semaine':   num_sem,
                'date_suivie':      v.date,
                'em__intitule':     v.em.intitule if v.em_id and v.em else type_lib,
                'type_seance':      type_lib,
                'departement__nom': format_depts_compact(v, filiere_utile),
                'duree_creneau':    float(v.duree or 0),
                'taux_paiement':    float(v.taux_paiement or 0),
                'type_semestre':    ts,
            })
        rows.sort(key=lambda r: (r['type_semestre'], r['numero_semaine'], r['date_suivie'] or ''))

        if not rows:
            return Response({'error': 'Aucune donnée pour ce professeur.'}, status=404)

        # Ajouter montant + date formatée sur chaque ligne
        for r in rows:
            r['montant']         = (r['duree_creneau'] or 0) * (r['taux_paiement'] or 0)
            r['date_suivie_fmt'] = r['date_suivie'].strftime('%d/%m/%Y') if r['date_suivie'] else '—'

        # Grouper par type_semestre puis par numero_semaine. Une semaine 1
        # "Impairs" et une semaine 1 "Pairs" sont des semaines differentes
        # (dates reelles distinctes) — il faut les rendre dans des sections
        # separees pour eviter toute confusion.
        SECTIONS_DEF = [('I', 'Semestres Impairs'), ('P', 'Semestres Pairs')]
        sections = []
        for ts_code, ts_label in SECTIONS_DEF:
            type_rows = [r for r in rows if r.get('type_semestre') == ts_code]
            if not type_rows:
                continue
            buckets = defaultdict(list)
            for r in type_rows:
                buckets[r['numero_semaine']].append(r)
            sec_semaines = []
            for num, sem_rows in sorted(buckets.items()):
                sec_semaines.append({
                    'numero':        num,
                    'rows':          sem_rows,
                    'total_heures':  sum(r['duree_creneau'] or 0 for r in sem_rows),
                    'total_montant': sum(r['montant'] for r in sem_rows),
                })
            sections.append({
                'label':         ts_label,
                'code':          ts_code,
                'semaines':      sec_semaines,
                'nb_semaines':   len(sec_semaines),
                'total_heures':  sum(r['duree_creneau'] or 0 for r in type_rows),
                'total_montant': sum(r['montant'] for r in type_rows),
            })

        total_heures  = sum(r['duree_creneau'] or 0 for r in rows)
        total_montant = sum(r['montant'] for r in rows)
        nb_semaines   = sum(s['nb_semaines'] for s in sections)

        # ── Tableau recapitulatif ─────────────────────────────────────────────
        # Total Faites (deja calcule via rows)
        nb_faites     = len(rows)
        heures_faites = total_heures
        # Decomposition par type_seance (CM / TD / TP / PR)
        type_recap = {'CM': [0, 0.0], 'TD': [0, 0.0], 'TP': [0, 0.0], 'PR': [0, 0.0]}
        for r in rows:
            t = (r.get('type_seance') or '').upper()
            if t in type_recap:
                type_recap[t][0] += 1
                type_recap[t][1] += (r['duree_creneau'] or 0)
        # Non Faites : autre query
        non_faites_qs = SuiviePointage.objects.filter(
            annee_universitaire=annee, prof_id=prof_id,
        ).exclude(commentaire='Fait')
        nb_non_faites     = non_faites_qs.count()
        heures_non_faites = sum(p.duree_creneau or 0 for p in non_faites_qs)
        recap_lignes = [
            {'libelle': 'Séances Non Faites',  'nb': nb_non_faites, 'heures': heures_non_faites, 'is_sub': False},
            {'libelle': 'Total Séances Faites', 'nb': nb_faites,    'heures': heures_faites,    'is_sub': False},
            {'libelle': '- CM', 'nb': type_recap['CM'][0], 'heures': type_recap['CM'][1], 'is_sub': True},
            {'libelle': '- TD', 'nb': type_recap['TD'][0], 'heures': type_recap['TD'][1], 'is_sub': True},
            {'libelle': '- TP', 'nb': type_recap['TP'][0], 'heures': type_recap['TP'][1], 'is_sub': True},
            {'libelle': '- PR', 'nb': type_recap['PR'][0], 'heures': type_recap['PR'][1], 'is_sub': True},
        ]

        try:
            prof_obj  = Prof.objects.get(pk=prof_id)
            prof_nom  = prof_obj.nom
            prof_type = (prof_obj.type or '').lower()
        except Prof.DoesNotExist:
            prof_nom  = f'Professeur #{prof_id}'
            prof_type = ''

        # Permanents et contractuels ont une charge reglementaire incluse dans
        # leur salaire — on ne montre les montants que pour les vacataires.
        is_vacataire = prof_type == 'vacataire'

        context = {
            'sections':            sections,
            'prof_nom':            prof_nom,
            'prof_type':           prof_type,
            'is_vacataire':        is_vacataire,
            'annee_universitaire': annee,
            'nb_semaines':         nb_semaines,
            'total_heures':        total_heures,
            'total_montant':       total_montant,
            'recap_lignes':        recap_lignes,
        }
        filename = f"details_{prof_nom}_{annee}.pdf".replace(' ', '_').replace('/', '-')
        return _render_pdf('avancement_details_pdf.html', context, filename, orientation='Portrait')


