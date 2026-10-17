"""
Lot 2 du module absences à l'IPGEI (17/10/2026) : la liste d'appel.

Une séance appelle les inscriptions IPGEI actives de l'année — de la CLASSE
pour un cours, du SOUS-GROUPE pour un TP —, auditeurs compris, abandons et
réorientations écartés (`apps/ipgei/liste_appel.py`). Le socle appelait par
`Etudiant.departement`, la classe : la liste d'un TP était vide.

Branchée sur : `presences/liste-appel/`, les fiches PDF, la feuille
d'émargement (`seances/<id>/absences/`, GET et POST) et l'app enseignant.
Plus : une absence retirée de la feuille d'émargement quitte le registre du
socle (`miroir.retirer_reflets`).
"""
import pytest

from tests._absences_ipgei import (  # noqa: F401  (fixtures)
    ANNEE, agent_de_saisie, api, droit, inscrit, monde, seance, suivi, superutilisateur,
)
from tests.factories.auth import UserFactory

URL = '/api/v1/absences/presences/liste-appel/'


@pytest.fixture
def promo(monde):
    """MPSI A : deux étudiants en A1, un en A2, un auditeur libre en A1, un
    abandon et un réorienté ; un étudiant de MPSI B."""
    return {
        'a1': inscrit(monde, '101', sous_groupe='A1'),
        'a1bis': inscrit(monde, '102', sous_groupe='A1'),
        'a2': inscrit(monde, '103', sous_groupe='A2'),
        'auditeur': inscrit(monde, '104', sous_groupe='A1', auditeur_libre=True),
        'abandon': inscrit(monde, '105', sous_groupe='A1', statut='abandon'),
        'reoriente': inscrit(monde, '106', sous_groupe='A2', statut='reoriente'),
        'autre': inscrit(monde, '201', classe='MPSI B'),
    }


def ids(liste):
    return [e.id for e in liste]


# ── La règle ─────────────────────────────────────────────────────────────────

class TestLaRegle:

    def test_une_seance_de_classe_appelle_la_classe(self, monde, promo):
        from apps.ipgei.liste_appel import SOURCE_CLASSE, liste_appel
        r = liste_appel(monde['classes']['MPSI A'].pk, ANNEE)
        assert r['source'] == SOURCE_CLASSE
        assert ids(r['etudiants']) == [promo[k].id for k in ('a1', 'a1bis', 'a2', 'auditeur')]

    def test_un_tp_appelle_son_sous_groupe(self, monde, promo):
        from apps.ipgei.liste_appel import SOURCE_SOUS_GROUPE, liste_appel
        r = liste_appel(monde['sg']['A1'].pk, ANNEE)
        assert r['source'] == SOURCE_SOUS_GROUPE
        assert ids(r['etudiants']) == [promo[k].id for k in ('a1', 'a1bis', 'auditeur')]
        assert ids(liste_appel(monde['sg']['A2'].pk, ANNEE)['etudiants']) == [promo['a2'].id]

    def test_l_auditeur_est_appele_et_nomme(self, monde, promo):
        from apps.ipgei.liste_appel import liste_appel
        mentions = {e.id: e.mention for e in liste_appel(monde['sg']['A1'].pk, ANNEE)['etudiants']}
        assert mentions[promo['auditeur'].id] == 'auditeur libre'
        assert mentions[promo['a1'].id] == ''

    def test_une_autre_annee_n_est_pas_appelee(self, monde, promo):
        from apps.ipgei.liste_appel import liste_appel
        assert liste_appel(monde['classes']['MPSI A'].pk, '2024-2025')['etudiants'] == []

    def test_l_ordre_est_celui_des_matricules_en_nombre(self, monde):
        from apps.ipgei.liste_appel import liste_appel
        grand, petit = inscrit(monde, '10000'), inscrit(monde, '9999')
        assert ids(liste_appel(monde['classes']['MPSI A'].pk, ANNEE)['etudiants']) == [petit.id, grand.id]

    def test_une_classe_et_ses_sous_groupes(self, monde):
        from apps.ipgei.liste_appel import unites
        a = monde['classes']['MPSI A']
        assert sorted(unites(a.pk)) == sorted([a.pk, monde['sg']['A1'].pk, monde['sg']['A2'].pk])
        assert unites(monde['sg']['A1'].pk) == [monde['sg']['A1'].pk]


