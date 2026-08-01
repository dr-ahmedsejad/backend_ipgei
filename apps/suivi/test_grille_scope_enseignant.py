"""
Test — portail enseignant (LECTURE SEULE) : un enseignant doit voir SON
PROPRE emploi via /api/v1/suivi/pointages/grille/ même s'il n'a aucun
`managed_departements`, et de façon ergonomique :

  1. self-view : ses séances s'affichent (bypass du scope départemental) ;
  2. sécurité  : consulter un AUTRE prof reste scopé (donc vide) → pas de fuite ;
  3. défaut    : sans numero_semaine, la grille tombe sur SA dernière semaine
                 avec cours (pas la dernière semaine globale, souvent vide) ;
  4. semaines  : /semaines/?prof= ne liste que les semaines où il a cours.

Base SQLite en mémoire (siga.settings.test) — aucun contact gesafped26/siga.
"""
from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework import status
from rest_framework.test import APITestCase

from apps.parametres.models import Institution, Creneau, Jour
from apps.departement.models import Departement
from apps.prof.models import Prof, ProfTypeHistory
from apps.suivi.models import SuiviePointage, SuiviePointageDepartement

User = get_user_model()


class GrilleScopeEnseignantTest(APITestCase):
    @classmethod
    def setUpClass(cls):
        # create_model plante la transaction PG si la table existe deja (creee par
        # la fixture session de tests/conftest.py) : verifier l'existence AVANT
        # d'entrer dans le schema_editor est vendor-neutre (sqlite ET postgresql).
        existantes = set(connection.introspection.table_names())
        for model in (ProfTypeHistory, SuiviePointageDepartement):
            if model._meta.db_table not in existantes:
                with connection.schema_editor() as se:
                    se.create_model(model)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test', est_principale=True)
        cls.dept = Departement.objects.create(nom='Dept A', institution=cls.inst)
        cls.creneau = Creneau.objects.create(creneau='08:00-09:30', ordre=1)
        cls.jour = Jour.objects.create(jour='Lundi')

        # Enseignant SANS managed_departements (cas réel : 62/62 en prod)
        cls.ens = User.objects.create_user(
            username='ens_self', email='ens@test.local', password='Xk93!plqz72', role='enseignant',
        )
        cls.prof = Prof.objects.create(NNI=111111, nom='Prof Self', type='permanent', user=cls.ens)
        cls.other_prof = Prof.objects.create(NNI=222222, nom='Prof Autre', type='permanent')

        def _pointage(prof, semaine):
            sp = SuiviePointage.objects.create(
                prof=prof, institution=cls.inst, annee_universitaire='2025-2026',
                numero_semaine=semaine, type_semestre='I',
                creneau_fk=cls.creneau, jour_fk=cls.jour, commentaire='Fait',
            )
            sp.departements.add(cls.dept)   # dept que l'enseignant ne "gère" PAS
            return sp

        # Le prof a cours en semaines 1 et 3 (sa dernière = 3).
        _pointage(cls.prof, 1)
        _pointage(cls.prof, 3)
        # Un autre prof a cours en semaine 5 (= dernière semaine GLOBALE).
        _pointage(cls.other_prof, 5)

    def setUp(self):
        self.client.force_authenticate(user=self.ens)
        assert list(self.ens.managed_departements.values_list('id', flat=True)) == []

    def _grille(self, prof_pk, semaine=None):
        params = {'annee_universitaire': '2025-2026', 'prof': str(prof_pk), 'type_semestre': 'I'}
        if semaine is not None:
            params['numero_semaine'] = str(semaine)
        return self.client.get('/api/v1/suivi/pointages/grille/', params)

    @staticmethod
    def _count(resp):
        grille = resp.data['grille']
        return sum(len(cells) for jours in grille.values() for cells in jours.values())

    def test_self_view_retourne_ses_seances(self):
        r = self._grille(self.prof.pk, semaine=1)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(self._count(r), 1, "L'enseignant doit voir sa séance de la semaine 1.")

    def test_autre_prof_reste_scope(self):
        r = self._grille(self.other_prof.pk, semaine=5)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(self._count(r), 0, "Pas de fuite : aucune séance d'un autre prof.")

    def test_semaine_par_defaut_est_celle_du_prof(self):
        """Sans numero_semaine : on tombe sur la dernière semaine DU PROF (3),
        pas la dernière semaine globale (5, celle de l'autre prof)."""
        r = self._grille(self.prof.pk, semaine=None)
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(self._count(r), 1, "La grille par défaut ne doit pas être vide pour le prof.")

    def test_semaines_scopees_par_prof(self):
        r = self.client.get('/api/v1/suivi/pointages/semaines/', {
            'annee_universitaire': '2025-2026', 'type_semestre': 'I', 'prof': str(self.prof.pk),
        })
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data['semaines'], [1, 3], "Doit lister seulement les semaines du prof.")

    def test_semaines_sans_prof_inchange(self):
        """Rétrocompat : sans `prof`, toutes les semaines (admin EDT)."""
        r = self.client.get('/api/v1/suivi/pointages/semaines/', {
            'annee_universitaire': '2025-2026', 'type_semestre': 'I',
        })
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data['semaines'], [1, 3, 5], "Sans prof : toutes les semaines.")
