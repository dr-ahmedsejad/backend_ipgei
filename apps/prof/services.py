"""Helpers partagés pour gérer le statut historique d'un prof.

Branchés derrière le feature flag `USE_PROF_TYPE_HISTORY` (.env).

Usage type :

    from apps.prof.services import use_prof_type_history, prof_ids_with_type_in_month

    if use_prof_type_history():
        ids = prof_ids_with_type_in_month('vacataire', year, month)
    else:
        ids = list(Prof.objects.filter(type='vacataire').values_list('id', flat=True))

NB : on lit `.env` via `decouple.config` car le projet n'utilise PAS os.environ
direct (cf. siga.settings.base : `from decouple import config`).
"""
import datetime as _dt
from django.db.models import Q


def use_prof_type_history() -> bool:
    """Retourne True si la BD `prof_type_history` doit etre prise en compte.

    Default : False (comportement legacy : on filtre sur Prof.type courant).
    Activable via `.env` : USE_PROF_TYPE_HISTORY=true
    """
    from decouple import config
    return config('USE_PROF_TYPE_HISTORY', default='false').lower() == 'true'


def _month_bounds(year: int, month: int) -> tuple[_dt.date, _dt.date]:
    """Retourne (premier_jour, dernier_jour) du mois donne."""
    debut = _dt.date(year, month, 1)
    if month == 12:
        fin = _dt.date(year + 1, 1, 1) - _dt.timedelta(days=1)
    else:
        fin = _dt.date(year, month + 1, 1) - _dt.timedelta(days=1)
    return debut, fin


def prof_ids_with_type_in_month(prof_type: str, year: int, month: int) -> list[int]:
    """IDs des profs qui avaient le statut `prof_type` AU COURS du mois donne.

    Periode du mois = [1er, dernier_jour].
    Match si la periode [date_debut, date_fin] de prof_type_history recoupe
    le mois (chevauchement). date_fin IS NULL = encore en cours.
    """
    from .models import ProfTypeHistory
    debut, fin = _month_bounds(year, month)
    qs = ProfTypeHistory.objects.filter(type=prof_type, date_debut__lte=fin)
    qs = qs.filter(Q(date_fin__isnull=True) | Q(date_fin__gte=debut))
    return list(qs.values_list('prof_id', flat=True).distinct())


def prof_ids_with_type_at(prof_type: str, at_date: _dt.date) -> list[int]:
    """IDs des profs qui avaient le statut `prof_type` a une date precise."""
    from .models import ProfTypeHistory
    qs = ProfTypeHistory.objects.filter(type=prof_type, date_debut__lte=at_date)
    qs = qs.filter(Q(date_fin__isnull=True) | Q(date_fin__gte=at_date))
    return list(qs.values_list('prof_id', flat=True).distinct())


def vac_ids_for_month(year: int, month: int) -> list[int]:
    """Alias commode : IDs des vacataires AU COURS du mois donne.

    NE PAS modifier : utilise par apps/avancement/* pour la charge enseignante.
    Les personnel_admin/militaire n'enseignent pas → doivent rester exclus ici.
    """
    return prof_ids_with_type_in_month('vacataire', year, month)


# Types de profs payes a l'heure (vacation/etat/attestation/fiches) :
# - vacataire    : enseignant vacataire
# - personnel_admin : personnel administratif (surveillance, encadrement via vacation)
# - personnel_militaire : personnel militaire (surveillance, encadrement via vacation)
TYPES_PAYES_A_LHEURE = ['vacataire', 'personnel_admin', 'personnel_militaire']