# ── presences/liste-appel/ ───────────────────────────────────────────────────

class TestLaRoute:

    def test_la_liste_d_un_tp(self, monde, promo):
        r = api(agent_de_saisie()).get(URL, {'departement': monde['sg']['A1'].pk,
                                             'annee_universitaire': ANNEE})
        assert r.status_code == 200, r.data
        assert [e['matricule'] for e in r.data['etudiants']] == ['101', '102', '104']
        assert r.data['source'] == 'sous_groupe' and r.data['liste_non_verifiee'] is False
        assert set(r.data['etudiants'][0]) == {'id', 'matricule', 'nom', 'genre', 'mention'}

    def test_parametres_requis(self, monde):
        client = api(agent_de_saisie())
        assert client.get(URL, {'annee_universitaire': ANNEE}).status_code == 400
        assert client.get(URL, {'departement': 'x', 'annee_universitaire': ANNEE}).status_code == 400

    def test_sous_abs_saisie(self, monde):
        r = api(UserFactory(username='u_sans', role='enseignant')).get(
            URL, {'departement': monde['sg']['A1'].pk, 'annee_universitaire': ANNEE})
        assert r.status_code == 403


# ── Les fiches PDF ───────────────────────────────────────────────────────────

def test_la_fiche_d_un_tp_porte_son_sous_groupe(monde, promo, monkeypatch):
    """Choisir la classe donne aussi les fiches de ses TP, chacune avec SA
    liste ; la fiche d'un TP sortait vide."""
    from apps.absence import views
    vues = {}

    def rendu(gabarit, contexte):
        vues['fiches'] = contexte['fiches']
        return '<html></html>'
    monkeypatch.setattr(views, 'render_to_string', rendu)
    monkeypatch.setattr(views.PresenceViewSet, '_make_pdf', staticmethod(lambda html: b'%PDF'))

    suivi(monde, seance(monde, matiere='MATH', type_='CM'))
    suivi(monde, seance(monde, sous_groupe='A1', matiere='INFO', type_='TP', creneau='09h45-11h15'))
    r = api(superutilisateur()).get('/api/v1/absences/presences/fiches-pdf/', {
        'annee_universitaire': ANNEE, 'numero_semaine': 5,
        'departement': monde['classes']['MPSI A'].pk})
    assert r.status_code == 200
    par_type = {f['type_seance']: [e['matricule'] for e in f['etudiants']] for f in vues['fiches']}
    assert par_type == {'CM': ['101', '102', '103', '104'], 'TP': ['101', '102', '104']}


# ── La feuille d'émargement ──────────────────────────────────────────────────

def _feuille(monde, client, s):
    r = client.get(f'/api/v1/ipgei/seances/{s.pk}/absences/')
    assert r.status_code == 200, r.data
    return r


class TestLaFeuilleDEmargement:

    def test_le_tp_refuse_un_etudiant_de_l_autre_sous_groupe(self, monde, promo):
        from django.core.exceptions import ValidationError

        from apps.ipgei import services
        from apps.ipgei.models import InscriptionIPGEI
        tp = seance(monde, sous_groupe='A1', matiere='INFO', type_='TP')
        a1 = InscriptionIPGEI.objects.get(etudiant=promo['a1'])
        a2 = InscriptionIPGEI.objects.get(etudiant=promo['a2'])
        assert services.saisir_absences(tp, [{'inscription': a1.pk}])['enregistrees'] == 1
        with pytest.raises(ValidationError, match='sous-groupe'):
            services.saisir_absences(tp, [{'inscription': a2.pk}])

    def test_la_feuille_ecarte_l_abandon_sauf_s_il_y_est_deja(self, monde, promo):
        from apps.ipgei.models import AbsenceSeance, InscriptionIPGEI
        s = seance(monde, matiere='PHYS', type_='DS')
        client = api(superutilisateur())
        noms = [i['etudiant'] for i in _feuille(monde, client, s).data['classe']]
        assert promo['abandon'].id not in noms and promo['a1'].id in noms
        # Absent AVANT de déclarer l'abandon : il reste sur la feuille.
        AbsenceSeance.objects.create(seance=s, inscription=InscriptionIPGEI.objects.get(
            etudiant=promo['abandon']))
        noms = [i['etudiant'] for i in _feuille(monde, client, s).data['classe']]
        assert promo['abandon'].id in noms


