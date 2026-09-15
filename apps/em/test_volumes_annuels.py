"""
Test — le volume prévu d'une année ne change plus quand la maquette bouge.

Il n'existe qu'UN EM par matière, partagé par toutes les promotions : `Matiere`
n'est pas datée, et le miroir réécrit l'EM à chaque retouche. Or « Avancement »
lisait `em.CM` en direct comme volume de référence. Ramener MATH-S1 de 147 h à
100 h pour la rentrée faisait donc passer l'avancement de 2024-2025 de 1 % à
2 % — une année révolue dont le chiffre change sans que personne n'y touche.

Ce que le test fige :

  1. le gel recopie les volumes du moment, une ligne par (année, EM) ;
  2. seuls les EM MIROIRS d'une matière IPGEI sont gelés — pas le catalogue
     socle, qui n'appartient à aucune maquette de prépa ;
  3. il est idempotent : regénérer une semaine ne regèle rien, le premier gel
     fait foi ;
  4. **une retouche de maquette ne touche plus à l'année gelée** — c'est
     l'assertion qui porte tout le reste ;
  5. une année jamais gelée continue de lire les volumes vivants : on ne rend
     pas un écran vide pour un gel manquant ;
  6. `rafraichir=True` réécrit, parce que corriger une maquette fausse doit
     rester possible — mais jamais en silence.

Base SQLite en mémoire (siga.settings.test).
"""
from django.test import TestCase

from apps.em.models import EM, VolumeAnnuelEM
from apps.em.volumes import figer_maquette, volumes_applicables, volumes_par_em
from apps.ipgei.models import Matiere, NiveauIPGEI
from apps.parametres.models import Institution
from apps.scolarite.models import Filiere


class VolumeAnnuelEMTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.filiere = Filiere.objects.create(code='IPGEI', intitule_fr='IPGEI')
        cls.ref = NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI',
                                             libelle='MPSI', rang=1)

        # Un EM MIROIR : il a une matière IPGEI derrière lui.
        cls.em = EM.objects.create(code_em='MATH-S1', intitule='Mathématiques',
                                   CM=147, TD=21, TP=0, PR=0, filiere=cls.filiere)
        cls.matiere = Matiere.objects.create(
            institution=cls.inst, code='MATH', code_semestre='S1',
            intitule='Mathématiques', coefficient=14, niveau_ref=cls.ref,
            volume_cm=147, volume_td=21, volume_tp=0, em_miroir=cls.em)

        # Un EM du CATALOGUE SOCLE : aucune matière IPGEI derrière.
        cls.em_socle = EM.objects.create(code_em='CHIM-S1', intitule='Chimie',
                                         CM=30, TD=10, TP=10, PR=0,
                                         filiere=cls.filiere)

    # ── 1-2. Ce que le gel prend, et ce qu'il laisse ────────────────────────
    def test_le_gel_recopie_les_volumes_du_moment(self):
        self.assertEqual(figer_maquette('2024-2025'), 1)
        fige = VolumeAnnuelEM.objects.get(annee_universitaire='2024-2025')
        self.assertEqual((fige.em_id, fige.CM, fige.TD, fige.TP, fige.PR),
                         (self.em.pk, 147, 21, 0, 0))

    def test_le_catalogue_socle_n_est_pas_gele(self):
        """Geler CHIM donnerait une histoire à une matière que l'IPGEI n'enseigne pas."""
        figer_maquette('2024-2025')
        self.assertFalse(
            VolumeAnnuelEM.objects.filter(em=self.em_socle).exists())

    # ── 3. Idempotence ──────────────────────────────────────────────────────
    def test_regeler_n_ecrit_rien(self):
        figer_maquette('2024-2025')
        self.assertEqual(figer_maquette('2024-2025'), 0)
        self.assertEqual(VolumeAnnuelEM.objects.count(), 1)

    # ── 4. L'assertion qui porte tout ───────────────────────────────────────
    def test_une_retouche_de_maquette_ne_touche_plus_a_l_annee_gelee(self):
        figer_maquette('2024-2025')

        # La scolarité ramène MATH-S1 de 147 h à 100 h pour la rentrée, et le
        # miroir réécrit l'EM — le geste réel, pas une simulation.
        self.em.CM = 100
        self.em.save(update_fields=['CM'])

        self.assertEqual(volumes_applicables(self.em, '2024-2025').CM, 147,
                         "l'année gelée doit garder son volume d'origine")
        self.assertEqual(self.em.CM, 100,
                         'la maquette vivante, elle, a bien changé')

    def test_l_annee_suivante_gele_la_nouvelle_valeur(self):
        figer_maquette('2024-2025')
        self.em.CM = 100
        self.em.save(update_fields=['CM'])
        figer_maquette('2026-2027')

        self.assertEqual(volumes_applicables(self.em, '2024-2025').CM, 147)
        self.assertEqual(volumes_applicables(self.em, '2026-2027').CM, 100)

    # ── 5. Le repli ─────────────────────────────────────────────────────────
    def test_une_annee_jamais_gelee_lit_les_volumes_vivants(self):
        self.assertIs(volumes_applicables(self.em, '2030-2031'), self.em)
        self.assertEqual(volumes_par_em('2030-2031'), {})

    def test_sans_annee_on_rend_l_em(self):
        self.assertIs(volumes_applicables(self.em, ''), self.em)
        self.assertIsNone(volumes_applicables(None, '2024-2025'))

    # ── 6. Le rafraîchissement, délibéré ────────────────────────────────────
    def test_rafraichir_reecrit_le_gel(self):
        figer_maquette('2024-2025')
        self.em.CM = 100
        self.em.save(update_fields=['CM'])

        figer_maquette('2024-2025')                      # sans rafraîchir
        self.assertEqual(volumes_applicables(self.em, '2024-2025').CM, 147)

        figer_maquette('2024-2025', rafraichir=True)     # geste délibéré
        self.assertEqual(volumes_applicables(self.em, '2024-2025').CM, 100)

    # ── Le dictionnaire de boucle dit la même chose que l'unitaire ──────────
    def test_volumes_par_em_concorde_avec_l_unitaire(self):
        figer_maquette('2024-2025')
        par_em = volumes_par_em('2024-2025')
        self.assertEqual(par_em[self.em.pk].CM,
                         volumes_applicables(self.em, '2024-2025').CM)
        self.assertNotIn(self.em_socle.pk, par_em,
                         'le catalogue socle se replie sur ses volumes vivants')

    def test_une_seule_ligne_par_annee_et_par_em(self):
        from django.db import IntegrityError, transaction
        figer_maquette('2024-2025')
        with self.assertRaises(IntegrityError), transaction.atomic():
            VolumeAnnuelEM.objects.create(
                annee_universitaire='2024-2025', em=self.em, CM=1)
