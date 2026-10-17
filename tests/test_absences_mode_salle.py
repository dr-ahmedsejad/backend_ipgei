"""
Lot 3 du module absences à l'IPGEI (17/10/2026) : le mode salle, lu sur le
pointage (`Suivie`) — porté de l'ISS (`apps/absence/mode_salle.py`).

Les écrans lisaient `/suivi/suivies/`, gardé par `suivi_saisie` et borné aux
groupes délégués : la scolarité, qui fait l'appel sans saisir le suivi, ne
voyait aucune séance. Trois lectures sous `abs_saisie` / voir :
`fiches-semaines`, `fiches-seances` (bilan d'appel par séance) et
`saisie-seance` (séance, liste d'appel, présences).
"""
import datetime as dt

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from tests._absences_ipgei import (  # noqa: F401  (fixtures)
    ANNEE, LUNDI, api, droit, inscrit, monde, seance, suivi,
)
from tests.factories.auth import UserFactory

URL_SEANCES  = '/api/v1/absences/presences/fiches-seances/'
URL_SEMAINES = '/api/v1/absences/presences/fiches-semaines/'
URL_SAISIE   = '/api/v1/absences/presences/saisie-seance/'


def presence(s, etu, statut, commentaire=''):
    from apps.absence.models import Presence
    return Presence.objects.create(suivi=s, etudiant=etu, statut=statut, commentaire=commentaire)


@pytest.fixture
def appelant(db):
    """Fait l'appel (`abs_saisie`) sans `suivi_saisie` ni groupe délégué :
    la scolarité de l'IPGEI, que les anciens écrans laissaient sans séance."""
    droit('scolarite', 'abs_saisie', 'voir')
    return UserFactory(username='u_appel', role='scolarite')


def seances(user, monde, classe='MPSI A', **params):
    dep = monde['sg'][classe] if classe in monde['sg'] else monde['classes'][classe]
    p = {'annee_universitaire': ANNEE, 'type_semestre': 'I', 'numero_semaine': 5,
         'departement': dep.pk, **params}
    r = api(user).get(URL_SEANCES, {k: v for k, v in p.items() if v is not None})
    assert r.status_code == 200, r.data
    return r.data


# ── Le bilan d'appel ─────────────────────────────────────────────────────────

class TestBilan:

    def test_le_bilan_compte_chaque_statut(self, monde, appelant):
        s = suivi(monde, seance(monde))
        for i, statut in enumerate((0, 0, 0, 1, 1, 2, 3)):
            presence(s, inscrit(monde, '50%d' % i), statut)
        [l] = seances(appelant, monde)
        assert l['appel'] == {'saisi': True, 'presents': 3, 'absents': 2,
                              'sanctions': 1, 'justifiees': 1}

    def test_une_seance_sans_appel_n_est_pas_tous_presents(self, monde, appelant):
        suivi(monde, seance(monde))
        [l] = seances(appelant, monde)
        assert l['appel'] == {'saisi': False, 'presents': 0, 'absents': 0,
                              'sanctions': 0, 'justifiees': 0}

    def test_les_libelles_de_la_seance(self, monde, appelant):
        s = suivi(monde, seance(monde, matiere='PHYS', type_='TD', prof='SIDI', salle='102'))
        [l] = seances(appelant, monde)
        assert (l['id'], l['jour_label'], l['date_suivie'], l['creneau_label'],
                l['type_seance_label'], l['prof_nom'], l['em_intitule'], l['salle_nom'],
                l['departement'], l['sous_groupe']) == (
            s.pk, 'Lundi', LUNDI.isoformat(), '08h00-09h30', 'TD', 'SIDI', 'Phys', '102',
            monde['classes']['MPSI A'].pk, False)


# ── Le dédoublonnage ─────────────────────────────────────────────────────────

class TestDoublons:

    def test_deux_lignes_identiques_font_une_seance(self, monde, appelant):
        r = seance(monde)
        a = suivi(monde, r)
        suivi(monde, r)
        assert [l['id'] for l in seances(appelant, monde)] == [a.pk]

    def test_le_double_qui_porte_l_appel_est_celui_qu_on_montre(self, monde, appelant):
        r = seance(monde)
        suivi(monde, r)
        b = suivi(monde, r)
        presence(b, inscrit(monde, '600'), 1)
        [l] = seances(appelant, monde)
        assert (l['id'], l['appel']['absents']) == (b.pk, 1)

    def test_une_autre_matiere_au_meme_creneau_est_une_autre_seance(self, monde, appelant):
        r = seance(monde)
        suivi(monde, r)
        monde['matieres']['PHYS'].refresh_from_db()
        suivi(monde, r, em_id=monde['matieres']['PHYS'].em_miroir_id)
        assert len(seances(appelant, monde)) == 2


# ── Ce que la semaine d'une classe contient ──────────────────────────────────

