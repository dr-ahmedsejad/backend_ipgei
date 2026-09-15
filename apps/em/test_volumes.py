"""
Test — le volume de maquette est FIGÉ par année.

Le défaut réparé : il n'existe qu'un EM par matière, partagé par toutes les
promotions, et « Avancement » lisait `em.CM` en direct comme volume de
référence. Ramener MATH-S1 de 147 h à 100 h pour la rentrée faisait passer
l'avancement de 2024-2025 de 1 % à 2 % — une année révolue dont le chiffre
change sans que personne n'y ait touché.

Base SQLite en mémoire (siga.settings.test).
"""
from django.db import connection
from django.test import TestCase

from apps.avancement.views import _compute_avancement_prof
from apps.departement.models import Departement
from apps.em.models import EM, VolumeAnnuelEM
from apps.em.volumes import figer_maquette, volumes_applicables, volumes_par_em
from apps.ipgei.models import Matiere
from apps.parametres.models import Institution, Niveau, Seance, Semestre
from apps.prof.models import Prof, ProfTypeHistory
from apps.scolarite.models import Filiere
from apps.suivi.models import SuiviePointage


class VolumeAnnuelTest(TestCase):
    @classmethod
    def setUpClass(cls):
        if ProfTypeHistory._meta.db_table not in connection.introspection.table_names():
            with connection.schema_editor() as se:
                se.create_model(ProfTypeHistory)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.inst   = Institution.objects.create(acronyme='TST', nom='Test', est_principale=True)
        cls.niveau = Niveau.objects.create(niveau='MPSI')
        cls.fil    = Filiere.objects.create(code='IPGEI', intitule_fr='IPGEI', institution=cls.inst)
        cls.dept   = Departement.objects.create(nom='MPSI A', institution=cls.inst,
                                                filiere=cls.fil, niveau=cls.niveau)
        cls.sem    = Semestre.objects.create(code_semestre='S1', semestre='S1',
                                             niveau_semestre=cls.niveau, type_semestre='I')
        cls.cm     = Seance.objects.create(type_seance='CM')
        cls.em     = EM.objects.create(code_em='MATH-S1', intitule='Mathématiques',
                                       departement=cls.dept, semestre=cls.sem,
                                       institution=cls.inst, CM=147, TD=21, TP=0)
        cls.prof   = Prof.objects.create(NNI=999101, nom='P', type='permanent')

        # `figer_maquette` ne gèle que les EM MIROIRS d'une matière IPGEI : sans
        # cette matière derrière, les tests de gel passeraient à vide.
        cls.matiere = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Mathématiques',
            code_semestre='S1', coefficient=14,
            volume_cm=147, volume_td=21, volume_tp=0, em_miroir=cls.em,
        )

        SuiviePointage.objects.create(
            prof=cls.prof, em=cls.em, institution=cls.inst,
            annee_universitaire='2024-2025', numero_semaine=1, type_semestre='I',
            type_seance_fk=cls.cm, duree_creneau=1.5, commentaire='Fait',
        )

    # ── La règle « particulier sinon général » ──────────────────────────────
    def test_sans_gel_on_lit_l_em_vivant(self):
        """Le repli est le comportement d'avant, conservé tel quel."""
        self.assertIs(volumes_applicables(self.em, '2024-2025'), self.em)
        self.assertEqual(volumes_par_em('2024-2025'), {})

    def test_le_gel_prend_le_pas_sur_l_em(self):
        VolumeAnnuelEM.objects.create(annee_universitaire='2024-2025', em=self.em, CM=99)
        self.assertEqual(volumes_applicables(self.em, '2024-2025').CM, 99)
        self.assertEqual(self.em.CM, 147, "l'EM lui-même ne doit pas bouger")

    # ── Ce que `figer_maquette` gèle, et ce qu'il laisse ────────────────────
    def test_ne_gele_que_les_miroirs_d_une_matiere_ipgei(self):
        """
        Le catalogue socle (CHIM, EPS…) n'appartient à aucune maquette de
        prépa : lui donner une histoire de maquette serait mentir.
        """
        EM.objects.create(code_em='CHIM', intitule='Chimie', departement=self.dept,
                          semestre=self.sem, institution=self.inst, CM=30)
        figer_maquette('2024-2025')
        geles = set(VolumeAnnuelEM.objects.values_list('em__code_em', flat=True))
        self.assertIn('MATH-S1', geles,
                      "le miroir d'une matière IPGEI, lui, est bien gelé")
        self.assertNotIn('CHIM', geles)

    def test_le_gel_est_idempotent(self):
        """Regénérer une semaine ne doit rien regeler : le premier gel fait foi."""
        VolumeAnnuelEM.objects.create(annee_universitaire='2024-2025', em=self.em, CM=147)
        self.em.CM = 100
        self.em.save(update_fields=['CM'])

        self.assertEqual(figer_maquette('2024-2025'), 0)
        self.assertEqual(
            VolumeAnnuelEM.objects.get(annee_universitaire='2024-2025', em=self.em).CM,
            147, 'un second passage ne doit pas écraser le gel initial')

    def test_rafraichir_est_un_geste_delibere(self):
        VolumeAnnuelEM.objects.create(annee_universitaire='2024-2025', em=self.em, CM=147)
        self.em.CM = 100
        self.em.save(update_fields=['CM'])

        figer_maquette('2024-2025', rafraichir=True)
        self.assertEqual(
            VolumeAnnuelEM.objects.get(annee_universitaire='2024-2025', em=self.em).CM, 100)

    def test_une_annee_ne_gele_pas_celle_d_a_cote(self):
        VolumeAnnuelEM.objects.create(annee_universitaire='2024-2025', em=self.em, CM=147)
        self.assertIs(volumes_applicables(self.em, '2025-2026'), self.em)

    # ── La garde : le passé ne se réécrit plus ──────────────────────────────
    def test_retoucher_la_maquette_ne_reecrit_plus_une_annee_gelee(self):
        """
        LE test. Une année gelée garde son volume de référence quoi qu'il
        arrive à la maquette ; une année qui ne l'est pas suit la maquette du
        jour, comme avant — c'est ce qui rend la correction sûre à poser sur
        une base existante.
        """
        VolumeAnnuelEM.objects.create(annee_universitaire='2024-2025', em=self.em,
                                      CM=147, TD=21, TP=0)

        avant, _ = _compute_avancement_prof('2024-2025', self.prof.pk)
        self.assertEqual(avant[0]['plan_CM'], 147)
        pct_avant = avant[0]['pct_CM']

        # La scolarité ramène la matière à 100 h pour la rentrée prochaine.
        self.em.CM = 100
        self.em.save(update_fields=['CM'])

        apres, _ = _compute_avancement_prof('2024-2025', self.prof.pk)
        self.assertEqual(apres[0]['plan_CM'], 147,
                         "le volume de 2024-2025 ne doit pas suivre la maquette 2027")
        self.assertEqual(apres[0]['pct_CM'], pct_avant,
                         'le pourcentage d\'une année révolue ne doit pas bouger')

    def test_une_annee_neuve_prend_bien_la_maquette_du_jour(self):
        SuiviePointage.objects.create(
            prof=self.prof, em=self.em, institution=self.inst,
            annee_universitaire='2026-2027', numero_semaine=1, type_semestre='I',
            type_seance_fk=self.cm, duree_creneau=1.5, commentaire='Fait',
        )
        self.em.CM = 100
        self.em.save(update_fields=['CM'])

        lignes, _ = _compute_avancement_prof('2026-2027', self.prof.pk)
        self.assertEqual(lignes[0]['plan_CM'], 100,
                         'sans gel, on lit la maquette vivante — le repli')
