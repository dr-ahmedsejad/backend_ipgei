"""
Test — l'ancrage d'un EM, et le filtre qui écarte les EM hors maquette.

Deux défauts constatés sur la base réelle, en cherchant pourquoi le sélecteur
d'élément de module des écrans de vacation restait vide :

1. `_classe_ancre` élisait la CLASSE D'ATTENTE. Son `groupe` est vide, elle
   triait donc première. Les EM de S3 s'ancraient sur « MP » plutôt que sur
   « MP A » — or la classe d'attente est un conteneur, tenu hors des emplois du
   temps, du suivi et des listes d'appel, et plusieurs écrans excluent
   explicitement `is_container`.

2. Le socle porte huit EM hérités d'avant le fork — CHIM et EPS aux quatre
   semestres — auxquels aucune matière de la maquette ne correspond. Rien ne
   les écartait : ils étaient seulement masqués par un filtre cassé, et sont
   remontés dans le sélecteur dès qu'on l'a réparé. On saisirait des heures sur
   un enseignement qui n'existe pas.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.em.models import EM
from apps.em.views import EMViewSet
from apps.ipgei.miroir import _classe_ancre, synchroniser_matiere
from apps.ipgei.models import Classe, Matiere, NiveauIPGEI, SemestreIPGEI
from apps.parametres.models import Institution, Niveau


class AncrageDesEMTest(TestCase):
    """L'ancrage donne un NIVEAU. Encore faut-il qu'il désigne une vraie classe."""

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle_mp = Niveau.objects.create(niveau='MP')
        cls.ref_mp = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                                libelle='MP', rang=2)
        cls.annee = '2025-2026'
        SemestreIPGEI.objects.create(institution=cls.inst, code='S3',
                                     annee_universitaire=cls.annee,
                                     date_debut=date(2025, 10, 6))

    def _matiere(self, code='MATH'):
        return Matiere.objects.create(
            institution=self.inst, code=code, intitule=code, code_semestre='S3',
            niveau_ref=self.ref_mp, coefficient=Decimal('4'))

    def _classe(self, groupe):
        """`groupe` vide = la classe d'attente, marquée conteneur par `save()`."""
        return Classe.objects.create(
            institution=self.inst, niveau=self.socle_mp, groupe=groupe,
            annee_universitaire=self.annee)

    def test_la_classe_d_attente_n_est_jamais_choisie_comme_ancre(self):
        attente = self._classe('')
        reelle = self._classe('A')
        self.assertTrue(attente.is_container, 'la classe sans groupe est un conteneur')

        self.assertEqual(_classe_ancre(self._matiere()), reelle,
                         "l'ancre doit être une classe réelle")

    def test_sans_aucune_classe_reelle_l_ancre_reste_vide(self):
        """Mieux vaut pas d'ancre qu'une ancre sur un conteneur."""
        self._classe('')
        self.assertIsNone(_classe_ancre(self._matiere()))

    def test_un_em_cree_sans_classe_s_ancre_des_qu_une_classe_existe(self):
        """
        Le cas réel : les EM de S3 et S4 sont nés avant l'ouverture des classes
        MP, donc sans ancrage. Rejouer le miroir doit les rattraper.
        """
        # `post_save` synchronise déjà : l'EM naît sans ancre, faute de classe.
        matiere = self._matiere()
        matiere.refresh_from_db()
        self.assertIsNone(matiere.em_miroir.departement_id, 'aucune classe encore')

        reelle = self._classe('A')
        synchroniser_matiere(matiere)
        matiere.refresh_from_db()
        self.assertEqual(matiere.em_miroir.departement_id, reelle.pk)

    def test_un_ancrage_valable_ne_bouge_pas(self):
        """
        La règle de stabilité : ouvrir l'année suivante ne doit pas déplacer
        les ancrages existants, sans quoi l'avancement de l'année en cours se
        vide.
        """
        premiere = self._classe('A')
        matiere = self._matiere()
        matiere.refresh_from_db()
        self.assertEqual(matiere.em_miroir.departement_id, premiere.pk)

        Classe.objects.create(institution=self.inst, niveau=self.socle_mp,
                              groupe='A', annee_universitaire='2026-2027')
        synchroniser_matiere(matiere)
        matiere.refresh_from_db()
        self.assertEqual(matiere.em_miroir.departement_id, premiere.pk,
                         "l'ancrage ne suit pas la nouvelle année")


class FiltreMiroirTest(TestCase):
    """`?miroir=1` : ne proposer que les EM qui reflètent une matière active."""

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MP')
        cls.ref = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                             libelle='MP', rang=2)
        SemestreIPGEI.objects.create(institution=cls.inst, code='S3',
                                     annee_universitaire='2025-2026',
                                     date_debut=date(2025, 10, 6))
        Classe.objects.create(institution=cls.inst, niveau=cls.socle, groupe='A',
                              annee_universitaire='2025-2026')

        # La création suffit : `post_save` pose le miroir.
        cls.matiere = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Mathématiques',
            code_semestre='S3', niveau_ref=cls.ref, coefficient=Decimal('4'))
        cls.matiere.refresh_from_db()

        # L'héritage du socle : un EM que nulle matière ne reflète.
        cls.orphelin = EM.objects.create(code_em='CHIM-S3', intitule='Chimie',
                                         institution=cls.inst)

        U = get_user_model()
        cls.admin = U.objects.create_superuser(
            username='patron', email='a@t.l', password='Admin!2612mdp')

    def _lister(self, requete=''):
        vue = EMViewSet.as_view({'get': 'list'})
        demande = APIRequestFactory().get('/api/v1/ems/?' + requete)
        force_authenticate(demande, user=self.admin)
        reponse = vue(demande)
        donnees = reponse.data
        lignes = donnees.get('results') if isinstance(donnees, dict) else donnees
        return reponse.status_code, sorted(e['code_em'] for e in lignes)

    def test_sans_le_filtre_les_em_herites_remontent(self):
        code, codes = self._lister()
        self.assertEqual(code, 200)
        self.assertIn('CHIM-S3', codes, "le catalogue complet reste accessible")

    def test_avec_le_filtre_seuls_les_miroirs_sont_rendus(self):
        code, codes = self._lister('miroir=1')
        self.assertEqual(code, 200)
        self.assertEqual(codes, ['MATH-S3'])

    def test_une_matiere_desactivee_disparait_du_filtre(self):
        """Retirer une matière de la maquette doit la retirer des sélecteurs."""
        self.matiere.actif = False
        self.matiere.save(update_fields=['actif'])

        _, codes = self._lister('miroir=1')
        self.assertEqual(codes, [])

    def test_le_filtre_ne_s_applique_pas_sans_le_demander(self):
        """
        Ce ViewSet sert aussi la couche LMD, où les EM ne sont pas des miroirs :
        appliqué par défaut, le filtre y viderait tout.
        """
        _, sans = self._lister()
        self.assertIn('CHIM-S3', sans)
        _, avec = self._lister('miroir=0')
        self.assertIn('CHIM-S3', avec)
