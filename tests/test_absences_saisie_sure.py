"""
Lot 1 du module absences à l'IPGEI (17/10/2026) : saisie sûre et droits.

  * `presences/bulk/` ne défait pas une absence justifiée, et ne pose « J »
    que pour qui peut justifier (`abs_justificatifs` / `modifier`) ;
  * la fiche d'une présence (POST, PUT, PATCH) : entrer en justifiée ou en
    sortir exige le même droit ;
  * écrire exige `abs_saisie` / `modifier` — à l'IPGEI c'est déjà le cas, par
    le verbe HTTP (`core.permissions.METHODE_ACTION`) ;
  * la pagination des présences est stable (ordre total) ;
  * le reflet de l'émargement (`miroir.refleter_absences`) ne récrit jamais
    une justifiée.
"""
import pytest

from tests._absences_ipgei import (  # noqa: F401  (fixtures)
    agent_de_saisie, api, droit, inscrit, monde, seance, suivi, superutilisateur,
)
from tests.factories.auth import UserFactory

URL = '/api/v1/absences/presences/bulk/'


@pytest.fixture
def saisie(monde):
    """Une séance de MPSI A, une absence justifiée et une absence ordinaire."""
    from apps.absence.models import Presence
    s = suivi(monde, seance(monde))
    just = inscrit(monde, '901')
    abs_ = inscrit(monde, '902')
    Presence.objects.create(suivi=s, etudiant=just, statut=3, commentaire='certificat')
    Presence.objects.create(suivi=s, etudiant=abs_, statut=1)
    return s, just, abs_


# ── bulk : la justifiée gardée ───────────────────────────────────────────────

def test_une_justifiee_n_est_pas_remise_en_present_par_la_saisie(saisie):
    from apps.absence.models import Presence
    s, just, abs_ = saisie
    r = api(agent_de_saisie()).post(URL, {'suivi_id': s.pk, 'presences': [
        {'etudiant_id': just.pk, 'statut': 0, 'commentaire': ''},
        {'etudiant_id': abs_.pk, 'statut': 0, 'commentaire': ''},
    ]}, format='json')
    assert r.status_code == 200, r.data
    assert (r.data['updated'], r.data['justifiees_preservees'], r.data['justifiees_refusees']) == (1, 1, 0)
    p = Presence.objects.get(suivi=s, etudiant=just)
    assert (p.statut, p.commentaire) == (3, 'certificat')
    # Une absence ordinaire, elle, se corrige toujours.
    assert Presence.objects.get(suivi=s, etudiant=abs_).statut == 0


def test_qui_peut_justifier_peut_aussi_defaire(saisie):
    from apps.absence.models import Presence
    s, just, _ = saisie
    r = api(superutilisateur()).post(URL, {'suivi_id': s.pk, 'presences': [
        {'etudiant_id': just.pk, 'statut': 0, 'commentaire': ''},
    ]}, format='json')
    assert r.status_code == 200, r.data
    assert r.data['justifiees_preservees'] == 0
    assert Presence.objects.get(suivi=s, etudiant=just).statut == 0


def test_le_droit_de_justifier_vient_de_la_matrice(saisie):
    """Un DA a `abs_justificatifs` / `modifier` à l'IPGEI : il défait."""
    from apps.absence.models import Presence
    s, just, _ = saisie
    agent = agent_de_saisie('DA')
    droit('DA', 'abs_justificatifs', 'modifier')
    r = api(agent).post(URL, {'suivi_id': s.pk, 'presences': [
        {'etudiant_id': just.pk, 'statut': 1, 'commentaire': ''},
    ]}, format='json')
    assert r.status_code == 200, r.data
    assert Presence.objects.get(suivi=s, etudiant=just).statut == 1


def test_une_justifiee_renvoyee_telle_quelle_reste_justifiee(saisie):
    from apps.absence.models import Presence
    s, just, _ = saisie
    r = api(agent_de_saisie()).post(URL, {'suivi_id': s.pk, 'presences': [
        {'etudiant_id': just.pk, 'statut': 3, 'commentaire': 'certificat'},
    ]}, format='json')
    assert r.status_code == 200, r.data
    assert r.data['justifiees_refusees'] == 0
    assert Presence.objects.get(suivi=s, etudiant=just).statut == 3