class TestPerimetre:

    def test_la_classe_montre_ses_tp(self, monde, appelant):
        """Choisir « MPSI A » donne aussi les TP de A1 et A2 — introuvables
        jusqu'ici : la grille filtrait sur le département de la classe."""
        cm = suivi(monde, seance(monde))
        tp = suivi(monde, seance(monde, sous_groupe='A1', matiere='INFO', type_='TP',
                                 creneau='09h45-11h15'))
        suivi(monde, seance(monde, classe='MPSI B', creneau='11h30-13h00'))
        lignes = seances(appelant, monde)
        assert [(l['id'], l['sous_groupe']) for l in lignes] == [(cm.pk, False), (tp.pk, True)]
        assert lignes[1]['groupe'] == 'A1'
        # Le sous-groupe seul : ses séances à lui.
        assert [l['id'] for l in seances(appelant, monde, classe='A1')] == [tp.pk]

    def test_seulement_la_semaine_et_la_periode_demandees(self, monde, appelant):
        r = seance(monde)
        ici = suivi(monde, r)
        suivi(monde, r, numero_semaine=6)
        suivi(monde, r, type_semestre='P')
        assert [l['id'] for l in seances(appelant, monde)] == [ici.pk]

    def test_une_ligne_sans_type_n_a_pas_d_appel_sans_enseignant_si(self, monde, appelant):
        """Une épreuve sans surveillant désigné se pointe : règle de l'IPGEI."""
        r = seance(monde, prof=None, matiere='PHYS', type_='DS')
        sans_prof = suivi(monde, r)
        suivi(monde, seance(monde, creneau='09h45-11h15'), type_seance_fk=None)
        assert [l['id'] for l in seances(appelant, monde)] == [sans_prof.pk]

    def test_une_autre_institution_ne_se_voit_pas(self, monde, appelant):
        from apps.parametres.models import Institution
        autre = Institution.objects.create(acronyme='AUTRE', nom='Autre', est_principale=False)
        suivi(monde, seance(monde), institution=autre)
        assert seances(appelant, monde) == []

    def test_l_ordre_est_celui_de_la_semaine(self, monde, appelant):
        mardi = suivi(monde, seance(monde, jour='Mardi'))
        tard = suivi(monde, seance(monde, creneau='09h45-11h15'))
        tot = suivi(monde, seance(monde))
        assert [l['id'] for l in seances(appelant, monde)] == [tot.pk, tard.pk, mardi.pk]

    def test_parametres_invalides(self, monde, appelant):
        r = api(appelant).get(URL_SEANCES, {'annee_universitaire': ANNEE, 'numero_semaine': 'x'})
        assert r.status_code == 400
        r = api(appelant).get(URL_SEANCES, {'numero_semaine': 5})
        assert r.status_code == 400


# ── Les semaines ─────────────────────────────────────────────────────────────

class TestSemaines:

    def test_les_semaines_generees_avec_leurs_dates(self, monde, appelant):
        suivi(monde, seance(monde))
        r = api(appelant).get(URL_SEMAINES, {'annee_universitaire': ANNEE, 'type_semestre': 'I'})
        assert r.status_code == 200, r.data
        assert r.data['semaines'] == [5]
        # Le calendrier du décor : lundi → samedi.
        assert r.data['dates']['5'] == {'debut': LUNDI.isoformat(),
                                        'fin': (LUNDI + dt.timedelta(days=5)).isoformat()}
        assert r.data['courante'] == 5

    def test_la_semaine_proposee(self, monde):
        from apps.absence.mode_salle import semaines_generees
        r = seance(monde)
        suivi(monde, r)
        suivi(monde, r, numero_semaine=9, date_suivie=LUNDI + dt.timedelta(days=28))
        assert semaines_generees(ANNEE, 'I', LUNDI + dt.timedelta(days=29))['courante'] == 9
        # Entre deux semaines : la dernière commencée.
        assert semaines_generees(ANNEE, 'I', LUNDI + dt.timedelta(days=10))['courante'] == 5
        # Avant tout : la première.
        assert semaines_generees(ANNEE, 'I', LUNDI - dt.timedelta(days=30))['courante'] == 5

    def test_aucune_semaine(self, monde, appelant):
        r = api(appelant).get(URL_SEMAINES, {'annee_universitaire': ANNEE, 'type_semestre': 'I'})
        assert r.data == {'semaines': [], 'courante': None, 'dates': {}}


# ── L'appel d'une séance ─────────────────────────────────────────────────────

