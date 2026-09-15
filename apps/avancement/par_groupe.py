"""
Avancement par GROUPE — la question que `_compute_avancement_em` ne pose pas.

L'écran des matières retient, pour chaque matière, la progression du groupe LE
PLUS AVANCÉ : c'est juste, et c'est voulu — un TP dédoublé sur G1 et G2 fait
avancer le programme d'une séance, pas de deux, et la somme afficherait 200 %.

Mais ce maximum rend le retard invisible : MPSI A a fait ses 10 h de TP, MPSI B
n'en a fait que 4, et la matière affiche 100 %. Ce module reprend EXACTEMENT la
même accumulation, et ne fait simplement pas le dernier geste — il rend le
détail par unité au lieu de l'écraser.

Il ne lit rien d'autre que `SuiviePointage`, comme l'écran existant : une ligne
par séance programmée, `commentaire` à « Fait » ou « Non fait ». Une séance
manquée est donc déjà en base ; personne ne la regardait, voilà tout.

Aucune écriture, aucun modèle nouveau.
"""
from collections import defaultdict
from datetime import date

from django.db.models import Min, Q

from apps.departement.models import Departement
from apps.em.volumes import volumes_par_em
from apps.em.models import EM as EMModel
from apps.suivi.models import SuiviePointage

from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import RBACPermission

from .views import _ts_label

#: Familles d'enseignement : un TP2 est du TP, et un DS se rattache au TD —
#: mêmes regroupements que l'écran des matières, sans quoi les deux chiffres
#: divergeraient sans que rien ne le signale.
FAMILLES = {'CM': 'CM', 'CM2': 'CM', 'TD': 'TD', 'TD2': 'TD',
            'TP': 'TP', 'TP2': 'TP', 'PR': 'PR', 'PR2': 'PR'}

CATEGORIES = ('CM', 'TD', 'TP', 'PR')


def _heures_par_unite(pointages):
    """
    Heures pointées, par famille PUIS par unité d'enseignement.

    Même déduplication que l'écran des matières : la clé
    (unité, semaine, jour, créneau, type) empêche de compter deux fois un
    créneau assuré par deux enseignants ou dans deux salles.

    Une séance peut porter plusieurs unités — un cours commun à deux classes :
    elle fait alors avancer chacune, ce qui est le comportement voulu.
    """
    heures = defaultdict(lambda: defaultdict(dict))
    for seance in pointages:
        label = _ts_label(seance)
        # Le DS rejoint EF et ER : une épreuve n'avance pas le programme, et ne
        # doit rien déduire de la charge de la matière. Il comptait dans les
        # heures de TD — il s'en tient un sur un créneau de TD —, si bien que
        # deux devoirs suffisaient à annoncer un semestre bouclé.
        #
        # Cette vue et « Avancement par matière » doivent trancher pareil : la
        # seconde retient le MAXIMUM des unités calculées ici. Un désaccord
        # ferait afficher deux volumes de TD différents pour la même matière.
        famille = FAMILLES.get(label)
        if famille is None:
            continue                    # DS, EF, ER : des épreuves, pas du programme
        # La clé ne porte PAS l'unité : c'est ce qui permet ensuite de réunir un
        # sous-groupe et sa classe sans compter deux fois une séance inscrite
        # aux deux. Dans une même unité, elle joue le rôle de l'ancienne
        # déduplication — deux profs sur le même créneau ne font qu'une séance.
        cle = (seance.numero_semaine, seance.jour_fk_id,
               seance.creneau_fk_id, label)
        for unite in [d.id for d in seance.departements.all()] or [0]:
            heures[famille][unite].setdefault(cle, seance.duree_creneau or 0)
    return heures


def _cumul(par_unite, unite, parent=None):
    """
    Heures d'un groupe, sa classe comprise s'il en est un sous-groupe.

    CM et TD sont dispensés à la CLASSE entière ; seul le TP est dédoublé en
    sous-groupes. Un sous-groupe qui ne compterait que ses propres séances
    afficherait donc 0 % de cours magistral, ce qui est faux : il les a suivis
    avec sa classe.

    L'union se fait par clé de séance, jamais par addition : une séance inscrite
    à la fois à la classe et au sous-groupe ne compte ainsi qu'une fois.
    """
    seances = dict(par_unite.get(parent, {})) if parent else {}
    seances.update(par_unite.get(unite, {}))
    return sum(seances.values())


def _est_ratee(seance, aujourdhui):
    """
    Une séance À VENIR n'est pas une séance manquée.

    Toutes les semaines futures sont à « Non fait » par défaut : les compter
    afficherait un retard massif dès la première semaine du semestre. Et sans
    date, on ne peut rien affirmer — on s'abstient plutôt que d'accuser un
    groupe à tort.
    """
    return (seance.commentaire != 'Fait'
            and seance.date_suivie is not None
            and seance.date_suivie < aujourdhui)