def test_un_identifiant_en_texte_ne_contourne_pas_la_garde(saisie):
    from apps.absence.models import Presence
    s, just, _ = saisie
    r = api(agent_de_saisie()).post(URL, {'suivi_id': s.pk, 'presences': [
        {'etudiant_id': str(just.pk), 'statut': '0', 'commentaire': ''},
    ]}, format='json')
    assert r.status_code == 200, r.data
    assert r.data['justifiees_preservees'] == 1
    assert Presence.objects.get(suivi=s, etudiant=just).statut == 3


def test_un_identifiant_illisible_refuse_tout_l_envoi(saisie):
    from apps.absence.models import Presence
    s, _, abs_ = saisie
    r = api(agent_de_saisie()).post(URL, {'suivi_id': s.pk, 'presences': [
        {'etudiant_id': abs_.pk, 'statut': 0, 'commentaire': ''},
        {'etudiant_id': 'douze', 'statut': 0, 'commentaire': ''},
    ]}, format='json')
    assert r.status_code == 400
    # Rien d'écrit, pas même la ligne lisible.
    assert Presence.objects.get(suivi=s, etudiant=abs_).statut == 1


def test_un_statut_hors_bornes_est_refuse(saisie):
    s, _, abs_ = saisie
    r = api(agent_de_saisie()).post(URL, {'suivi_id': s.pk, 'presences': [
        {'etudiant_id': abs_.pk, 'statut': 7, 'commentaire': ''},
    ]}, format='json')
    assert r.status_code == 400


# ── bulk : poser « J » ───────────────────────────────────────────────────────

def test_sans_droit_de_justifier_on_ne_pose_pas_j(saisie, monde):
    from apps.absence.models import Presence
    s, _, abs_ = saisie
    nouveau = inscrit(monde, '903')
    r = api(agent_de_saisie()).post(URL, {'suivi_id': s.pk, 'presences': [
        {'etudiant_id': abs_.pk, 'statut': 3, 'commentaire': ''},
        {'etudiant_id': nouveau.pk, 'statut': '3', 'commentaire': ''},
    ]}, format='json')
    assert r.status_code == 200, r.data
    assert (r.data['updated'], r.data['justifiees_refusees']) == (0, 2)
    assert Presence.objects.get(suivi=s, etudiant=abs_).statut == 1
    assert not Presence.objects.filter(suivi=s, etudiant=nouveau).exists()


def test_qui_peut_justifier_pose_j(saisie):
    from apps.absence.models import Presence
    s, _, abs_ = saisie
    agent = agent_de_saisie()
    droit('scolarite', 'abs_justificatifs', 'modifier')
    r = api(agent).post(URL, {'suivi_id': s.pk, 'presences': [
        {'etudiant_id': abs_.pk, 'statut': 3, 'commentaire': ''},
    ]}, format='json')
    assert r.status_code == 200, r.data
    assert r.data['justifiees_refusees'] == 0
    assert Presence.objects.get(suivi=s, etudiant=abs_).statut == 3


# ── Le droit d'écrire ────────────────────────────────────────────────────────

def test_qui_ne_peut_que_voir_ne_peut_pas_ecrire(saisie):
    """À l'IPGEI, un `@action` en POST demande `modifier` (le verbe HTTP
    tranche) : le DA, qui n'a que `voir` sur `abs_saisie`, ne réécrit rien."""
    from apps.absence.models import Presence
    s, _, abs_ = saisie
    droit('DA', 'abs_saisie', 'voir')
    r = api(UserFactory(username='u_da', role='DA')).post(URL, {'suivi_id': s.pk, 'presences': [
        {'etudiant_id': abs_.pk, 'statut': 0, 'commentaire': ''},
    ]}, format='json')
    assert r.status_code == 403
    assert Presence.objects.get(suivi=s, etudiant=abs_).statut == 1


