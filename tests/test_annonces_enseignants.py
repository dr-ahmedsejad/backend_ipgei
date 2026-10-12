"""Annonces aux ENSEIGNANTS (comme à l'ESP, maquette validée le 11/10/2026).

L'écran envoie aux élèves des classes cochées, aux enseignants — tous, ou ceux
qui ont cours dans les classes cochées (emploi du temps de l'année) —, ou aux
deux.

Décor : `test_annonces` — MPSI A, MPSI B, MP A, MP C.
"""
import pytest

from apps.emplois.models import Emplois
from apps.notifications.models import Notification
from apps.parametres.models import Institution
from apps.prof.models import Prof
from tests.factories.auth import UserFactory
from tests.test_annonces import URL, api, ecole  # noqa: F401
from tests.test_annonces import ANNEE


@pytest.fixture
def decor(ecole):
    inst = Institution.objects.first()

    def prof(n, nom, *classes):
        p = Prof.objects.create(NNI=9800000000 + n, nom=nom, type='vacataire', actif=True)
        p.user = UserFactory(username=f'ens_{nom.lower()}', role='enseignant')
        p.save(update_fields=['user'])
        for c in classes:
            Emplois.objects.create(annee_universitaire=ANNEE, prof=p, departement=ecole['c'][c],
                                   institution=inst)
        return p.user

    ens = {'mpsi_a': prof(1, 'Mpsi', 'MPSI A'), 'mp_a': prof(2, 'Mp', 'MP A'),
           'sans_cours': prof(3, 'Libre')}
    return dict(ecole, ens=ens)


def _cle(decor, classe):
    return f"c{decor['c'][classe].pk}"


def _envoyer(decor, **corps):
    return api(decor['admin']).post(URL, {
        'titre': 'Réunion', 'texte': 'Jeudi 10h.', 'resume': 'test', **corps}, format='json')


def _recu(user):
    return Notification.objects.filter(destinataire=user, type='annonce').exists()


def test_la_grille_compte_les_enseignants_de_chaque_classe(decor):
    r = api(decor['admin']).get(f'{URL}grille/')
    assert r.status_code == 200, r.data
    cases = {c['cle']: c['nb_enseignants'] for c in r.data['cases']}
    assert cases[_cle(decor, 'MPSI A')] == 1
    assert cases[_cle(decor, 'MPSI B')] == 0
    assert r.data['nb_enseignants'] == 3


def test_tous_les_enseignants_sans_cocher_de_classe(decor):
    r = _envoyer(decor, etudiants=False, enseignants='tous', cibles=[])
    assert r.status_code == 201, r.data
    assert all(_recu(u) for u in decor['ens'].values())
    assert not _recu(decor['e']['mpsi_a'])


def test_les_enseignants_des_classes_cochees_seulement(decor):
    r = _envoyer(decor, etudiants=False, enseignants='cases', cibles=[_cle(decor, 'MPSI A')])
    assert r.status_code == 201, r.data
    assert _recu(decor['ens']['mpsi_a'])
    assert not _recu(decor['ens']['mp_a'])
    assert not _recu(decor['ens']['sans_cours'])
    assert not _recu(decor['e']['mpsi_a'])


def test_eleves_et_enseignants_ensemble(decor):
    r = _envoyer(decor, etudiants=True, enseignants='cases', cibles=[_cle(decor, 'MP A')])
    assert r.status_code == 201, r.data
    assert r.data['nb_destinataires'] == 2
    assert _recu(decor['e']['mp_a']) and _recu(decor['ens']['mp_a'])


def test_il_faut_au_moins_un_public(decor):
    r = _envoyer(decor, etudiants=False, enseignants='', cibles=[_cle(decor, 'MPSI A')])
    assert r.status_code == 400


def test_sans_precision_c_est_aux_eleves_comme_avant(decor):
    r = _envoyer(decor, cibles=[_cle(decor, 'MPSI A')])
    assert r.status_code == 201, r.data
    assert _recu(decor['e']['mpsi_a']) and not _recu(decor['ens']['mpsi_a'])
