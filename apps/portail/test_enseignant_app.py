"""
Côté enseignant de l'app mobile — mêmes adresses et mêmes réponses qu'à l'ISS :
profil (portail/enseignant/profil/), liste des étudiants d'une séance
(absences/enseignant/liste/), détail des séances non faites
(avancement/suivi-pointage-prof/?tous=1), état de la contestation d'une séance
dans la grille (suivi/pointages/grille/). Base SQLite en mémoire.
"""
import datetime as dt

from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework.test import APITestCase

from apps.parametres.models import Creneau, Institution, Jour
from apps.prof.models import Prof, ProfTypeHistory
from apps.suivi.models import SuiviePointage, SuiviePointageDepartement

User = get_user_model()
ANNEE = '2025-2026'
# IPGEI : les étudiants de la CLASSE, par l'inscription IPGEI de l'année.
SOURCE = 'classe'


def _groupe(inst):
    from apps.ipgei.models import Classe
    from apps.parametres.models import Niveau
    return Classe.objects.create(nom='MP A', institution=inst, annee_universitaire=ANNEE,
                                 niveau=Niveau.objects.create(niveau='MP'), groupe='A')


def _inscrire(classe, matricules):
    from apps.absence.models import Etudiant
    from apps.ipgei.models import InscriptionIPGEI
    for m in matricules:
        e = Etudiant.objects.create(matricule=m, nom=f'Etudiant {m}', nom_fr=f'Etudiant {m}')
        InscriptionIPGEI.objects.create(etudiant=e, classe=classe, annee_universitaire=ANNEE)
    # Une inscription inactive (départ) n'est pas listée.
    e = Etudiant.objects.create(matricule='77', nom='Parti', nom_fr='Parti')
    InscriptionIPGEI.objects.create(etudiant=e, classe=classe, annee_universitaire=ANNEE, actif=False)


class EnseignantAppTest(APITestCase):
    @classmethod
    def setUpClass(cls):
        existantes = set(connection.introspection.table_names())
        for model in (ProfTypeHistory, SuiviePointageDepartement):
            if model._meta.db_table not in existantes:
                with connection.schema_editor() as se:
                    se.create_model(model)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test', est_principale=True)
        cls.groupe = _groupe(cls.inst)
        cls.creneau = Creneau.objects.create(creneau='08:00-09:30', ordre=1)
        cls.lundi = Jour.objects.create(jour='Lundi')
        cls.ens = User.objects.create_user(username='ens_app', email='e@t.local', password='Xk93!plqz72',
                                           role='enseignant')
        cls.prof = Prof.objects.create(NNI=555555, nom='Prof App', type='vacataire', user=cls.ens)
        cls.autre = Prof.objects.create(NNI=666666, nom='Autre Prof', type='vacataire')

    def setUp(self):
        self.client.force_authenticate(user=self.ens)

    def _pointage(self, commentaire='Fait', jour=None, prof=None, semaine=1):
        sp = SuiviePointage.objects.create(
            prof=prof or self.prof, institution=self.inst, annee_universitaire=ANNEE,
            numero_semaine=semaine, type_semestre='I', creneau_fk=self.creneau, jour_fk=self.lundi,
            commentaire=commentaire, date_suivie=jour or dt.date.today() - dt.timedelta(days=3))
        sp.departements.add(self.groupe)
        return sp

    # ── Profil ──────────────────────────────────────────────────────────────
    def test_profil_lu_et_coordonnees_corrigees(self):
        url = '/api/v1/portail/enseignant/profil/'
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data['nom'], r.data['type']), ('Prof App', 'vacataire'))
        self.assertEqual(self.client.patch(url, {'telephone': '12'}, format='json').status_code, 400)
        r = self.client.patch(url, {'telephone': '22 33 44 55', 'email': 'prof@t.local'}, format='json')
        self.assertEqual((r.data['telephone'], r.data['email']), ('22334455', 'prof@t.local'))

    # ── Liste des étudiants d'une séance ────────────────────────────────────
    def test_liste_de_sa_seance_par_matricule_croissant(self):
        _inscrire(self.groupe, ['10', '9', '100'])
        r = self.client.get('/api/v1/absences/enseignant/liste/', {'pointage': self._pointage().pk})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual([e['matricule'] for e in r.data['groupes'][0]['etudiants']], ['9', '10', '100'])
        self.assertEqual(r.data['total'], 3)
        self.assertEqual(r.data['groupes'][0]['source'], SOURCE)

    def test_liste_pas_la_seance_d_un_autre(self):
        sp = self._pointage(prof=self.autre)
        self.assertEqual(self.client.get('/api/v1/absences/enseignant/liste/', {'pointage': sp.pk}).status_code, 404)
        self.assertEqual(self.client.get('/api/v1/absences/enseignant/liste/').status_code, 400)

    # ── Détail des séances : ?tous=1 ────────────────────────────────────────
    def test_tous_ajoute_les_seances_non_faites_passees_sans_les_payer(self):
        passe = dt.date.today() - dt.timedelta(days=3)
        self._pointage('Fait', passe)
        self._pointage('Non fait', passe - dt.timedelta(days=1))
        self._pointage('Reporté', passe - dt.timedelta(days=2))
        self._pointage('Non fait', dt.date.today() + dt.timedelta(days=5))   # à venir : absente
        url = '/api/v1/avancement/suivi-pointage-prof/'
        sans = self.client.get(url, {'annee_universitaire': ANNEE}).data
        avec = self.client.get(url, {'annee_universitaire': ANNEE, 'tous': 1}).data
        self.assertEqual([r['statut'] for r in sans['rows']], ['Fait'])
        self.assertEqual(sorted(r['statut'] for r in avec['rows']), ['Fait', 'Non fait', 'Reporté'])
        self.assertTrue(all(r['id'] and r['creneau'] == '08:00-09:30' for r in avec['rows']))
        # Seule la séance faite est payée : mêmes totaux avec ou sans `tous`.
        self.assertEqual((avec['total_heures'], avec['total_montant']),
                         (sans['total_heures'], sans['total_montant']))

    # ── Grille : état de la contestation déposée en ligne ───────────────────
    def test_la_grille_montre_l_etat_de_la_derniere_contestation(self):
        from apps.reclamations.models import ReclamationSeance
        sp = self._pointage('Non fait', semaine=3)

        def etat():
            g = self.client.get('/api/v1/suivi/pointages/grille/', {
                'annee_universitaire': ANNEE, 'prof': self.prof.pk, 'numero_semaine': 3}).data
            return [e['reclamation_statut'] for crs in g['grille'].values() for l in crs.values() for e in l]

        self.assertEqual(etat(), [''])
        cle = dict(prof_id=self.prof.pk, annee_universitaire=ANNEE, type_semestre='I', numero_semaine=3,
                   em_id=None, jour='Lundi', creneau='08:00-09:30')
        # Identifiant de pointage périmé (une publication l'a changé) : la clé
        # naturelle suffit.
        ReclamationSeance.objects.create(**cle, pointage_id=sp.pk + 999, motif='Faite', statut='rejetee')
        self.assertEqual(etat(), ['rejetee'])
        ReclamationSeance.objects.create(**cle, pointage_id=sp.pk, motif='Encore', statut='en_attente')
        self.assertEqual(etat(), ['en_attente'])