# ── La fiche d'une présence : « J » réservée ─────────────────────────────────

def _url(p):
    return '/api/v1/absences/presences/%d/' % p.pk


def test_sans_droit_de_justifier_ni_vers_3_ni_depuis_3(saisie, monde):
    from apps.absence.models import Presence
    s, just, abs_ = saisie
    client = api(agent_de_saisie())
    p_abs = Presence.objects.get(suivi=s, etudiant=abs_)
    p_just = Presence.objects.get(suivi=s, etudiant=just)

    r = client.patch(_url(p_abs), {'statut': '3'}, format='json')     # en texte
    assert r.status_code == 403
    assert 'justificatifs' in str(r.data)
    assert client.patch(_url(p_just), {'statut': 1}, format='json').status_code == 403
    r = client.put(_url(p_just), {'suivi': s.pk, 'etudiant': just.pk, 'statut': 0,
                                  'commentaire': ''}, format='json')
    assert r.status_code == 403
    nouveau = inscrit(monde, '904')
    r = client.post('/api/v1/absences/presences/', {'suivi': s.pk, 'etudiant': nouveau.pk,
                                                    'statut': 3}, format='json')
    assert r.status_code == 403

    assert Presence.objects.get(pk=p_abs.pk).statut == 1
    p_just.refresh_from_db()
    assert (p_just.statut, p_just.commentaire) == (3, 'certificat')
    assert not Presence.objects.filter(suivi=s, etudiant=nouveau).exists()


def test_sans_droit_de_justifier_le_reste_passe(saisie):
    from apps.absence.models import Presence
    s, just, abs_ = saisie
    client = api(agent_de_saisie())
    p_abs = Presence.objects.get(suivi=s, etudiant=abs_)
    assert client.patch(_url(p_abs), {'statut': 0}, format='json').status_code == 200
    assert client.patch(_url(p_abs), {'statut': 2, 'commentaire': 'retard'},
                        format='json').status_code == 200
    # Une justifiée qu'on commente sans changer son statut.
    p_just = Presence.objects.get(suivi=s, etudiant=just)
    assert client.patch(_url(p_just), {'commentaire': 'vu'}, format='json').status_code == 200
    assert Presence.objects.get(pk=p_abs.pk).statut == 2
    assert Presence.objects.get(pk=p_just.pk).statut == 3


def test_avec_droit_de_justifier_on_pose_et_on_retire(saisie):
    from apps.absence.models import Presence
    s, just, abs_ = saisie
    agent = agent_de_saisie()
    droit('scolarite', 'abs_justificatifs', 'modifier')
    client = api(agent)
    p_abs = Presence.objects.get(suivi=s, etudiant=abs_)
    p_just = Presence.objects.get(suivi=s, etudiant=just)
    assert client.patch(_url(p_abs), {'statut': 3}, format='json').status_code == 200
    assert client.patch(_url(p_just), {'statut': 1}, format='json').status_code == 200
    assert Presence.objects.get(pk=p_abs.pk).statut == 3
    assert Presence.objects.get(pk=p_just.pk).statut == 1


# ── La pagination ────────────────────────────────────────────────────────────

def test_la_pagination_ne_repete_ni_n_omet_aucune_presence(monde):
    from apps.absence.models import Presence
    from apps.absence.views import PresenceViewSet
    assert PresenceViewSet.ordering[-1] == 'id'
    s = suivi(monde, seance(monde))
    for i in range(5):
        Presence.objects.create(suivi=s, etudiant=inscrit(monde, '95%d' % i), statut=1)
    client = api(superutilisateur())
    vus = []
    for page in (1, 2):
        r = client.get('/api/v1/absences/presences/', {'suivi': s.pk, 'page_size': 3, 'page': page})
        assert r.status_code == 200, r.data
        vus += [p['id'] for p in r.data['results']]
    assert vus == sorted(Presence.objects.filter(suivi=s).values_list('id', flat=True))


