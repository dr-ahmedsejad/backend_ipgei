"""
La vue de surveillance : où doit être chaque classe, à chaque créneau du jour.

Les écrans d'emploi du temps répondent chacun à une question — où est cette
classe, que fait cet enseignant, cette salle est-elle prise. Celui qui veille
sur les étudiants en pose une autre : « à cette heure-ci, où doit être CHAQUE
groupe, et avec qui ? ». Il lui faut tous les groupes en même temps, sur une
journée.

    lignes    = les classes de l'année, rangées par niveau (MPSI, MP), chacune
                suivie de ses sous-groupes de TP
    colonnes  = les créneaux de la journée
    case      = la salle d'abord (c'est là qu'on va vérifier), la matière,
                l'enseignant — et ce qui trompe la surveillance : une épreuve,
                une épreuve sans surveillant désigné, une séance annulée (le
                groupe est libre), des classes réunies dans une salle ou en
                parallèle dans deux.

Reprise des vues de l'ISS et de l'ESP (10/2026), adaptée au modèle IPGEI :

  * LA SOURCE est l'emploi du temps vivant — `SeanceReelle`, ce que chaque
    écran EDT de l'IPGEI lit — plus les ÉPREUVES (`EpreuveClasse`), qui n'y
    entrent pas : elles durent 2 à 4 h et couvrent plusieurs créneaux. C'est
    aussi la seule source qui dise qu'une séance est ANNULÉE ; le suivi
    (`Suivie`), lui, n'existe qu'après génération de la semaine et ignore
    les annulations (`miroir.projeter_semaine_reelle`).
  * UN SOUS-GROUPE DE TP est un département à part entière. Sa ligne montre ses
    propres séances (TP dédoublé) et, ailleurs, ce qu'il suit avec sa classe —
    marqué `herite`. La ligne de la classe, elle, dit quand elle est éclatée en
    sous-groupes, et où chacun se trouve.
  * LES CRÉNEAUX sont les créneaux actifs PLUS ceux que la journée utilise,
    signalés inactifs : une vue limitée aux actifs cacherait les séances
    posées sur un créneau désactivé depuis.

La case ouvre la saisie des absences de SA ligne de suivi (`pointable`) — voir
`_perimetre_saisie` pour les règles, qui sont celles de l'écran de saisie.

Rien ici n'écrit.
"""
import datetime as dt
from collections import defaultdict

from django.db.models import Q

from core.types_seance import TYPES_EPREUVE

from .models import (
    Classe, EpreuveClasse, NiveauIPGEI, SeanceReelle, SemaineIPGEI, SousGroupeTP,
    _bornes_creneau,
)
from .socle import SEMESTRES

JOURS = ('Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi', 'Dimanche')

#: Ce que l'écran de saisie des absences (`/dashboard/absences/saisir/salle/
#: <suivi>`) lit en s'ouvrant, et le droit que chaque lecture exige :
#:   * la séance — `/suivi/suivies/<id>/`   → suivi_saisie : voir ;
#:   * la liste des étudiants — `/absences/etudiants/?departement=`
#:                                          → scolarite_etudiants : voir ;
#:   * le pointage déjà fait — `/absences/presences/?suivi=`
#:                                          → abs_saisie : voir ;
#:   * la validation — `/absences/presences/bulk/` → abs_saisie : modifier.
#: Il en manque un, l'écran s'ouvre sur une erreur ou valide dans le vide.
DROITS_SAISIE = (
    ('suivi_saisie', 'voir'),
    ('scolarite_etudiants', 'voir'),
    ('abs_saisie', 'voir'),
    ('abs_saisie', 'modifier'),
)

# Pourquoi une case ne s'ouvre pas, pour qui a les droits de saisie.
SANS_SUIVI = 'Suivi non généré'
SOUS_GROUPE = 'Appel par sous-groupe indisponible'
HORS_PERIMETRE = 'Groupe non délégué'


def _debut(libelle: str) -> int:
    """« 08h00-09h30 » → 480 (minutes), pour ranger les créneaux dans le temps."""
    bornes = _bornes_creneau(libelle or '')
    return bornes[0] if bornes else 24 * 60