def payes_a_lheure_ids_for_month(year: int, month: int) -> list[int]:
    """IDs des profs qui etaient PAYES A L'HEURE au cours du mois donne.

    Inclut vacataire + personnel_admin + personnel_militaire (cf TYPES_PAYES_A_LHEURE).
    Utilise pour les recap mensuels paie (vacation/etat, attestation, fiches).
    NE PAS utiliser pour la charge enseignante -> voir vac_ids_for_month.

    Eligibilite en DEUX volets :

    (A) Historique : statut paye-a-l'heure ACTIF pendant le mois. Gere correctement
        les bascules de statut via `date_fin` (un prof devenu permanent au 01/02 a son
        enreg. vacataire cloture -> exclu des mois suivants).

    (B) Rescousse "saisie tardive" UNIQUEMENT : un prof dont le travail du mois
        (vacation ou pointage "Fait") PRECEDE TOUT son historique enregistre, et dont
        le tout premier statut connu est paye-a-l'heure -> on retro-etend ce statut.
        Couvre le cas "on enregistre / on marque les seances un mois apres".

        La condition `premier.date_debut > fin` est la cle : elle distingue une
        SAISIE TARDIVE (tout l'historique est posterieur au mois) d'une BASCULE de
        statut (l'historique entoure deja le mois -> on fait confiance a date_fin et
        on ne rescousse PAS, sinon on repaie a l'heure un ex-vacataire devenu permanent).
    """
    from .models import ProfTypeHistory, Prof
    debut, fin = _month_bounds(year, month)

    # (A) Historique actif pendant le mois
    qs = ProfTypeHistory.objects.filter(
        type__in=TYPES_PAYES_A_LHEURE,
        date_debut__lte=fin,
    )
    qs = qs.filter(Q(date_fin__isnull=True) | Q(date_fin__gte=debut))
    hist_ids = set(qs.values_list('prof_id', flat=True).distinct())

    # (B) Rescousse saisie tardive
    from apps.vacation.models import Vacation
    from apps.suivi.models import SuiviePointage
    worked = set(
        Vacation.objects.filter(date__year=year, date__month=month)
        .values_list('prof_id', flat=True)
    )
    worked |= set(
        SuiviePointage.objects.filter(
            date_suivie__year=year, date_suivie__month=month, commentaire='Fait'
        ).values_list('prof_id', flat=True)
    )
    candidats = worked - hist_ids - {None}
    if candidats:
        # 1er statut (date_debut min) de chaque candidat, en 1 requete
        premiers = {}
        for h in (ProfTypeHistory.objects
                  .filter(prof_id__in=candidats)
                  .order_by('prof_id', 'date_debut')):
            premiers.setdefault(h.prof_id, h)  # 1er vu = date_debut la plus petite
        for pid in candidats:
            first = premiers.get(pid)
            if first and first.type in TYPES_PAYES_A_LHEURE and first.date_debut > fin:
                hist_ids.add(pid)

    return list(hist_ids)


def vac_ids_legacy() -> list[int]:
    """Helper legacy : tous les vacataires actuels (snapshot Prof.type).

    NE PAS modifier : utilise par avancement (charge enseignante).
    """
    from .models import Prof
    return list(
        Prof.objects.filter(type__iexact='vacataire').values_list('id', flat=True)
    )


def payes_a_lheure_ids_legacy() -> list[int]:
    """Helper legacy paie : tous les profs actuellement payes a l'heure
    (vacataire + personnel_admin + personnel_militaire) — snapshot Prof.type."""
    from .models import Prof
    return list(
        Prof.objects.filter(type__in=TYPES_PAYES_A_LHEURE).values_list('id', flat=True)
    )