def _matieres(annee, type_semestre, semestre_id=None, niveau=None, ems=None):
    """
    Les matières de la période, dédoublonnées par code — comme l'écran des matières.

    `niveau` les restreint à l'année d'étude demandée. Sans ce filtre, une
    période impaire rendait S1 (MPSI) ET S3 (MP) : un tableau MPSI y affichait
    des matières de deuxième année, que ses groupes ne suivent pas. Le semestre
    porte son niveau en base, c'est lui qui tranche.
    """
    # Meme garde-fou que l'ecran des matieres : une matiere qui a perdu son
    # ancre (classe supprimee, SET_NULL) reste visible des lors que son niveau a
    # une classe active cette annee.
    base = (EMModel.objects
            .filter(semestre__type_semestre=type_semestre)
            .filter(Q(departement__annee_universitaire=annee)
                    | Q(departement__classe_ipgei__isnull=False)
                    | Q(departement__isnull=True,
                        matiere_ipgei__isnull=False, matiere_ipgei__actif=True,
                        semestre__niveau_semestre__departements__annee_universitaire=annee,
                        semestre__niveau_semestre__departements__classe_ipgei__isnull=False,
                        semestre__niveau_semestre__departements__classe_ipgei__actif=True))
            .select_related('semestre'))
    if niveau:
        base = base.filter(semestre__niveau_semestre__niveau=niveau)
    if semestre_id:
        base = base.filter(semestre_id=semestre_id)
    # Perimetre disciplinaire : un chef ou un coordinateur ne voit que SES
    # matieres. `ems` vaut None pour les autres roles, qui gardent la vue entiere.
    if ems is not None:
        base = base.filter(id__in=ems)
    premiers = (base.values('code_em').annotate(premier=Min('id'))
                .values_list('premier', flat=True))
    return (EMModel.objects.filter(id__in=list(premiers))
            .select_related('semestre')
            .order_by('semestre__code_semestre', 'code_em'))


def groupes_du_niveau(annee, niveau=None):
    """
    Les groupes du referentiel : classes IPGEI et sous-groupes de TP.

    On part d'ICI plutot que des pointages. Un groupe qui n'a encore rien fait
    doit figurer avec sa planification et zero realise — c'est meme le cas le
    plus interessant en debut de semestre, et celui qu'une liste tiree des
    pointages laissait invisible : pas de seance pointee, pas de colonne, donc
    aucune trace du groupe en retard.

    Les classes conteneurs sont ecartees : elles n'ont ni cours ni emploi du
    temps, ce sont des salles d'attente de niveau.
    """
    qs = (Departement.objects
          .filter(annee_universitaire=annee, is_container=False)
          .filter(Q(classe_ipgei__isnull=False) | Q(sous_groupe_ipgei__isnull=False))
          .select_related('niveau', 'sous_groupe_ipgei__classe'))
    if niveau:
        qs = qs.filter(niveau__niveau=niveau)

    groupes = {}
    for d in qs.distinct():
        sous_groupe = getattr(d, 'sous_groupe_ipgei', None)
        parent = sous_groupe.classe_id if sous_groupe else None
        groupes[d.id] = {
            'nom':    d.nom,
            'niveau': d.niveau.niveau if d.niveau_id else '',
            # La classe de rattachement : elle-même pour une classe, la classe
            # mère pour un sous-groupe de TP. C'est par elle qu'on regroupe à
            # l'écran, et d'elle que le sous-groupe hérite ses CM et TD.
            'parent': parent,
            'classe': (sous_groupe.classe.nom if sous_groupe else d.nom),
        }
    return groupes