def libelle_niveau(code: str, ref) -> str:
    """« MPSI — 1re année » ; un niveau sans référentiel reste tel quel."""
    if ref is None or not ref.rang:
        return code or 'Sans niveau'
    return f"{code} — {ref.rang}{'re' if ref.rang == 1 else 'e'} année"


# ── Droits ───────────────────────────────────────────────────────────────────
def _droit(user, module: str, action: str) -> bool:
    """La règle de `RBACPermission` : l'administrateur passe, les autres par la
    matrice (avec son cache, que `invalidate_role_rbac` périme)."""
    from core.permissions import _has_access
    if user.role == 'admin' or user.is_superuser:
        return True
    return _has_access(user, module, action)


def _perimetre_saisie(user):
    """Les départements dont `user` peut ouvrir la saisie des absences.

    `None` : tous (superutilisateur) ; un ensemble, éventuellement vide, sinon.
    Sans utilisateur (le PDF), personne n'ouvre rien.

    Les règles sont celles de l'écran de saisie, sans les contourner :
      * les quatre droits de `DROITS_SAISIE` ;
      * la séance est relue par `SuivieViewSet`, qui ne montre que
        l'institution principale (`InstitutionScopedMixin`) — vérifié par
        case — et à chacun ses groupes délégués, `managed_departements`
        (`DepartementScopedMixin`). Seul le superutilisateur y échappe : le
        rôle administrateur, lui, y est soumis comme tout le monde.
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return set(), False
    if not all(_droit(user, m, a) for m, a in DROITS_SAISIE):
        return set(), False
    if user.is_superuser:
        return None, True
    return set(user.managed_departements.values_list('id', flat=True)), True


# ── Les lignes ───────────────────────────────────────────────────────────────
def _lignes(annee: str) -> list:
    """Les classes de l'année, par niveau, chacune suivie de ses sous-groupes.

    Une classe d'attente (« MPSI », sans suffixe) ne se planifie pas : elle
    n'a rien à surveiller. Un sous-groupe n'a sa ligne que s'il a au moins une
    séance dans l'année — sans quoi elle répéterait celle de sa classe.
    """
    niveaux = {n.code: n for n in NiveauIPGEI.objects.filter(actif=True)}
    seances_annee = SeanceReelle.objects.filter(semaine__semestre__annee_universitaire=annee)
    classes = list(
        Classe.objects
        .filter(Q(actif=True) | Q(pk__in=seances_annee.values('classe_id')),
                annee_universitaire=annee, is_container=False)
        .select_related('niveau'))
    sous_groupes = defaultdict(list)
    for sg in (SousGroupeTP.objects
               .filter(classe__in=[c.pk for c in classes],
                       pk__in=seances_annee.filter(sous_groupe__isnull=False)
                                           .values('sous_groupe_id'))
               .order_by('groupe', 'pk')):
        sous_groupes[sg.classe_id].append(sg)

    def rang(c):
        ref = niveaux.get(c.niveau_code)
        return (ref is None, ref.rang if ref else 0, ref.ordre if ref else 0,
                c.niveau_code, c.nom)

    lignes = []
    for c in sorted(classes, key=rang):
        niveau = c.niveau_code
        commun = {'niveau': niveau,
                  'niveau_libelle': libelle_niveau(niveau, niveaux.get(niveau)),
                  'classe': c.pk}
        lignes.append({'id': c.pk, 'nom': c.nom, 'libelle': c.nom,
                       'sous_groupe': False, **commun})
        for sg in sous_groupes.get(c.pk, []):
            lignes.append({'id': sg.pk, 'nom': sg.nom, 'libelle': sg.groupe,
                           'sous_groupe': True, **commun})
    return lignes


# ── La journée ───────────────────────────────────────────────────────────────
def _calendrier(annee: str, date):
    from apps.parametres.models import Semaine
    return (Semaine.objects.filter(annee_universitaire=annee, date=date)
            .select_related('jour_fk').order_by('pk').first())


def _index_suivis(annee: str, date) -> dict:
    """Les lignes de suivi du jour, par ce qui identifie une séance des deux
    côtés — unité (classe ou sous-groupe), créneau, matière (par son EM miroir)
    et type —, comme `miroir.refleter_absences`. Les lignes vides du
    remplissage (sans élément) n'identifient rien."""
    from apps.suivi.models import Suivie
    index = {}
    for s in (Suivie.objects
              .filter(annee_universitaire=annee, date_suivie=date, em__isnull=False)
              .values('pk', 'departement_id', 'creneau_fk_id', 'em_id',
                      'type_seance_fk__type_seance', 'institution_id',
                      'prof__nom', 'salle__nom')
              .order_by('pk')):
        cle = (s['departement_id'], s['creneau_fk_id'], s['em_id'],
               (s['type_seance_fk__type_seance'] or '').strip().upper())
        index.setdefault(cle, s)
    return index


