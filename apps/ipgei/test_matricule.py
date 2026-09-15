"""
Test — le matricule se propose tout seul, sans jamais s'imposer.

Les 440 matricules de la base suivent une règle régulière : deux chiffres
d'année, un segment, trois chiffres de séquence — `242011`. Rien ne la mettait
en œuvre : le champ était une saisie libre, et la série s'est trouée (205
numéros distribués pour 143 étudiants en 2024) par des attributions restées
sans suite.

Ce que le test fige :

  1. le préfixe se LIT dans les données, il n'est pas écrit en dur — s'il
     encode un jour autre chose, la règle suivra ;
  2. on reprend le premier numéro LIBRE, pas le suivant du plus grand : les
     trous se comblent au lieu de s'accumuler ;
  3. une promotion encore vide reçoit sa série sans qu'on ait à l'amorcer ;
  4. l'endpoint refuse une année mal formée plutôt que d'inventer un numéro ;
  5. un matricule déjà pris donne un message qui NOMME son titulaire, et non
     une erreur d'intégrité.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.absence.models import Etudiant
from apps.ipgei import services
from apps.ipgei.models import Classe, InscriptionIPGEI, NiveauIPGEI
from apps.parametres.models import Institution, Niveau


class MatriculeTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MPSI')
        cls.ref = NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI',
                                             libelle='MPSI', rang=1)
        cls.classe = Classe.objects.create(
            nom='MPSI A', institution=cls.inst, annee_universitaire='2025-2026',
            niveau=cls.socle, groupe='A')

    def _etudiants(self, *matricules):
        for m in matricules:
            Etudiant.objects.create(matricule=m, nom=f'Etudiant {m}')

    # ── 1. Le préfixe se lit dans les données ───────────────────────────────
    def test_le_segment_vient_des_matricules_existants(self):
        """
        Le « 2 » de `242011` n'est pas documenté. Plutôt que de l'écrire en dur,
        on le relit : une promotion numérotée autrement sera suivie, pas
        contredite.
        """
        self._etudiants('257001', '257002', '257003')
        self.assertEqual(services._prefixe_matricule('2025-2026'), '257')
        # Une année sans matricule emprunte le segment le plus répandu.
        self.assertEqual(services._prefixe_matricule('2026-2027'), '267')

    def test_base_vide_la_constante_sert_de_dernier_recours(self):
        self.assertEqual(services._prefixe_matricule('2026-2027'), '262')
        self.assertEqual(services.prochain_matricule('2026-2027'), '262001')

    # ── 2. Le premier LIBRE, pas le suivant du plus grand ───────────────────
    def test_les_trous_se_comblent(self):
        """
        C'est le point qui distingue « prochain libre » de « dernier plus un ».
        Avec 001, 002 et 009 pris, on rend 003 — pas 010.
        """
        self._etudiants('252001', '252002', '252009')
        self.assertEqual(services.prochain_matricule('2025-2026'), '252003')

    def test_une_serie_continue_avance_normalement(self):
        self._etudiants('252001', '252002', '252003')
        self.assertEqual(services.prochain_matricule('2025-2026'), '252004')

    def test_les_promotions_ne_se_melangent_pas(self):
        """Remplir 2025 ne déplace pas le prochain numéro de 2024."""
        self._etudiants('252001', '252002', '242001')
        self.assertEqual(services.prochain_matricule('2025-2026'), '252003')
        self.assertEqual(services.prochain_matricule('2024-2025'), '242002')

    def test_serie_epuisee_dit_pourquoi(self):
        from django.core.exceptions import ValidationError

        Etudiant.objects.bulk_create([
            Etudiant(matricule=f'262{i:03d}', nom=f'E{i}')
            for i in range(1, 1000)])
        with self.assertRaises(ValidationError) as cas:
            services.prochain_matricule('2026-2027')
        self.assertIn('262', str(cas.exception))

    # ── 3. L'endpoint ───────────────────────────────────────────────────────
    def _vue(self, annee):
        from rest_framework.test import APIRequestFactory, force_authenticate

        from apps.authentication.models import CustomUser
        from apps.ipgei.views import InscriptionIPGEIViewSet

        admin, _ = CustomUser.objects.get_or_create(
            username='adm_matricule', defaults={'email': 'a@t.l', 'role': 'admin'})
        vue = InscriptionIPGEIViewSet(action_map={'get': 'prochain_matricule'})
        requete = APIRequestFactory().get('/', {'annee': annee})
        force_authenticate(requete, user=admin)
        vue.request = vue.initialize_request(requete)
        vue.action = 'prochain_matricule'
        vue.format_kwarg = None
        return vue

    def test_l_endpoint_rend_le_numero(self):
        self._etudiants('252001')
        reponse = self._vue('2025-2026').prochain_matricule(self._vue('2025-2026').request)
        self.assertEqual(reponse.data, {'matricule': '252002'})

    def test_l_endpoint_refuse_une_annee_mal_formee(self):
        """
        Sans ce contrôle, `''[2:4]` vaut `''` et la fonction rendrait un
        matricule tronqué — un numéro faux valant mieux qu'aucun, pour qui le
        saisit sans le relire.
        """
        for mauvaise in ('', 'bidon', '2026', '2026/2027'):
            vue = self._vue(mauvaise)
            with self.assertRaises(DRFValidationError, msg=f'pour {mauvaise!r}'):
                vue.prochain_matricule(vue.request)

    # ── 4. Champ laissé vide : le serveur attribue ──────────────────────────
    def _inscrire(self, identite):
        from rest_framework.test import APIRequestFactory, force_authenticate

        from apps.authentication.models import CustomUser
        from apps.ipgei.views import InscriptionIPGEIViewSet

        admin, _ = CustomUser.objects.get_or_create(
            username='adm_creation', defaults={'email': 'a@t.l', 'role': 'admin'})
        vue = InscriptionIPGEIViewSet(action_map={'post': 'nouvelle'})
        requete = APIRequestFactory().post(
            '/', {'classe': self.classe.pk, 'nouvel_etudiant': identite},
            format='json')
        force_authenticate(requete, user=admin)
        vue.request = vue.initialize_request(requete)
        vue.action = 'nouvelle'
        vue.format_kwarg = None
        return vue.nouvelle(vue.request)

    def test_matricule_vide_le_serveur_attribue(self):
        """
        Le cas courant : on saisit un nom, rien d'autre. Le numéro est choisi à
        L'ÉCRITURE — proposé à l'ouverture du formulaire, il aurait pu être pris
        entre-temps sans que la secrétaire l'apprenne avant de valider.
        """
        self._etudiants('252001', '252002')
        reponse = self._inscrire({'matricule': '', 'nom': 'Sans Numero'})
        self.assertEqual(reponse.data['etudiant_matricule'], '252003')
        self.assertTrue(Etudiant.objects.filter(matricule='252003').exists())

    def test_matricule_absent_du_corps_vaut_vide(self):
        """Ne pas envoyer la clé du tout doit valoir la même chose que l'envoyer vide."""
        reponse = self._inscrire({'nom': 'Clef Absente'})
        self.assertEqual(reponse.data['etudiant_matricule'], '252001')

    def test_un_matricule_impose_est_respecte(self):
        """La saisie manuelle reste maîtresse — l'auditeur qui garde son numéro."""
        reponse = self._inscrire({'matricule': '242011', 'nom': 'Revenant'})
        self.assertEqual(reponse.data['etudiant_matricule'], '242011')

    def test_deux_creations_de_suite_ne_se_marchent_pas_dessus(self):
        premiere = self._inscrire({'nom': 'Premier'})
        seconde  = self._inscrire({'nom': 'Second'})
        self.assertEqual(premiere.data['etudiant_matricule'], '252001')
        self.assertEqual(seconde.data['etudiant_matricule'],  '252002')

    def test_une_collision_simultanee_reprend_le_suivant(self):
        """
        Deux créations visant le même numéro : l'unicité en base tranche, et
        l'on reprend le suivant. Personne ne voulait CE numéro en particulier —
        renvoyer une erreur serait refuser un service pour rien.

        On simule en faisant surgir le concurrent entre le choix et l'écriture.
        """
        from unittest.mock import patch

        vrai = services.prochain_matricule
        appels = []

        def concurrent(annee):
            numero = vrai(annee)
            appels.append(numero)
            if len(appels) == 1:          # quelqu'un d'autre le prend d'abord
                Etudiant.objects.create(matricule=numero, nom='Concurrent')
            return numero

        with patch.object(services, 'prochain_matricule', side_effect=concurrent):
            reponse = self._inscrire({'nom': 'Deuxieme Arrive'})

        self.assertEqual(appels[0], '252001')
        self.assertEqual(reponse.data['etudiant_matricule'], '252002')

    # ── 5. Le doublon se dit en clair ───────────────────────────────────────
    def test_un_matricule_deja_pris_nomme_son_titulaire(self):
        """
        `Etudiant.matricule` est unique en base : sans contrôle, le doublon
        remonte en IntegrityError — une 500 opaque après une saisie complète.
        """
        from rest_framework.test import APIRequestFactory, force_authenticate

        from apps.authentication.models import CustomUser
        from apps.ipgei.views import InscriptionIPGEIViewSet

        Etudiant.objects.create(matricule='252001', nom='Fatimetou Mint Ahmed',
                                nom_fr='Fatimetou Mint Ahmed')
        admin, _ = CustomUser.objects.get_or_create(
            username='adm_doublon', defaults={'email': 'a@t.l', 'role': 'admin'})
        corps = {'classe': self.classe.pk,
                 'nouvel_etudiant': {'matricule': '252001', 'nom': 'Homonyme'}}
        vue = InscriptionIPGEIViewSet(action_map={'post': 'nouvelle'})
        requete = APIRequestFactory().post('/', corps, format='json')
        force_authenticate(requete, user=admin)
        vue.request = vue.initialize_request(requete)
        vue.action = 'nouvelle'
        vue.format_kwarg = None

        with self.assertRaises(DRFValidationError) as cas:
            vue.nouvelle(vue.request)
        message = str(cas.exception)
        self.assertIn('252001', message)
        self.assertIn('Fatimetou', message, 'le message doit NOMMER le titulaire')
        self.assertEqual(Etudiant.objects.filter(matricule='252001').count(), 1)
