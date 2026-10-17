"""
Le MODE SALLE — l'appel séance par séance — et les séances de la grille.

Porté de l'ISS le 17/10/2026 (`backend_iss` 1f11cbc), adapté à l'IPGEI. Les
écrans d'appel lisaient `/api/v1/suivi/suivies/…`, gardé par `suivi_saisie`
et borné aux groupes délégués du compte : la scolarité de l'IPGEI, qui fait
l'appel (`abs_saisie` / `modifier`) sans saisir le suivi, ne voyait AUCUNE
séance. Ils lisaient aussi chaque séance, puis sa liste, puis ses présences —
trois allers-retours, et aucun moyen de savoir, sans ouvrir une séance, si son
appel avait été fait.

L'appel porte sur le pointage hebdomadaire — `suivi.Suivie`, la clé de
`Presence.suivi`. Ce module LIT `Suivie` et ne l'écrit jamais : la génération
du suivi reste à `apps/suivi/`. L'appel s'enregistre par `presences/bulk/`.

Règles, reprises des écrans qu'il remplace et adaptées à l'IPGEI :

  * une séance à appeler a un TYPE. Pas d'enseignant exigé, contrairement à
    l'ISS : une épreuve dont le surveillant n'est pas encore désigné se pointe
    quand même (règle de la grille de l'IPGEI) ;
  * choisir une CLASSE donne aussi les séances de ses sous-groupes — ses TP
    (`apps/ipgei/liste_appel.unites`) ;
  * le pointage peut porter des DOUBLONS (même groupe, jour, créneau, type,
    enseignant et matière) : une seule séance est montrée — celle qui a déjà
    des présences, sinon la plus ancienne. La matière entre dans la clé à
    l'IPGEI : deux matières au même créneau ne se confondent pas ;
  * le cloisonnement : institution principale et année. Pas de délégation par
    groupe (`managed_departements`) : elle bornait la saisie du SUIVI, et
    l'enregistrement des présences (`bulk`) ne l'a jamais exigée.
"""
import datetime as dt

from django.db.models import Count, Max, Min

JOURS_ORDRE = {'Lundi': 1, 'Mardi': 2, 'Mercredi': 3, 'Jeudi': 4, 'Vendredi': 5,
               'Samedi': 6, 'Dimanche': 7}

SELECT_SEANCE = ('jour_fk', 'creneau_fk', 'type_seance_fk', 'prof', 'em', 'salle',
                 'departement', 'departement__niveau')


def _suivies():
    """Le pointage de l'institution principale."""
    from apps.parametres.models import Institution
    from apps.suivi.models import Suivie

    principale = Institution.objects.filter(est_principale=True).first()
    if principale is None:
        return Suivie.objects.none()
    return Suivie.objects.filter(institution=principale)


def _a_appeler(qs):
    """Les séances qui appellent un appel : celles qui ont un type."""
    return qs.filter(type_seance_fk__isnull=False)


# ── Le bilan d'appel ─────────────────────────────────────────────────────────

def bilan_appel(par_statut):
    """Présents, absents, sanctionnés, justifiés d'une séance.

    `saisi` est faux tant que personne n'a fait l'appel : « 0 absent » ne veut
    pas dire « tous présents » quand rien n'a été saisi.
    """
    par_statut = par_statut or {}
    return {
        'saisi':      bool(par_statut),
        'presents':   par_statut.get(0, 0),
        'absents':    par_statut.get(1, 0),
        'sanctions':  par_statut.get(2, 0),
        'justifiees': par_statut.get(3, 0),
    }


def comptes_par_seance(suivi_ids):
    """{suivi_id: {statut: nombre}} — une requête pour toutes les séances."""
    from apps.absence.models import Presence

    comptes: dict = {}
    for r in (Presence.objects.filter(suivi_id__in=list(suivi_ids))
              .values('suivi_id', 'statut').annotate(n=Count('id'))):
        comptes.setdefault(r['suivi_id'], {})[r['statut']] = r['n']
    return comptes


# ── La forme d'une séance à l'écran ─────────────────────────────────────────

