"""
GET /portail/enseignant/accueil/ : l'accueil enseignant en une requête.
Chaque partie = la réponse EXACTE de son adresse habituelle (même vue, mêmes
droits). Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework.test import APITestCase

from apps.departement.models import Departement
from apps.parametres.models import Creneau, Institution, Jour
from apps.portail.accueil_enseignant import semaine_du_jour
from apps.prof.models import Prof, ProfTypeHistory
from apps.suivi.models import SuiviePointage, SuiviePointageDepartement

User = get_user_model()
ANNEE = '2025-2026'


class SemaineDuJourTest(APITestCase):
    dates = {1: {'debut': '06/10/2025', 'fin': '11/10/2025'}, 2: {'debut': '13/10/2025', 'fin': '18/10/2025'}}

    def test_regle_de_l_app(self):
        self.assertEqual(semaine_du_jour([1, 2], self.dates, date(2025, 10, 8)), 1)   # contient le jour
        self.assertEqual(semaine_du_jour([1, 2], self.dates, date(2025, 10, 12)), 1)  # dimanche : dernière commencée
        self.assertEqual(semaine_du_jour([1, 2], self.dates, date(2025, 9, 1)), 2)    # rien de commencé : la dernière
        self.assertIsNone(semaine_du_jour([], self.dates))


class AccueilEnseignantTest(APITestCase):
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
        dept = Departement.objects.create(nom='Dept A', institution=cls.inst)
        creneau = Creneau.objects.create(creneau='08:00-09:30', ordre=1)
        lundi = Jour.objects.create(jour='Lundi')
        cls.ens = User.objects.create_user(username='ens_acc', email='e@t.local', password='Xk93!plqz72',
                                           role='enseignant')
        cls.prof = Prof.objects.create(NNI=444444, nom='Prof Accueil', type='vacataire', user=cls.ens)
        for semaine in (1, 2, 3):
            sp = SuiviePointage.objects.create(prof=cls.prof, institution=cls.inst, annee_universitaire=ANNEE,
                                               numero_semaine=semaine, type_semestre='I', creneau_fk=creneau,
                                               jour_fk=lundi, commentaire='Fait')
            sp.departements.add(dept)

    def setUp(self):
        self.client.force_authenticate(user=self.ens)
        self.ctx = {'annee_universitaire': ANNEE, 'type_semestre': 'I', 'prof': str(self.prof.pk)}

    def test_chaque_partie_est_la_reponse_de_son_adresse(self):
        r = self.client.get('/api/v1/portail/enseignant/accueil/', self.ctx)
        self.assertEqual(r.status_code, 200, r.data)
        semaines = self.client.get('/api/v1/suivi/pointages/semaines/', self.ctx)
        self.assertEqual(r.data['semaines'], semaines.data)
        n = r.data['numero']
        self.assertEqual(n, 3)    # sans dates : la dernière (règle de l'app)
        grille = self.client.get('/api/v1/suivi/pointages/grille/', {**self.ctx, 'numero_semaine': n})
        self.assertEqual(r.data['grille'], grille.data)
        self.assertIsNone(r.data['numero_suivant'])
        self.assertEqual(r.data['non_lues'], self.client.get('/api/v1/notifications/unread-count/').data['count'])
        recl = self.client.get('/api/v1/reclamations/pour-enseignant/', {'annee_universitaire': ANNEE})
        self.assertEqual(r.data['reclamations'], recl.data if recl.status_code == 200 else None)

    def test_reserve_aux_enseignants(self):
        etu = User.objects.create_user(username='etu_acc', email='u@t.local', password='Xk93!plqz72',
                                       role='etudiant')
        self.client.force_authenticate(user=etu)
        self.assertEqual(self.client.get('/api/v1/portail/enseignant/accueil/', self.ctx).status_code, 403)