# ── Le reflet : une absence retirée quitte le registre ──────────────────────

class TestRetirerLeReflet:

    @pytest.fixture
    def epreuve(self, monde, promo):
        from apps.ipgei.models import InscriptionIPGEI
        ep = seance(monde, matiere='PHYS', type_='DS')
        s = suivi(monde, ep)
        insc = {k: InscriptionIPGEI.objects.get(etudiant=promo[k]) for k in ('a1', 'a1bis', 'a2')}
        return ep, s, insc

    def test_une_absence_retiree_de_la_feuille_quitte_le_registre(self, epreuve):
        from apps.absence.models import Presence
        from apps.ipgei import services
        ep, s, insc = epreuve
        services.saisir_absences(ep, [{'inscription': insc['a1'].pk},
                                      {'inscription': insc['a2'].pk}])
        assert Presence.objects.filter(suivi=s).count() == 2
        services.saisir_absences(ep, [{'inscription': insc['a2'].pk}])
        assert list(Presence.objects.filter(suivi=s).values_list('etudiant_id', flat=True)) == [
            insc['a2'].etudiant_id]

    def test_une_absence_devenue_retard_quitte_le_registre(self, epreuve):
        """Le socle n'a pas de « retard » : l'étudiant était là."""
        from apps.absence.models import Presence
        from apps.ipgei import services
        ep, s, insc = epreuve
        services.saisir_absences(ep, [{'inscription': insc['a1'].pk, 'justificatif': 'bus'}])
        assert Presence.objects.get(suivi=s).commentaire == 'bus'
        services.saisir_absences(ep, [{'inscription': insc['a1'].pk, 'statut': 'retard'}])
        assert not Presence.objects.filter(suivi=s).exists()

    def test_une_justifiee_ou_une_ligne_retouchee_reste(self, epreuve):
        """Rien ne distingue une ligne retouchée dans le module ABS d'une
        saisie de la scolarité : dans le doute, on n'efface pas."""
        from apps.absence.models import Presence
        from apps.ipgei import services
        ep, s, insc = epreuve
        services.saisir_absences(ep, [{'inscription': i.pk} for i in insc.values()])
        Presence.objects.filter(suivi=s, etudiant_id=insc['a1'].etudiant_id).update(statut=3)
        Presence.objects.filter(suivi=s, etudiant_id=insc['a1bis'].etudiant_id).update(
            commentaire='vu par la scolarité')
        services.saisir_absences(ep, [])
        assert set(Presence.objects.filter(suivi=s).values_list('etudiant_id', flat=True)) == {
            insc['a1'].etudiant_id, insc['a1bis'].etudiant_id}

    def test_une_presence_saisie_hors_emargement_reste(self, epreuve):
        from apps.absence.models import Presence
        from apps.ipgei import services
        ep, s, insc = epreuve
        # Pointée « Absent » dans la grille, sans passer par la feuille.
        Presence.objects.create(suivi=s, etudiant_id=insc['a2'].etudiant_id, statut=1,
                                commentaire='')
        services.saisir_absences(ep, [{'inscription': insc['a1'].pk}])
        services.saisir_absences(ep, [])
        assert list(Presence.objects.filter(suivi=s).values_list('etudiant_id', flat=True)) == [
            insc['a2'].etudiant_id]


# ── L'app enseignant lit la même liste ──────────────────────────────────────

def test_l_app_enseignant_lit_la_meme_liste(monde, promo):
    from types import SimpleNamespace

    from apps.absence.enseignant import _groupe
    lignes, source = _groupe(monde['sg']['A2'], SimpleNamespace(annee_universitaire=ANNEE))
    assert source == 'classe'
    assert [l['matricule'] for l in lignes] == ['103']          # le réorienté écarté