def creer_compte_pour_prof_detaille(prof) -> tuple[bool, str]:
    """Crée un CustomUser pour un prof et le lie. Rend `(créé, raison)`.

    Conditions cumulatives, toutes vérifiées AVANT la moindre écriture :

      - `prof.user_id` vide — pas déjà lié ;
      - `prof.telephone` renseigné — il sert d'identifiant de connexion ;
      - `prof.email` renseigné — et c'est la condition qui a coûté le plus
        cher. `CustomUser.email` est UNIQUE, et une chaîne vide n'est pas un
        NULL : PostgreSQL l'indexe comme une valeur ordinaire. Un seul compte
        à courriel vide suffit donc à empêcher la création de TOUS les
        suivants. C'est arrivé en production : le compte de test « 22000001 »,
        créé le 28/07/2026, a bloqué 27 enseignants pendant trois mois sans
        que rien ne s'affiche à l'écran ;
      - ni l'identifiant ni le courriel déjà portés par un autre compte.

    Le compte est créé avec :
      - username = `prof.telephone`
      - password = `prof.NNI`, changement imposé à la première connexion
      - role     = `enseignant`
      - `UserContexte` sur l'année la plus récente, semestre « Impairs »

    La raison rendue est faite pour être AFFICHÉE : elle dit quoi corriger.
    C'est tout l'objet de cette variante — l'ancienne ne rendait qu'un booléen,
    et l'appelant ne pouvait que constater l'échec sans jamais le nommer.

    Appelée :
      1. par le signal `post_save` sur Prof (toute création unitaire) ;
      2. par `/profs/generer-comptes/` et `/profs/{id}/generer-compte/` pour
         rattraper les imports en masse — `bulk_create` ne déclenche aucun
         signal — et les créations antérieures restées sans compte.
    """
    from django.contrib.auth import get_user_model
    from django.db import transaction

    User = get_user_model()

    if prof.user_id:
        return False, 'Ce professeur a déjà un compte.'
    if not prof.telephone:
        return False, ("Téléphone manquant : c'est l'identifiant de connexion.")

    email = (prof.email or '').strip()
    if not email:
        return False, ('Courriel manquant : il identifie le compte, et un '
                       'courriel vide empêcherait la création de tous les suivants.')

    username = str(prof.telephone)
    if User.objects.filter(username=username).exists():
        return False, (f"Le téléphone {username} sert déjà d'identifiant à un "
                       'autre compte : deux professeurs ne peuvent pas le partager.')
    if User.objects.filter(email__iexact=email).exists():
        return False, f'Le courriel {email} est déjà porté par un autre compte.'

    from apps.authentication.models import UserContexte
    from apps.parametres.models import Year

    # Un seul bloc, et un point de reprise : si la pose du contexte échoue, le
    # compte ne reste pas orphelin, à moitié créé. Il permet surtout à
    # l'appelant d'attraper une erreur de base sans condamner la transaction
    # qui l'englobe — celle qui crée le prof lui-même, notamment : sans
    # savepoint, une exception rattrapée ici ferait échouer tout le POST au
    # moment du commit.
    with transaction.atomic():
        user = User.objects.create_user(
            username=username,
            password=str(prof.NNI),
            name=prof.nom,
            email=email,
            role='enseignant',
            is_active=True,
        )
        user.doit_changer_mdp = True
        user.save(update_fields=['doit_changer_mdp'])

        prof.user = user
        prof.save(update_fields=['user'])

        # FK vers annee.annee, qui refuse '' : sans année en base, on pose le
        # compte sans contexte plutôt que de ne pas le poser du tout.
        latest_year = (
            Year.objects.filter(est_active=True).order_by('-annee').values_list('annee', flat=True).first()
            or Year.objects.order_by('-annee').values_list('annee', flat=True).first()
        )
        if latest_year:
            UserContexte.objects.get_or_create(
                user=user,
                defaults={'annee_universitaire': latest_year, 'semestre': 'Impairs'},
            )

    return True, f'Compte « {username} » créé.'


def creer_compte_pour_prof(prof) -> bool:
    """Variante booléenne, pour les appelants qui n'affichent pas la raison."""
    return creer_compte_pour_prof_detaille(prof)[0]


def generer_comptes_manquants():
    """Rattrape les comptes des enseignants ACTIFS qui n'en ont pas.

    Jamais le personnel administratif (`Prof.type` hors TYPES_ENSEIGNANTS) ni
    une fiche archivée. Chaque fiche est traitée seule : un cas qui coince
    n'arrête pas les autres. Rend (créés, non créés), chaque non-créé avec sa
    raison — l'écran l'affiche telle quelle.
    """
    from .models import Prof, TYPES_ENSEIGNANTS

    crees, non_crees = [], []
    profs = (Prof.objects.filter(user__isnull=True, actif=True, type__in=TYPES_ENSEIGNANTS)
             .order_by('nom'))
    for prof in profs:
        ligne = {'id': prof.pk, 'nom': prof.nom}
        try:
            cree, raison = creer_compte_pour_prof_detaille(prof)
        except Exception as exc:                              # noqa: BLE001
            cree, raison = False, f'erreur : {exc}'[:300]
        if cree:
            crees.append({**ligne, 'identifiant': str(prof.telephone)})
        else:
            non_crees.append({**ligne, 'raison': raison})
    return crees, non_crees