def avancement_par_groupe(annee, type_semestre, semestre_id=None, niveau=None, ems=None):
    """
    Une ligne par matière, et dans chacune le taux de CHAQUE groupe.

    Le volume de référence reste celui de la maquette : les heures programmées
    sont définies par matière, et chaque groupe doit les faire toutes. Le taux
    d'un groupe est donc ses heures rapportées à ce même volume — un demi-groupe
    de TP n'a pas un demi-programme.

    `niveau` — « MPSI », « MP » — restreint aux groupes de cette annee d'etude,
    sous-groupes de TP compris : une matiere de MPSI n'a rien a dire des groupes
    de MP, et melanger les deux donnerait un tableau ou la moitie des colonnes
    est vide sans raison.
    """
    aujourdhui = date.today()
    resultat = []
    # Le referentiel decide des colonnes ; les pointages n'y ajoutent que ce
    # qu'ils portent d'inattendu (un groupe d'une autre annee, par exemple).
    referentiel = groupes_du_niveau(annee, niveau)
    parents = {identifiant: infos['parent']
               for identifiant, infos in referentiel.items()}

    # Volume PREVU gele pour l'annee : cf. `em.volumes`. Une retouche de
    # maquette ne doit pas reecrire le pourcentage d'une annee revolue.
    volumes = volumes_par_em(annee)

    for matiere in _matieres(annee, type_semestre, semestre_id, niveau, ems):
        pointages = (SuiviePointage.objects
                     .filter(annee_universitaire=annee, em_id=matiere.id)
                     .select_related('type_seance_fk')
                     .prefetch_related('departements'))
        if semestre_id:
            pointages = pointages.filter(semestre_id=semestre_id)
        pointages = list(pointages)

        faites = _heures_par_unite([s for s in pointages if s.commentaire == 'Fait'])
        ratees = _heures_par_unite([s for s in pointages if _est_ratee(s, aujourdhui)])
        # Les seances ECOULEES, faites ou non. Sans ce compte, « zero heure due »
        # ne se distingue pas de « rien n'a jamais ete programme » : une base ou
        # le suivi n'a pas ete genere afficherait toutes les classes « a jour »,
        # ce qui est faux — on ne sait simplement rien.
        ecoulees = _heures_par_unite([
            s for s in pointages
            if s.date_suivie is not None and s.date_suivie < aujourdhui])

        # Les groupes du référentiel, PLUS ceux qu'un pointage mentionnerait sans
        # y figurer. Un groupe sans aucune séance pointée garde ainsi sa ligne,
        # avec sa planification et zéro réalisé.
        vues = {u for par_unite in faites.values() for u in par_unite}
        vues |= {u for par_unite in ratees.values() for u in par_unite}

        infos = dict(referentiel)
        manquantes = [u for u in vues if u and u not in infos]
        if manquantes:
            infos.update({
                d.id: {'nom': d.nom, 'parent': None, 'classe': d.nom,
                       'niveau': d.niveau.niveau if d.niveau_id else ''}
                for d in (Departement.objects.filter(id__in=manquantes)
                          .select_related('niveau'))})

        unites = set(referentiel) | {u for u in vues if u}
        if niveau:
            unites = {u for u in unites
                      if infos.get(u, {}).get('niveau') == niveau}

        vol  = volumes.get(matiere.id) or matiere
        plan = {'CM': vol.CM or 0, 'TD': vol.TD or 0,
                'TP': vol.TP or 0, 'PR': vol.PR or 0}

        # La REFERENCE : ce que le groupe le plus avance a fait. C'est elle qui
        # repond a la question de l'ecran — « quel groupe n'a pas encore fait ce
        # que les autres ont fait ? ». Ni la maquette (elle dit le semestre
        # entier, pas ou on en est), ni le calendrier (une seance peut etre
        # avancee ou reportee) : ce sont les autres groupes qui font foi.
        #
        # On compare les groupes qui RECOIVENT reellement l'enseignement — les
        # « feuilles » : les sous-groupes d'une classe qui en a, la classe
        # elle-meme sinon. Une classe divisee n'est pas une unite d'enseignement,
        # ce sont ses moities qui en sont.
        #
        # C'est ce qui permet de comparer MPSI B, qui n'a pas de sous-groupe, au
        # sous-groupe A1 de MPSI A : si A1 a eu le cours et pas B, B est en
        # retard, peu importe le niveau hierarchique ou la seance a ete pointee.
        # Et cela evite l'inverse — une classe declaree en retard sur son propre
        # sous-groupe, qui herite d'elle et ne peut la depasser autrement.
        avec_enfants = {p for p in parents.values() if p}
        feuilles = [u for u in unites if parents.get(u) or u not in avec_enfants]

        reference = {
            f: max((_cumul(faites[f], u, parents.get(u)) for u in feuilles),
                   default=0.0)
            for f in CATEGORIES
        }

        groupes = []
        for unite in sorted(unites, key=lambda u: infos.get(u, {}).get('nom', '')):
            fiche = infos.get(unite, {})
            ligne = {'departement': unite or None,
                     'nom':    fiche.get('nom', 'Sans groupe'),
                     'niveau': fiche.get('niveau', ''),
                     'parent': fiche.get('parent'),
                     'classe': fiche.get('classe', fiche.get('nom', ''))}
            for famille in CATEGORIES:
                parent = parents.get(unite)
                fait = _cumul(faites[famille], unite, parent)
                ligne['real_' + famille] = fait
                ligne['rate_' + famille] = _cumul(ratees[famille], unite, parent)
                # Ce qui aurait du etre fait a ce jour : le denominateur du
                # jugement « a jour », et le signal d'une periode non renseignee.
                ligne['due_' + famille] = _cumul(ecoulees[famille], unite, parent)
                # L'ECART au groupe le plus avance — le chiffre qui fait agir :
                # « ce groupe a une seance de moins que les autres ».
                # Une classe divisee n'a pas d'ecart propre : son etat est celui
                # de ses sous-groupes, affiches juste au-dessous.
                ligne['ecart_' + famille] = (
                    round(max(0.0, reference[famille] - fait), 2)
                    if unite in feuilles else 0.0)
                ligne['pct_' + famille] = (round(fait / plan[famille] * 100)
                                           if plan[famille] > 0 else 0)
            groupes.append(ligne)

        # Le chiffre de l'écran des matières, repris tel quel : les deux vues se
        # lisent ensemble, et l'écart entre elles EST l'information cherchée.
        programme = reference

        entree = {
            'em':       matiere.id,
            'code_em':  matiere.code_em,
            'intitule': matiere.intitule,
            'semestre': matiere.semestre.semestre if matiere.semestre else '—',
            'groupes':  groupes,
        }
        for famille in CATEGORIES:
            entree['plan_' + famille] = plan[famille]
            # Les heures du groupe le plus avance, et non son seul pourcentage :
            # l'ecran compare des heures, pas des taux.
            entree['programme_' + famille] = programme[famille]
            entree['programme_pct_' + famille] = (
                round(programme[famille] / plan[famille] * 100)
                if plan[famille] > 0 else 0)
        resultat.append(entree)

    return resultat