def _case_vide():
    return {
        'seance': None, 'epreuve': None, 'suivi': None,
        'em_code': '', 'em_intitule': '', 'type': '', 'special': False,
        'evaluation': False, 'horaire': '', 'prof': '', 'salle': '',
        'annulee': False, 'herite': False, 'dedouble': False,
        'sous_groupes': [], 'reunis': [], 'paralleles': [], 'doublon': False,
        'pointable': False, 'motif': '',
    }


def journee(annee: str, date: dt.date, user=None) -> dict:
    """Toutes les classes et leurs sous-groupes, tous les créneaux, ce jour-là."""
    from apps.parametres.models import Creneau, Institution

    perimetre, droits_saisie = _perimetre_saisie(user)
    principale = None
    if droits_saisie:
        # Exactement la lecture d'`InstitutionScopedMixin`.
        p = Institution.objects.filter(est_principale=True).first()
        principale = p.pk if p else None
    a_venir = date > dt.date.today()

    jour = _calendrier(annee, date)
    lignes = _lignes(annee)
    noms = {l['id']: l['nom'] for l in lignes}
    classes = {l['id'] for l in lignes if not l['sous_groupe']}

    seances = list(
        SeanceReelle.objects
        .filter(date=date, semaine__semestre__annee_universitaire=annee,
                classe_id__in=classes)
        .select_related('matiere', 'prof', 'salle', 'type_seance', 'creneau',
                        'sous_groupe', 'semaine__semestre')
        .order_by('annulee', 'pk'))           # une séance tenue prime une annulée
    convocations = list(
        EpreuveClasse.objects
        .filter(epreuve__date=date, classe_id__in=classes)
        .select_related('epreuve__matiere', 'salle')
        .order_by('epreuve__heure_debut', 'epreuve_id', 'classe_id'))
    suivis = _index_suivis(annee, date) if (droits_saisie or convocations) else {}

    # Colonnes : les créneaux actifs, et ceux que la journée utilise.
    actifs = list(Creneau.objects.filter(is_actif=True).order_by('ordre', 'id'))
    creneaux = {c.pk: c for c in actifs}
    for s in seances:
        creneaux.setdefault(s.creneau_id, s.creneau)
    colonnes = sorted(creneaux.values(), key=lambda c: (_debut(c.creneau), c.ordre or 0, c.pk))

    def ouvrable(case, unite, suivi):
        """`pointable` et, pour qui a les droits, pourquoi pas."""
        if not droits_saisie or a_venir or case['annulee']:
            return
        if suivi is None:
            case['motif'] = SANS_SUIVI
            return
        case['suivi'] = suivi['pk']
        if unite not in classes:
            # L'écran de saisie liste les étudiants par `Etudiant.departement`,
            # qui ne porte que la CLASSE : sur la ligne d'un sous-groupe, il
            # s'ouvrirait sur une liste vide (relevé du 14/10/2026 : aucun
            # étudiant n'a un sous-groupe pour département).
            case['motif'] = SOUS_GROUPE
            return
        if (principale is None or suivi['institution_id'] != principale
                or (perimetre is not None and unite not in perimetre)):
            case['motif'] = HORS_PERIMETRE
            return
        case['pointable'] = True

    cases = {}

    # 1. Les épreuves, d'abord : la classe compose, sur TOUS les créneaux que
    #    l'épreuve recouvre. La ligne de suivi, elle, est ancrée sur le premier
    #    créneau ACTIF recouvert (`models.ancrage_epreuve`).
    par_epreuve = defaultdict(list)
    for conv in convocations:
        par_epreuve[conv.epreuve_id].append(conv)
    for conv in convocations:
        ep = conv.epreuve
        ancre = next((c for c in actifs if ep.recouvre(c)), None)
        em_id = ep.matiere.em_miroir_id if ep.matiere_id else None
        suivi = (suivis.get((conv.classe_id, ancre.pk, em_id, ep.type_epreuve.upper()))
                 if ancre and em_id else None)
        salle = conv.salle.nom if conv.salle_id else ((suivi or {}).get('salle__nom') or '')
        reunis, paralleles = [], []
        for autre in par_epreuve[ep.pk]:
            if autre.classe_id == conv.classe_id:
                continue
            autre_salle = autre.salle.nom if autre.salle_id else ''
            if salle and autre_salle == salle:
                reunis.append(noms.get(autre.classe_id, ''))
            else:
                paralleles.append({'groupe': noms.get(autre.classe_id, ''), 'salle': autre_salle})
        for c in colonnes:
            if not ep.recouvre(c):
                continue
            cle = f'{conv.classe_id}:{c.pk}'
            if cle in cases:
                cases[cle]['doublon'] = True
                continue
            case = _case_vide()
            case.update({
                'epreuve': ep.pk,
                'em_code': ep.matiere.code if ep.matiere_id else '',
                'em_intitule': ep.intitule_affiche,
                'type': f'{ep.type_epreuve} {ep.numero}' if ep.numero > 1 else ep.type_epreuve,
                'evaluation': True,
                'horaire': f'{ep.heure_debut:%Hh%M}-{ep.heure_fin:%Hh%M}',
                # Le surveillant n'est pas une donnée de l'épreuve : il se
                # désigne au remplissage du suivi, sur la ligne de l'épreuve.
                'prof': (suivi or {}).get('prof__nom') or '',
                'salle': salle,
                'reunis': reunis, 'paralleles': paralleles,
            })
            ouvrable(case, conv.classe_id, suivi)
            cases[cle] = case

    # 2. Les séances. Un même cours : même créneau, même matière, même
    #    enseignant — RÉUNI quand les groupes partagent la salle, EN PARALLÈLE
    #    sinon.
    cours = defaultdict(list)
    for s in seances:
        if not s.annulee and s.matiere_id:
            cours[(s.creneau_id, s.matiere_id, s.prof_id)].append(s)

    par_sous_groupe = defaultdict(list)       # (classe, créneau) → séances de TP
    for s in seances:
        unite = s.sous_groupe_id or s.classe_id
        if s.sous_groupe_id and not s.annulee:
            par_sous_groupe[(s.classe_id, s.creneau_id)].append(s)
        cle = f'{unite}:{s.creneau_id}'
        if cle in cases:
            if not s.annulee:
                cases[cle]['doublon'] = True
            continue
        libelle = s.type_seance.type_seance if s.type_seance_id else ''
        salle = s.salle.nom if s.salle_id else ''
        reunis, paralleles = [], []
        if not s.annulee and s.matiere_id:
            for autre in cours[(s.creneau_id, s.matiere_id, s.prof_id)]:
                autre_unite = autre.sous_groupe_id or autre.classe_id
                if autre_unite == unite:
                    continue
                autre_salle = autre.salle.nom if autre.salle_id else ''
                if salle and autre_salle == salle:
                    reunis.append(noms.get(autre_unite, ''))
                else:
                    paralleles.append({'groupe': noms.get(autre_unite, ''), 'salle': autre_salle})
        case = _case_vide()
        case.update({
            'seance': s.pk,
            'em_code': s.matiere.code if s.matiere_id else '',
            'em_intitule': s.matiere.intitule if s.matiere_id else '',
            'type': libelle,
            'special': bool(s.type_seance_id and s.type_seance.is_special),
            'evaluation': libelle.strip().upper() in TYPES_EPREUVE,
            'prof': s.prof.nom if s.prof_id else '',
            'salle': salle,
            'annulee': bool(s.annulee),
            'reunis': reunis, 'paralleles': paralleles,
        })
        em_id = s.matiere.em_miroir_id if s.matiere_id else None
        suivi = (suivis.get((unite, s.creneau_id, em_id, libelle.strip().upper()))
                 if em_id else None)
        if em_id or not case['special']:
            ouvrable(case, unite, suivi)
        cases[cle] = case

    # 3. La classe éclatée en sous-groupes : sa ligne dit où est chacun.
    for (classe_id, creneau_id), tps in par_sous_groupe.items():
        detail = [{'groupe': s.sous_groupe.groupe, 'salle': s.salle.nom if s.salle_id else '',
                   'type': s.type_seance.type_seance if s.type_seance_id else '',
                   'em_code': s.matiere.code if s.matiere_id else '',
                   'prof': s.prof.nom if s.prof_id else ''}
                  for s in sorted(tps, key=lambda s: (s.sous_groupe.groupe, s.pk))]
        cle = f'{classe_id}:{creneau_id}'
        if cle in cases:
            cases[cle]['sous_groupes'] = detail
            if not cases[cle]['annulee']:
                cases[cle]['doublon'] = True   # la classe ET un sous-groupe : conflit
            continue
        case = _case_vide()
        case.update({'dedouble': True, 'sous_groupes': detail})
        cases[cle] = case

    # 4. Le sous-groupe suit sa classe partout où il n'a pas de séance à lui.
    for l in lignes:
        if not l['sous_groupe']:
            continue
        for c in colonnes:
            cle = f"{l['id']}:{c.pk}"
            mere = cases.get(f"{l['classe']}:{c.pk}")
            if cle in cases or mere is None or mere['dedouble']:
                continue
            cases[cle] = {**mere, 'herite': True, 'sous_groupes': []}

    numero, type_semestre = None, ''
    if jour:
        numero, type_semestre = jour.numero_semaine, jour.type_semestre
    elif seances:
        semaine = seances[0].semaine
        numero, type_semestre = semaine.numero, semaine.semestre.type_semestre
    return {
        'annee': annee,
        'date': date.isoformat(),
        'date_fr': date.strftime('%d/%m/%Y'),
        'jour': jour.jour_fk.jour if jour and jour.jour_fk_id else JOURS[date.weekday()],
        'dans_le_calendrier': jour is not None,
        'numero_semaine': numero,
        'type_semestre': type_semestre,
        # Férié, vacances, examens : la journée le dit, et l'écran le montre.
        'type_semaine': jour.type_semaine if jour else '',
        'a_venir': a_venir,
        'droits_saisie': droits_saisie,
        'creneaux': [{'id': c.pk, 'libelle': c.creneau, 'inactif': not c.is_actif}
                     for c in colonnes],
        'groupes': lignes,
        'cases': cases,
    }


