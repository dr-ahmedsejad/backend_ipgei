"""
Test — affectation en masse des inscrits aux sous-groupes de leur classe.

Le geste de la rentrée : la scolarité constitue ses demi-groupes de TD. Il se
distingue de `repartir_inscriptions`, qui change de CLASSE — ici la classe ne
bouge pas, seule la moitié change.

Ce que le service garantit :
  1. il affecte, et il retire (`sous_groupe: None`) ;
  2. un sous-groupe appartient à UNE classe : y placer l'élève d'une autre est
     refusé, même en connaissant l'identifiant ;
  3. tout passe ou rien — un mouvement refusé annule les précédents ;
  4. il ne journalise PAS dans `HistoriqueClasse`, réservé aux vrais transferts ;
  5. les mouvements sans effet ne sont pas comptés.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.absence.models import Etudiant
from apps.ipgei import services
from apps.ipgei.models import (
    Classe, HistoriqueClasse, InscriptionIPGEI, NiveauIPGEI, SousGroupeTP,
)
from apps.parametres.models import Institution, Niveau


class AffectationGroupesTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MPSI')
        NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI',
                                   libelle='MPSI', rang=1)
        cls.annee = '2024-2025'
        cls.a = Classe.objects.create(nom='MPSI A', institution=cls.inst,
                                      annee_universitaire=cls.annee,
                                      niveau=cls.socle, groupe='A')
        cls.b = Classe.objects.create(nom='MPSI B', institution=cls.inst,
                                      annee_universitaire=cls.annee,
                                      niveau=cls.socle, groupe='B')
        cls.a1 = SousGroupeTP.objects.create(classe=cls.a, groupe='A1')
        cls.a2 = SousGroupeTP.objects.create(classe=cls.a, groupe='A2')
        cls.b1 = SousGroupeTP.objects.create(classe=cls.b, groupe='B1')

    def _inscrit(self, matricule, classe=None, sous_groupe=None):
        etudiant = Etudiant.objects.create(matricule=matricule, nom=f'Elu {matricule}')
        return InscriptionIPGEI.objects.create(
            etudiant=etudiant, classe=classe or self.a,
            annee_universitaire=self.annee, sous_groupe=sous_groupe)

    # ── 1. Le geste nominal ─────────────────────────────────────────────────
    def test_affecte_plusieurs_inscrits_en_une_fois(self):
        un, deux = self._inscrit('800001'), self._inscrit('800002')

        bilan = services.affecter_sous_groupes([
            {'inscription': un.pk,   'sous_groupe': self.a1.pk},
            {'inscription': deux.pk, 'sous_groupe': self.a2.pk},
        ])

        self.assertEqual(bilan['affectees'], 2)
        un.refresh_from_db(); deux.refresh_from_db()
        self.assertEqual(un.sous_groupe, self.a1)
        self.assertEqual(deux.sous_groupe, self.a2)

    def test_retirer_du_groupe_sans_sortir_de_la_classe(self):
        eleve = self._inscrit('800003', sous_groupe=self.a1)

        services.affecter_sous_groupes([{'inscription': eleve.pk, 'sous_groupe': None}])

        eleve.refresh_from_db()
        self.assertIsNone(eleve.sous_groupe)
        self.assertEqual(eleve.classe, self.a, 'la classe ne bouge pas')

    def test_un_mouvement_sans_effet_n_est_pas_compte(self):
        eleve = self._inscrit('800004', sous_groupe=self.a1)
        bilan = services.affecter_sous_groupes(
            [{'inscription': eleve.pk, 'sous_groupe': self.a1.pk}])
        self.assertEqual(bilan['affectees'], 0)

    # ── 2. Les gardes ───────────────────────────────────────────────────────
    def test_un_groupe_d_une_autre_classe_est_refuse(self):
        eleve = self._inscrit('800005')                      # en MPSI A
        with self.assertRaises(ValidationError) as capture:
            services.affecter_sous_groupes(
                [{'inscription': eleve.pk, 'sous_groupe': self.b1.pk}])
        self.assertIn('MPSI B', str(capture.exception))
        eleve.refresh_from_db()
        self.assertIsNone(eleve.sous_groupe)

    def test_une_inscription_inconnue_est_refusee(self):
        with self.assertRaises(ValidationError):
            services.affecter_sous_groupes(
                [{'inscription': 999999, 'sous_groupe': self.a1.pk}])

    def test_un_sous_groupe_inconnu_est_refuse(self):
        eleve = self._inscrit('800006')
        with self.assertRaises(ValidationError):
            services.affecter_sous_groupes(
                [{'inscription': eleve.pk, 'sous_groupe': 999999}])

    def test_une_liste_vide_est_refusee(self):
        with self.assertRaises(ValidationError):
            services.affecter_sous_groupes([])

    def test_tout_passe_ou_rien(self):
        # Le premier mouvement est valide, le second ne l'est pas : aucun des
        # deux ne doit subsister.
        bon    = self._inscrit('800007')
        mauvais = self._inscrit('800008')
        with self.assertRaises(ValidationError):
            services.affecter_sous_groupes([
                {'inscription': bon.pk,     'sous_groupe': self.a1.pk},
                {'inscription': mauvais.pk, 'sous_groupe': self.b1.pk},   # autre classe
            ])
        bon.refresh_from_db()
        self.assertIsNone(bon.sous_groupe, 'le mouvement valide a ete annule aussi')

    # ── 3. Ce qu'il ne fait pas ─────────────────────────────────────────────
    def test_aucune_ligne_d_historique_de_classe(self):
        eleve = self._inscrit('800009')
        avant = HistoriqueClasse.objects.count()
        services.affecter_sous_groupes(
            [{'inscription': eleve.pk, 'sous_groupe': self.a1.pk}])
        self.assertEqual(HistoriqueClasse.objects.count(), avant,
                         "le journal retrace les changements de CLASSE, pas de groupe")

    # ── 4. L'endpoint ───────────────────────────────────────────────────────
    def _vue(self, donnees):
        from rest_framework.test import APIRequestFactory, force_authenticate

        from apps.authentication.models import CustomUser
        from apps.ipgei.views import InscriptionIPGEIViewSet

        admin, _ = CustomUser.objects.get_or_create(
            username='adm_groupes', defaults={'email': 'a@t.l', 'role': 'admin'})
        vue = InscriptionIPGEIViewSet(action_map={'post': 'affecter_groupes'})
        requete = APIRequestFactory().post('/', donnees, format='json')
        force_authenticate(requete, user=admin)
        vue.request = vue.initialize_request(requete)
        vue.action = 'affecter_groupes'
        vue.format_kwarg = None
        return vue

    def test_l_endpoint_affecte(self):
        eleve = self._inscrit('800010')
        vue = self._vue({'affectations': [
            {'inscription': eleve.pk, 'sous_groupe': self.a1.pk}]})
        reponse = vue.affecter_groupes(vue.request)
        self.assertEqual(reponse.data['affectees'], 1)
        eleve.refresh_from_db()
        self.assertEqual(eleve.sous_groupe, self.a1)

    def test_l_endpoint_rend_400_sur_un_groupe_d_une_autre_classe(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        eleve = self._inscrit('800011')
        vue = self._vue({'affectations': [
            {'inscription': eleve.pk, 'sous_groupe': self.b1.pk}]})
        with self.assertRaises(DRFValidationError):
            vue.affecter_groupes(vue.request)