def test_le_plafond_de_pagination_est_celui_que_lit_l_ecran():
    """L'écran charge page après page avec `page_size=1000` : c'est le
    plafond du serveur (`core/pagination.py`). Au-delà, le surplus serait
    tronqué en silence."""
    from core.pagination import StandardPagination
    assert StandardPagination.max_page_size == 1000


# ── Le reflet de l'émargement ────────────────────────────────────────────────

def test_l_emargement_ne_recrit_pas_une_justifiee(monde):
    """Chaque enregistrement de la feuille d'émargement — et chaque génération
    du suivi — rejouait le reflet, qui remettait en « Absent » une absence
    justifiée entre-temps dans « Justificatifs »."""
    from apps.absence.models import Presence
    from apps.ipgei import services
    from apps.ipgei.models import InscriptionIPGEI
    ep = seance(monde, matiere='PHYS', type_='DS')
    s = suivi(monde, ep)
    a, b = inscrit(monde, '801'), inscrit(monde, '802')
    ia = InscriptionIPGEI.objects.get(etudiant=a)
    ib = InscriptionIPGEI.objects.get(etudiant=b)

    services.saisir_absences(ep, [{'inscription': ia.pk, 'statut': 'absent'},
                                  {'inscription': ib.pk, 'statut': 'absent'}])
    assert set(Presence.objects.filter(suivi=s).values_list('etudiant_id', 'statut')) == {
        (a.pk, 1), (b.pk, 1)}

    # La scolarité justifie A dans « Justificatifs ».
    Presence.objects.filter(suivi=s, etudiant=a).update(statut=3, commentaire='certificat')
    # La feuille est réenregistrée (B corrigé au passage).
    services.saisir_absences(ep, [{'inscription': ia.pk, 'statut': 'absent'},
                                  {'inscription': ib.pk, 'statut': 'absent',
                                   'justificatif': 'arrivé en retard'}])
    pa = Presence.objects.get(suivi=s, etudiant=a)
    assert (pa.statut, pa.commentaire) == (3, 'certificat')
    assert Presence.objects.get(suivi=s, etudiant=b).commentaire == 'arrivé en retard'


# ── La migration 0027 : le DA écrit (17/10/2026, décision de l'utilisateur) ──

def _migration_0027():
    import importlib
    return importlib.import_module(
        'apps.authentication.migrations.0027_abs_saisie_modifier_da')


def test_la_migration_donne_l_ecriture_au_da(saisie, settings):
    from django.apps import apps

    from apps.absence.models import Presence
    from apps.authentication.models import RoleDefault
    settings.SEEDS_DESACTIVES_POUR_TESTS = False
    s, _, abs_ = saisie
    droit('DA', 'abs_saisie', 'voir')
    # Un refus déjà posé pour un autre rôle reste un refus.
    droit('DG', 'abs_saisie', 'modifier')
    RoleDefault.objects.filter(role='DG', module_action__action__code='modifier') \
        .update(allowed=False)

    _migration_0027().ajouter(apps, None)
    _migration_0027().ajouter(apps, None)          # idempotente

    droits = RoleDefault.objects.filter(module_action__module__code='abs_saisie',
                                        module_action__action__code='modifier')
    assert dict(droits.values_list('role', 'allowed')) == {'DA': True, 'DG': False}

    r = api(UserFactory(username='u_da2', role='DA')).post(URL, {'suivi_id': s.pk, 'presences': [
        {'etudiant_id': abs_.pk, 'statut': 0, 'commentaire': ''},
    ]}, format='json')
    assert r.status_code == 200, r.data
    assert Presence.objects.get(suivi=s, etudiant=abs_).statut == 0


def test_la_migration_ne_fait_rien_sur_la_base_de_test(db):
    from django.apps import apps

    from apps.authentication.models import RoleDefault
    _migration_0027().ajouter(apps, None)          # SEEDS_DESACTIVES_POUR_TESTS
    assert not RoleDefault.objects.filter(role='DA').exists()
