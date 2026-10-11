"""Annonces IPGEI : un message aux élèves choisis par classe.

Une case = une classe de l'année active (MPSI A…) ; la ligne MPSI = toutes ses
classes. Chaque élève (inscrit, non sorti par abandon, avec un compte) reçoit
une notification « annonce » qui porte le texte entier.
"""
import pytest
from rest_framework.test import APIClient

from apps.absence.models import Etudiant
from apps.annonces.models import Annonce
from apps.ipgei.models import Classe, InscriptionIPGEI, NiveauIPGEI
from apps.notifications.models import Notification
from apps.parametres.models import Institution, Niveau, Year
from tests.factories.auth import UserFactory

ANNEE = '2026-2027'
URL = '/api/v1/annonces/'


def api(u):
    c = APIClient()
    c.force_authenticate(user=u)
    return c


@pytest.fixture
def ecole(db):
    inst = Institution.objects.create(acronyme='TST', nom='Institut Test', est_principale=True)
    Year.objects.create(annee=ANNEE, est_active=True)
    mpsi, mp = Niveau.objects.create(niveau='MPSI'), Niveau.objects.create(niveau='MP')
    NiveauIPGEI.objects.create(institution=inst, code='MPSI', libelle='MPSI', rang=1)
    NiveauIPGEI.objects.create(institution=inst, code='MP', libelle='MP', rang=2)

    def classe(niveau, groupe, annee=ANNEE):
        return Classe.objects.create(nom=f'{niveau.niveau} {groupe}'.strip(), institution=inst,
                                     annee_universitaire=annee, niveau=niveau, groupe=groupe)

    c = {'MPSI A': classe(mpsi, 'A'), 'MPSI B': classe(mpsi, 'B'), 'MP A': classe(mp, 'A'),
         'MP C': classe(mp, 'C'), 'accueil': classe(mpsi, ''), 'ancienne': classe(mpsi, 'A', '2025-2026')}

    n = [0]

    def eleve(cl, statut=InscriptionIPGEI.STATUT_ACTIF, compte=True):
        n[0] += 1
        u = UserFactory(username=f'eleve{n[0]}', role='etudiant') if compte else None
        e = Etudiant.objects.create(matricule=f'90{n[0]:04d}', nom=f'Eleve {n[0]}', user=u)
        InscriptionIPGEI.objects.create(etudiant=e, classe=cl, annee_universitaire=cl.annee_universitaire,
                                        statut=statut)
        return u

    e = {'mpsi_a': eleve(c['MPSI A']), 'mpsi_b': eleve(c['MPSI B']), 'mp_a': eleve(c['MP A']),
         'abandon': eleve(c['MPSI A'], statut=InscriptionIPGEI.STATUT_ABANDON),
         'accueil': eleve(c['accueil']), 'ancien': eleve(c['ancienne'])}
    eleve(c['MP A'], compte=False)
    return dict(c=c, e=e, admin=UserFactory(username='adm', role='admin', is_superuser=True))


def test_la_grille_niveaux_par_classes(ecole):
    r = api(ecole['admin']).get(f'{URL}grille/')

    assert r.status_code == 200, r.data
    assert [(l['label'], l['sub']) for l in r.data['lignes']] == [('MPSI', '1re année'), ('MP', '2e année')]
    assert [c['label'] for c in r.data['colonnes']] == ['Classe A', 'Classe B', 'Classe C']
    c = ecole['c']
    assert {k['cle']: k['nb'] for k in r.data['cases']} == {
        f"c{c['MPSI A'].pk}": 1, f"c{c['MPSI B'].pk}": 1, f"c{c['MP A'].pk}": 1, f"c{c['MP C'].pk}": 0}


def test_toutes_les_classes_MPSI(ecole):
    c, e = ecole['c'], ecole['e']
    r = api(ecole['admin']).post(URL, {
        'titre': 'Devoir surveillé', 'texte': 'Le [DATE].', 'resume': 'Toutes les classes MPSI',
        'cibles': [f"c{c['MPSI A'].pk}", f"c{c['MPSI B'].pk}"]}, format='json')

    assert r.status_code == 201, r.data
    assert set(Notification.objects.values_list('destinataire_id', flat=True)) == {e['mpsi_a'].pk, e['mpsi_b'].pk}
    n = Notification.objects.get(destinataire=e['mpsi_a'])
    assert (n.type, n.message) == ('annonce', 'Le [DATE].')
    assert Annonce.objects.get().nb_destinataires == 2


def test_une_classe_d_une_autre_annee_ou_d_accueil_ignoree(ecole):
    c = ecole['c']
    r = api(ecole['admin']).post(URL, {'titre': 'T', 'texte': 'x',
                                       'cibles': [f"c{c['ancienne'].pk}", f"c{c['accueil'].pk}"]}, format='json')
    assert r.status_code == 400
    assert not Notification.objects.exists()


def test_sans_le_droit_refuse(ecole):
    assert api(UserFactory(username='aa', role='AA')).get(f'{URL}grille/').status_code == 403
