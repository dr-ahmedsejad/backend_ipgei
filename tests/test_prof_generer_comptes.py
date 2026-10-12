"""
« Générer les comptes » sur l'écran Enseignants (recette du 02/10/2026).

Rattrape les enseignants restés sans compte (création automatique ratée,
import en masse). Créer des comptes est un geste d'ADMINISTRATEUR ; seuls les
enseignants ACTIFS en reçoivent un — jamais le personnel administratif ou
militaire, jamais une fiche archivée ; un cas qui coince n'arrête pas les
autres, et la réponse dit qui a reçu un compte et pourquoi les autres non.

À l'IPGEI, le courriel reste obligatoire pour un compte (apps/prof/
test_comptes_enseignants.py) : les fiches de test en ont un.
"""
import pytest
from rest_framework.test import APIClient

from apps.authentication.models import CustomUser
from apps.banque.models import Banque
from apps.prof.models import Prof
from tests.factories.auth import AdminUserFactory, UserFactory

pytestmark = pytest.mark.django_db
URL = '/api/v1/profs/generer-comptes/'


def _prof(n, **kw):
    """Une fiche SANS compte : on coupe la création automatique, comme le
    ferait un import en masse."""
    banque = Banque.objects.get_or_create(nom='Banque test')[0]
    p = Prof(NNI=9700000000 + n, nom=kw.pop('nom', f'Prof {n}'), telephone=kw.pop('telephone', 96000000 + n),
             email=kw.pop('email', f'prof{n}@exemple.mr'), banque=banque, type=kw.pop('type', 'vacataire'), **kw)
    p._skip_account_signal = True
    p.save()
    p._skip_account_signal = False
    assert p.user_id is None
    return p


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def test_l_admin_genere_les_comptes_des_enseignants_actifs_sans_compte():
    a, b = _prof(1), _prof(2, type='permanent')
    r = _client(AdminUserFactory()).post(URL)
    assert r.status_code == 200, r.content
    d = r.json()
    assert d['created'] == 2, d
    assert sorted(x['nom'] for x in d['crees']) == ['Prof 1', 'Prof 2']
    for p in (a, b):
        p.refresh_from_db()
        assert p.user_id and p.user.role == 'enseignant' and p.user.doit_changer_mdp
    # Rejouer ne crée rien de plus.
    assert _client(AdminUserFactory()).post(URL).json()['created'] == 0


def test_ni_le_personnel_ni_une_fiche_archivee_ne_recoivent_de_compte():
    adm = _prof(1, type='personnel_admin')
    mil = _prof(2, type='personnel_militaire')
    arch = _prof(3, actif=False)
    r = _client(AdminUserFactory()).post(URL)
    assert r.json()['created'] == 0
    for p in (adm, mil, arch):
        p.refresh_from_db()
        assert p.user_id is None


def test_un_cas_qui_coince_n_arrete_pas_les_autres_et_dit_pourquoi():
    UserFactory(username='96000001')                 # le téléphone de Prof 1 est déjà un identifiant
    _prof(1)
    sans_tel = _prof(2, telephone=None)
    ok = _prof(3)
    r = _client(AdminUserFactory()).post(URL)
    assert r.status_code == 200, r.content
    d = r.json()
    assert [x['nom'] for x in d['crees']] == ['Prof 3']
    raisons = {x['nom']: x['raison'] for x in d['non_crees']}
    assert 'déjà' in raisons['Prof 1']
    assert 'éléphone' in raisons['Prof 2']
    ok.refresh_from_db(); sans_tel.refresh_from_db()
    assert ok.user_id and sans_tel.user_id is None


def _droit(role, module, action):
    from apps.authentication.models import Action, Module, ModuleAction, RoleDefault
    m, _ = Module.objects.get_or_create(code=module, defaults={'nom': module})
    a, _ = Action.objects.get_or_create(code=action, defaults={'nom': action})
    ma, _ = ModuleAction.objects.get_or_create(module=m, action=a)
    RoleDefault.objects.update_or_create(role=role, module_action=ma, defaults={'allowed': True})


@pytest.mark.parametrize('role', ['DE', 'RH', 'IT', 'scolarite', 'enseignant'])
def test_creer_des_comptes_est_reserve_a_l_administrateur(role):
    """MÊME avec le droit de modifier les enseignants : gérer les fiches
    n'est pas créer des comptes."""
    for action in ('voir', 'modifier'):
        _droit(role, 'profs', action)
    p = _prof(1)
    avant = CustomUser.objects.count()
    r = _client(UserFactory(role=role)).post(URL)
    assert r.status_code == 403, (role, r.status_code)
    r = _client(UserFactory(role=role, username=f'{role}_2')).post(f'/api/v1/profs/{p.pk}/generer-compte/')
    assert r.status_code == 403, (role, r.status_code)
    assert CustomUser.objects.count() == avant + 2     # les deux comptes de test, rien d'autre


def test_la_liste_dit_qui_a_un_compte():
    sans = _prof(1)
    avec = _prof(2)
    _client(AdminUserFactory()).post(f'/api/v1/profs/{avec.pk}/generer-compte/')
    r = _client(AdminUserFactory()).get('/api/v1/profs/', {'page_size': 50})
    d = r.json()
    lignes = {x['id']: x for x in d.get('results', d)}
    assert lignes[sans.pk]['a_un_compte'] is False
    assert lignes[avec.pk]['a_un_compte'] is True