def ligne_seance(s, sous_groupes=frozenset()):
    """Les libellés d'une séance, tels que les écrans les affichent.

    `sous_groupe` : la séance se tient sur un sous-groupe de TP — l'écran le
    dit sur la colonne, puisqu'elle n'appelle pas toute la classe.
    """
    dep = s.departement if s.departement_id else None
    return {
        'id':                s.pk,
        'jour_label':        s.jour_fk.jour if s.jour_fk_id else None,
        'date_suivie':       s.date_suivie.isoformat() if s.date_suivie else None,
        'creneau_label':     s.creneau_fk.creneau if s.creneau_fk_id else None,
        'type_seance_label': s.type_seance_fk.type_seance if s.type_seance_fk_id else None,
        'prof_nom':          s.prof.nom if s.prof_id else None,
        'em':                s.em_id,
        'em_code':           s.em.code_em if s.em_id else None,
        'em_intitule':       s.em.intitule if s.em_id else None,
        'salle_nom':         s.salle.nom if s.salle_id else None,
        'departement':       s.departement_id,
        'dept_nom':          dep.nom if dep else None,
        'groupe':            dep.groupe if dep else None,
        'sous_groupe':       s.departement_id in sous_groupes,
        'numero_semaine':    s.numero_semaine,
        'type_semestre':     s.type_semestre,
        'annee_universitaire': s.annee_universitaire,
    }


def _ordre(s):
    jour = s.jour_fk.jour if s.jour_fk_id else ''
    creneau = s.creneau_fk if s.creneau_fk_id else None
    return (JOURS_ORDRE.get(jour, 9),
            creneau.ordre if creneau else 0,
            creneau.creneau if creneau else '',
            s.pk)


def _cle_doublon(s):
    """Ce qui fait UNE séance pour l'appel."""
    return (s.departement_id, s.jour_fk_id, s.creneau_fk_id, s.type_seance_fk_id,
            s.prof_id, s.em_id)


def _sous_groupes(dep_ids):
    from apps.ipgei.models import SousGroupeTP
    return frozenset(SousGroupeTP.objects.filter(pk__in=list(dep_ids))
                     .values_list('pk', flat=True))


# ── Les séances d'une semaine ────────────────────────────────────────────────

def seances_de_la_semaine(annee, type_semestre, numero_semaine, departement_id=None):
    """Les séances à appeler de la semaine, dédoublonnées, par jour puis
    créneau, chacune avec son bilan d'appel. `departement_id` : une classe
    (avec ses sous-groupes) ou un sous-groupe."""
    from apps.ipgei.liste_appel import unites

    qs = _a_appeler(_suivies().filter(annee_universitaire=annee,
                                      numero_semaine=numero_semaine))
    if type_semestre:
        qs = qs.filter(type_semestre=type_semestre)
    if departement_id:
        qs = qs.filter(departement_id__in=unites(departement_id))
    seances = list(qs.select_related(*SELECT_SEANCE))
    comptes = comptes_par_seance(s.pk for s in seances)
    sous_groupes = _sous_groupes({s.departement_id for s in seances})

    # Parmi des doublons, celle qui porte déjà l'appel, sinon la plus ancienne :
    # le même choix d'un chargement à l'autre, et un appel fait reste visible.
    retenues: dict = {}
    for s in sorted(seances, key=lambda x: x.pk):
        cle = _cle_doublon(s)
        deja = retenues.get(cle)
        if deja is None or (s.pk in comptes and deja.pk not in comptes):
            retenues[cle] = s

    return [{**ligne_seance(s, sous_groupes), 'appel': bilan_appel(comptes.get(s.pk))}
            for s in sorted(retenues.values(), key=_ordre)]


# ── Les semaines générées et leurs dates ─────────────────────────────────────

