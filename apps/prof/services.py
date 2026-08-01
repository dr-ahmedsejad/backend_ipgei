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


def creer_compte_pour_prof(prof) -> bool:
    """Crée un CustomUser pour un prof et le lie. Retourne True si créé, False sinon.

    Conditions cumulatives pour la création :
      - prof.user_id doit être null (pas déjà lié)
      - prof.telephone doit être renseigné (sert de username)
      - le username (= telephone) ne doit pas déjà exister

    Le compte est créé avec :
      - username = prof.telephone
      - password = prof.NNI (à changer obligatoirement au 1er login)
      - role     = 'enseignant'
      - UserContexte.annee_universitaire = Year la plus récente
      - UserContexte.semestre = 'Impairs'

    Cette fonction est appelée :
      1. Automatiquement par le signal post_save sur Prof (toute création unitaire)
      2. Manuellement par les endpoints REST `/profs/generer-comptes/` et
         `/profs/{id}/generer-compte/` pour rattraper les imports en masse,
         migrations, ou cas de silent fail.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()

    if prof.user_id or not prof.telephone:
        return False
    username = str(prof.telephone)
    if User.objects.filter(username=username).exists():
        return False

    user = User.objects.create_user(
        username=username,
        password=str(prof.NNI),
        name=prof.nom,
        email=prof.email or '',
        role='enseignant',
        is_active=True,
    )
    user.doit_changer_mdp = True
    user.save(update_fields=['doit_changer_mdp'])

    prof.user = user
    prof.save(update_fields=['user'])

    # UserContexte avec l'année la plus récente (FK vers annee.annee — refuse '')
    from apps.authentication.models import UserContexte
    from apps.parametres.models import Year
    latest_year = (
        Year.objects.filter(est_active=True).order_by('-annee').values_list('annee', flat=True).first()
        or Year.objects.order_by('-annee').values_list('annee', flat=True).first()
    )
    if latest_year:
        UserContexte.objects.get_or_create(
            user=user,
            defaults={'annee_universitaire': latest_year, 'semestre': 'Impairs'},
        )
    return True