class TestSaisieSeance:

    def test_un_tp_appelle_son_sous_groupe(self, monde, appelant):
        a1 = inscrit(monde, '070', sous_groupe='A1')
        inscrit(monde, '071', sous_groupe='A2')
        aud = inscrit(monde, '072', sous_groupe='A1', auditeur_libre=True)
        inscrit(monde, '073', sous_groupe='A1', statut='abandon')
        s = suivi(monde, seance(monde, sous_groupe='A1', matiere='INFO', type_='TP'))

        r = api(appelant).get(URL_SAISIE, {'seance': s.pk})
        assert r.status_code == 200, r.data
        assert [(e['id'], e['mention']) for e in r.data['etudiants']] == [
            (a1.id, ''), (aud.id, 'auditeur libre')]
        assert r.data['source'] == 'sous_groupe'
        assert r.data['refus'] == '' and r.data['liste_non_verifiee'] is False
        assert r.data['seance']['id'] == s.pk and r.data['seance']['sous_groupe'] is True
        # « Séance suivante » enchaîne sur la journée de la classe.
        assert r.data['seance']['classe'] == monde['classes']['MPSI A'].pk

    def test_qui_est_deja_pointe_reste_a_l_ecran(self, monde, appelant):
        """Un abandon déclaré après coup sort de la liste — pas son pointage."""
        s = suivi(monde, seance(monde))
        parti = inscrit(monde, '079', statut='abandon')
        presence(s, parti, 1)
        r = api(appelant).get(URL_SAISIE, {'seance': s.pk})
        assert [(e['id'], e['mention']) for e in r.data['etudiants']] == [(parti.id, 'hors liste')]

    def test_les_presences_deja_saisies(self, monde, appelant):
        s = suivi(monde, seance(monde))
        a, b = inscrit(monde, '080'), inscrit(monde, '081')
        presence(s, a, 1, 'retard')
        presence(s, b, 3)
        r = api(appelant).get(URL_SAISIE, {'seance': s.pk})
        assert r.data['presences'] == {
            str(a.id): {'statut': 1, 'commentaire': 'retard', 'justificatif': False},
            str(b.id): {'statut': 3, 'commentaire': '', 'justificatif': False},
        }
        assert r.data['seance']['appel']['saisi'] is True

    def test_une_seance_sans_groupe_n_a_pas_de_liste(self, monde, appelant):
        s = suivi(monde, seance(monde), departement_id=None)
        r = api(appelant).get(URL_SAISIE, {'seance': s.pk})
        assert r.status_code == 200
        assert r.data['refus'] and r.data['etudiants'] == []

    def test_introuvable_ou_hors_perimetre(self, monde, appelant):
        from apps.parametres.models import Institution
        autre = Institution.objects.create(acronyme='AUTRE', nom='Autre', est_principale=False)
        ailleurs = suivi(monde, seance(monde), institution=autre)
        r = api(appelant).get(URL_SAISIE, {'seance': ailleurs.pk})
        assert r.status_code == 404 and 'introuvable' in str(r.data)
        assert api(appelant).get(URL_SAISIE, {'seance': 999999}).status_code == 404
        assert api(appelant).get(URL_SAISIE, {'seance': 'abc'}).status_code == 400


# ── Les droits, et rien d'écrit ──────────────────────────────────────────────

class TestDroitsEtLecture:

    def test_sans_abs_saisie_c_est_403(self, monde):
        """Le DE a `suivi_saisie`, pas `abs_saisie` (dans ce décor)."""
        droit('DE', 'suivi_saisie', 'voir')
        s = suivi(monde, seance(monde))
        client = api(UserFactory(username='u_de', role='DE'))
        assert client.get(URL_SEANCES, {'annee_universitaire': ANNEE, 'numero_semaine': 5}).status_code == 403
        assert client.get(URL_SEMAINES, {'annee_universitaire': ANNEE}).status_code == 403
        assert client.get(URL_SAISIE, {'seance': s.pk}).status_code == 403

    def test_aucune_ecriture(self, monde, appelant):
        s = suivi(monde, seance(monde))
        presence(s, inscrit(monde, '090'), 1)
        client = api(appelant)
        with CaptureQueriesContext(connection) as requetes:
            client.get(URL_SEANCES, {'annee_universitaire': ANNEE, 'type_semestre': 'I',
                                     'numero_semaine': 5,
                                     'departement': monde['classes']['MPSI A'].pk})
            client.get(URL_SEMAINES, {'annee_universitaire': ANNEE, 'type_semestre': 'I'})
            client.get(URL_SAISIE, {'seance': s.pk})
        # Le journal d'audit peut écrire ; ni les présences ni le suivi.
        ecritures = [q['sql'] for q in requetes.captured_queries
                     if q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
                     and ('absence_' in q['sql'] or 'suivi_' in q['sql'])]
        assert ecritures == []

    def test_requetes_en_nombre_constant(self, monde, appelant):
        """Une requête pour les bilans de toute la semaine, pas une par séance."""
        def mesure():
            from django.core.cache import cache
            cache.clear()
            with CaptureQueriesContext(connection) as q:
                seances(appelant, monde)
            return len(q.captured_queries)
        suivi(monde, seance(monde))
        une = mesure()
        for creneau in ('09h45-11h15', '11h30-13h00', '15h10-16h40'):
            suivi(monde, seance(monde, creneau=creneau))
        suivi(monde, seance(monde, sous_groupe='A1', matiere='INFO', type_='TP', jour='Mardi'))
        assert mesure() == une