def semaines_generees(annee, type_semestre, aujourdhui=None):
    """Les semaines qui ont un pointage, leurs dates et celle à proposer.

    `/suivi/suivies/semaines-generees/` existe, mais il est gardé par
    `suivi_saisie`, borné aux groupes délégués du compte et ne dit pas les
    dates : « Semaine 3 » seul ne dit pas quand.

    La semaine proposée est celle d'aujourd'hui, sinon la dernière déjà
    commencée, sinon la première.
    """
    from apps.parametres.models import Semaine

    aujourdhui = aujourdhui or dt.date.today()
    qs = _suivies().filter(annee_universitaire=annee)
    if type_semestre:
        qs = qs.filter(type_semestre=type_semestre)
    numeros = sorted(set(qs.values_list('numero_semaine', flat=True)))
    if not numeros:
        return {'semaines': [], 'courante': None, 'dates': {}}

    # Les dates viennent du calendrier (`parametres.Semaine`, une ligne par
    # jour) ; à défaut, des séances elles-mêmes.
    cal = Semaine.objects.filter(annee_universitaire=annee, numero_semaine__in=numeros)
    if type_semestre:
        cal = cal.filter(type_semestre=type_semestre)
    bornes = {r['numero_semaine']: (r['debut'], r['fin'])
              for r in cal.values('numero_semaine')
                          .annotate(debut=Min('date'), fin=Max('date'))
              if r['debut']}
    manquantes = [n for n in numeros if n not in bornes]
    if manquantes:
        for r in (qs.filter(numero_semaine__in=manquantes, date_suivie__isnull=False)
                  .values('numero_semaine')
                  .annotate(debut=Min('date_suivie'), fin=Max('date_suivie'))):
            bornes[r['numero_semaine']] = (r['debut'], r['fin'])

    en_cours = [n for n in numeros if n in bornes and bornes[n][0] <= aujourdhui <= bornes[n][1]]
    commencees = [n for n in numeros if n in bornes and bornes[n][0] <= aujourdhui]
    courante = (en_cours or commencees[-1:] or numeros)[0]
    dates = {str(n): {'debut': bornes[n][0].isoformat(), 'fin': bornes[n][1].isoformat()}
             for n in numeros if n in bornes}
    return {'semaines': numeros, 'courante': courante, 'dates': dates}


# ── Tout ce qu'il faut pour faire l'appel d'une séance ───────────────────────

MENTION_HORS_LISTE = 'hors liste'


def saisie_seance(suivi_id):
    """La séance, sa liste d'appel, les présences déjà saisies et, s'il y en a
    un, le motif qui empêche l'appel. None si la séance n'existe pas ou sort du
    périmètre — l'appelant répond 404, sans dire laquelle des deux.

    La liste est celle de `apps/ipgei/liste_appel.py` : la classe pour un
    cours, le sous-groupe pour un TP. Qui porte déjà un pointage sur la séance
    sans être sur la liste (abandon déclaré après coup) reste à l'écran,
    « hors liste » : on ne cache pas une présence enregistrée.
    """
    from apps.absence.models import Presence
    from apps.ipgei.liste_appel import (SOURCE_GROUPE, liste_appel, ordre_matricule,
                                        resume)

    s = _suivies().select_related(*SELECT_SEANCE).filter(pk=suivi_id).first()
    if s is None:
        return None

    pointees = list(Presence.objects.filter(suivi=s).select_related('etudiant'))
    presences = {str(p.etudiant_id): {'statut': p.statut, 'commentaire': p.commentaire,
                                      'justificatif': bool(p.justificatif)}
                 for p in pointees}

    refus = ''
    lignes, source = [], ''
    if s.departement_id:
        r = liste_appel(s.departement_id, s.annee_universitaire)
        source = r['source']
        lignes = [resume(e) for e in r['etudiants']]
        vus = {l['id'] for l in lignes}
        hors = [{**resume(p.etudiant), 'mention': MENTION_HORS_LISTE}
                for p in pointees if p.etudiant_id not in vus]
        lignes = sorted(lignes + hors, key=lambda l: ordre_matricule(l['matricule']))
    else:
        refus = "Cette séance n'est rattachée à aucun groupe : elle n'a pas de liste d'appel."

    par_statut: dict = {}
    for p in presences.values():
        par_statut[p['statut']] = par_statut.get(p['statut'], 0) + 1

    # La classe de la séance — elle-même, ou celle du sous-groupe d'un TP :
    # « Séance suivante » enchaîne sur la journée de la CLASSE, comme la liste
    # du mode salle d'où l'on vient.
    from apps.ipgei.models import SousGroupeTP
    classe = (SousGroupeTP.objects.filter(pk=s.departement_id)
              .values_list('classe_id', flat=True).first() or s.departement_id)

    return {
        'seance':             {**ligne_seance(s, _sous_groupes([s.departement_id])),
                               'classe': classe,
                               'appel': bilan_appel(par_statut)},
        'etudiants':          lignes,
        'source':             source,
        'liste_non_verifiee': source == SOURCE_GROUPE,
        'presences':          presences,
        'refus':              refus,
    }