def grille(j: dict) -> list:
    """La journée en lignes, pour l'impression : une par classe ou sous-groupe,
    sous son niveau, avec une case par créneau (None quand le groupe est libre)."""
    niveaux, courant = [], None
    for g in j['groupes']:
        if courant is None or courant['niveau'] != g['niveau']:
            courant = {'niveau': g['niveau'], 'niveau_libelle': g['niveau_libelle'],
                       'groupes': []}
            niveaux.append(courant)
        courant['groupes'].append({
            'libelle': g['libelle'], 'nom': g['nom'], 'sous_groupe': g['sous_groupe'],
            'cases': [j['cases'].get(f"{g['id']}:{c['id']}") for c in j['creneaux']],
        })
    return niveaux


def semaine(annee: str, type_semestre: str, numero: int) -> list:
    """Les journées de cours de cette semaine, dans l'ordre des dates.

    Le calendrier du socle (`parametres.Semaine`, un jour par ligne) fait foi ;
    une année antérieure qui n'y figure pas se lit sur les semaines IPGEI, du
    lundi au samedi."""
    from apps.parametres.models import Semaine
    dates = list(Semaine.objects
                 .filter(annee_universitaire=annee, type_semestre=type_semestre,
                         numero_semaine=numero, type_semaine=Semaine.TYPE_COURS)
                 .order_by('date').values_list('date', flat=True).distinct())
    if not dates and not Semaine.objects.filter(annee_universitaire=annee).exists():
        codes = [code for code, (_, t) in SEMESTRES.items() if t == type_semestre]
        lundis = (SemaineIPGEI.objects
                  .filter(semestre__annee_universitaire=annee, semestre__code__in=codes,
                          numero=numero, type_semaine=SemaineIPGEI.TYPE_COURS)
                  .values_list('date_debut', flat=True).distinct())
        dates = sorted({lundi + dt.timedelta(days=i) for lundi in lundis for i in range(6)})
    return [journee(annee, d) for d in dates]