def seances_ratees(annee, em_id, departement_id=None, semestre_id=None, ems=None):
    """
    Les séances qu'un groupe n'a pas eues : semaine, jour, créneau, enseignant.

    C'est ce qui permet de dire non pas « B est en retard », mais « B n'a pas eu
    le TP de la semaine 5, mardi 10 h, avec tel enseignant ».
    """
    aujourdhui = date.today()
    # Hors perimetre, on ne rend rien : connaitre l'identifiant d'une matiere
    # voisine ne doit pas suffire a en lire le detail.
    if ems is not None and int(em_id) not in ems:
        return []
    # Toutes les seances non faites, ecoulees ou non. Une seance a venir que les
    # AUTRES groupes ont deja eue est justement celle qu'il faut replanifier :
    # l'ecarter parce que sa date n'est pas passee priverait l'ecran de son
    # objet. Chaque ligne dit si elle est echue, et l'ecran s'en sert pour
    # ordonner l'urgence.
    qs = (SuiviePointage.objects
          .filter(annee_universitaire=annee, em_id=em_id)
          .exclude(commentaire='Fait')
          .select_related('type_seance_fk', 'jour_fk', 'creneau_fk', 'prof')
          .order_by('numero_semaine', 'date_suivie'))
    if semestre_id:
        qs = qs.filter(semestre_id=semestre_id)
    if departement_id:
        qs = qs.filter(departements__id=departement_id)

    return [{
        'numero_semaine': seance.numero_semaine,
        'date':           seance.date_suivie,
        'jour':           getattr(seance.jour_fk, 'jour', ''),
        'creneau':        getattr(seance.creneau_fk, 'creneau', ''),
        'type_seance':    _ts_label(seance),
        'prof':           getattr(seance.prof, 'nom', ''),
        'duree':          seance.duree_creneau or 0,
        'passee':         bool(seance.date_suivie and seance.date_suivie < aujourdhui),
    } for seance in qs.distinct()]


# ═════════════════════════════════════════════════════════════════════════════
# Endpoints — même module RBAC que l'écran des matières : c'est la même question
# posée autrement, elle n'ouvre aucune donnée nouvelle.
# ═════════════════════════════════════════════════════════════════════════════
class AvancementGroupeView(APIView):
    permission_classes = [RBACPermission]
    required_module    = 'avancement'

    def get(self, request):
        annee         = request.query_params.get('annee_universitaire')
        type_semestre = request.query_params.get('type_semestre')
        semestre_id   = request.query_params.get('semestre_id')
        niveau        = request.query_params.get('niveau')
        if not annee or not type_semestre:
            return Response(
                {'error': 'annee_universitaire et type_semestre requis.'}, status=400)
        from apps.ipgei.perimetre import ems_autorises
        return Response(avancement_par_groupe(
            annee, type_semestre, semestre_id, niveau, ems_autorises(request.user)))


class SeancesRateesView(APIView):
    permission_classes = [RBACPermission]
    required_module    = 'avancement'

    def get(self, request):
        annee  = request.query_params.get('annee_universitaire')
        em_id  = request.query_params.get('em')
        if not annee or not em_id:
            return Response({'error': 'annee_universitaire et em requis.'}, status=400)
        from apps.ipgei.perimetre import ems_autorises
        return Response(seances_ratees(
            annee, em_id,
            request.query_params.get('departement'),
            request.query_params.get('semestre_id'),
            ems_autorises(request.user),
        ))
