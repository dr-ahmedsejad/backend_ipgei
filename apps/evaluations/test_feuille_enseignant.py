"""
Test — page notes du portail enseignant.

Couvre deux choses :

A. CORRECTION DE LA LISTE D'ÉTUDIANTS (feuille) : un EM réutilisé d'une année
   sur l'autre ne doit PAS faire remonter les étudiants des promotions
   précédentes, ni dupliquer un redoublant (1 ligne ancienne année + 1 ligne
   dette année courante). La feuille est filtrée par l'année de la session.

B. DÉBLOCAGE + SCOPE ENSEIGNANT : un enseignant (sans module RBAC eval_saisie)
   peut lister les sessions, consulter la feuille et saisir les notes — mais
   UNIQUEMENT pour les EMs qu'il enseigne (ses pointages). Tout EM étranger = 403.

Base SQLite en mémoire (siga.settings.test) — aucun contact gesafped26/siga.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework import status
from rest_framework.test import APITestCase

from apps.parametres.models import Institution, Year, Niveau, Semestre
from apps.scolarite.models import Filiere
from apps.departement.models import Departement
from apps.absence.models import Etudiant
from apps.em.models import EM
from apps.prof.models import Prof, ProfTypeHistory
from apps.suivi.models import SuiviePointage
from apps.inscriptions.models import (
    InscriptionAdministrative, InscriptionPedagogique, InscriptionElement,
)
from apps.evaluations.models import SessionEvaluation, Note

User = get_user_model()


class FeuilleEnseignantTest(APITestCase):
    @classmethod
    def setUpClass(cls):
        # create_model plante la transaction PG si la table existe deja (creee par
        # la fixture session de tests/conftest.py) : verifier l'existence AVANT
        # d'entrer dans le schema_editor est vendor-neutre (sqlite ET postgresql).
        if ProfTypeHistory._meta.db_table not in connection.introspection.table_names():
            with connection.schema_editor() as se:
                se.create_model(ProfTypeHistory)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test', est_principale=True)
        cls.niveau = Niveau.objects.create(niveau='L1')
        cls.filiere = Filiere.objects.create(code='LP', intitule_fr='Licence Pro', institution=cls.inst)
        cls.dept = Departement.objects.create(nom='G1', institution=cls.inst, filiere=cls.filiere, niveau=cls.niveau)
        cls.sem = Semestre.objects.create(code_semestre='S2', semestre='Semestre 2',
                                          niveau_semestre=cls.niveau, type_semestre='P')

        cls.year_cur = Year.objects.create(annee='2025-2026', est_active=True)
        cls.year_old = Year.objects.create(annee='2024-2025')

        cls.em_mien = EM.objects.create(code_em='ST91', intitule='Python', departement=cls.dept,
                                        semestre=cls.sem, institution=cls.inst)
        cls.em_autre = EM.objects.create(code_em='XX99', intitule='Autre', departement=cls.dept,
                                         semestre=cls.sem, institution=cls.inst)

        # Enseignant SANS eval_saisie, lié à un prof qui enseigne em_mien
        cls.ens = User.objects.create_user(username='ens_notes', email='n@test.local',
                                           password='Xk93!plqz72', role='enseignant')
        cls.prof = Prof.objects.create(NNI=333333, nom='Prof Notes', type='permanent', user=cls.ens)
        SuiviePointage.objects.create(prof=cls.prof, institution=cls.inst,
                                      annee_universitaire='2025-2026', numero_semaine=1,
                                      type_semestre='P', em=cls.em_mien)

        cls._seq = 0

        def inscrire(matricule, year, em, est_dette=False):
            cls._seq += 1
            etu, _ = Etudiant.objects.get_or_create(
                matricule=matricule, defaults={'nom': matricule, 'departement': cls.dept, 'genre': 'M'})
            adm = InscriptionAdministrative.objects.create(
                etudiant=etu, annee_univ=year, filiere=cls.filiere, institution=cls.inst,
                niveau=1, numero_inscription=f'INS-{cls._seq}')
            ped = InscriptionPedagogique.objects.create(inscription_admin=adm, semestre=cls.sem)
            return InscriptionElement.objects.create(inscription_ped=ped, em=em, est_dette=est_dette)

        # A, B : inscrits em_mien en 2025-2026 (normaux)
        cls.ie_A = inscrire('A', cls.year_cur, cls.em_mien)
        inscrire('B', cls.year_cur, cls.em_mien)
        # C : ancienne inscription 2024-2025 + dette 2025-2026 -> doit apparaitre UNE fois
        inscrire('C', cls.year_old, cls.em_mien)
        inscrire('C', cls.year_cur, cls.em_mien, est_dette=True)
        # D : uniquement 2024-2025 -> doit etre EXCLU de la feuille 2025-2026
        inscrire('D', cls.year_old, cls.em_mien)
        # E : inscrit a em_autre (EM non enseigne par le prof)
        cls.ie_E = inscrire('E', cls.year_cur, cls.em_autre)

        cls.session = SessionEvaluation.objects.create(
            annee_univ=cls.year_cur, institution=cls.inst,
            type_session='normale', type_semestre='Pairs', est_ouverte=True)

    def setUp(self):
        self.client.force_authenticate(user=self.ens)

    # ── A. Correction de la liste ───────────────────────────────────────────
    def test_feuille_filtree_par_annee_session(self):
        r = self.client.get('/api/v1/evaluations/notes/feuille/',
                             {'session': str(self.session.id), 'em': str(self.em_mien.id)})
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        matricules = sorted(row['etudiant_matricule'] for row in r.data)
        # A, B, C (dette année courante) — PAS D (ancienne année), C non dupliqué
        self.assertEqual(matricules, ['A', 'B', 'C'],
                         f"Liste attendue [A,B,C], obtenu {matricules}")

    # ── B. Scope enseignant ─────────────────────────────────────────────────
    def test_enseignant_peut_lister_sessions(self):
        r = self.client.get('/api/v1/evaluations/sessions/')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)

    def test_feuille_em_non_enseigne_refusee(self):
        r = self.client.get('/api/v1/evaluations/notes/feuille/',
                             {'session': str(self.session.id), 'em': str(self.em_autre.id)})
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_saisir_bulk_son_em_ok(self):
        r = self.client.post('/api/v1/evaluations/notes/saisir-bulk/', {
            'session': self.session.id,
            'rows': [{'inscription_element': self.ie_A.id, 'cc': 12, 'tp': None, 'exam': 14}],
        }, format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertTrue(Note.objects.filter(inscription_element=self.ie_A, type_note='CC',
                                            valeur=Decimal('12')).exists())

    def test_saisir_bulk_em_etranger_refuse(self):
        r = self.client.post('/api/v1/evaluations/notes/saisir-bulk/', {
            'session': self.session.id,
            'rows': [{'inscription_element': self.ie_E.id, 'cc': 10}],
        }, format='json')
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Note.objects.filter(inscription_element=self.ie_E).exists(),
                         "Aucune note ne doit être écrite sur un EM étranger.")
