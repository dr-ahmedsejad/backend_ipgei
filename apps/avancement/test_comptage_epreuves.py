"""
Test — l'avancement COMPTE les épreuves au lieu de les cocher.

Un semestre porte couramment deux ou trois DS. Une croix posée dès le premier
laissait croire l'évaluation faite, sans jamais dire qu'il en manquait deux.

Ce qui identifie UNE épreuve est le couple (type, DATE), non la séance : une
épreuve de niveau se tient dans plusieurs classes le même jour, donc sur
plusieurs pointages. Compter les pointages afficherait « 5 DS » là où il y en a
eu un — c'est le piège que ces tests verrouillent.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date

from django.db import connection
from django.test import TestCase

from apps.avancement.views import _compute_avancement_em, _compute_avancement_prof
from apps.departement.models import Departement
from apps.em.models import EM
from apps.parametres.models import Institution, Niveau, Seance, Semestre
from apps.prof.models import Prof, ProfTypeHistory
from apps.scolarite.models import Filiere
from apps.suivi.models import SuiviePointage, SuiviePointageDepartement


class ComptageDesEpreuvesTest(TestCase):
    @classmethod
    def setUpClass(cls):
        # Même précaution que les tests voisins : ces tables de liaison peuvent
        # déjà exister selon la fixture de session.
        existantes = set(connection.introspection.table_names())
        for modele in (ProfTypeHistory, SuiviePointageDepartement):
            if modele._meta.db_table not in existantes:
                with connection.schema_editor() as se:
                    se.create_model(modele)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.inst   = Institution.objects.create(acronyme='TST', nom='Test', est_principale=True)
        cls.niveau = Niveau.objects.create(niveau='L1')
        cls.fil    = Filiere.objects.create(code='LP', intitule_fr='LP', institution=cls.inst)
        cls.g1     = Departement.objects.create(nom='G1', institution=cls.inst,
                                                filiere=cls.fil, niveau=cls.niveau,
                                                annee_universitaire='2025-2026')
        cls.g2     = Departement.objects.create(nom='G2', institution=cls.inst,
                                                filiere=cls.fil, niveau=cls.niveau,
                                                annee_universitaire='2025-2026')
        cls.sem    = Semestre.objects.create(code_semestre='S1', semestre='S1',
                                             niveau_semestre=cls.niveau, type_semestre='I')
        cls.em     = EM.objects.create(code_em='X1', intitule='Cours X', departement=cls.g1,
                                       semestre=cls.sem, institution=cls.inst, CM=10, TD=10)
        cls.prof   = Prof.objects.create(NNI=999002, nom='P', type='permanent')

        cls.ds = Seance.objects.create(type_seance='DS')
        cls.ef = Seance.objects.create(type_seance='EF')
        cls.td = Seance.objects.create(type_seance='TD')
        Seance.objects.create(type_seance='ER')

    def _pointer(self, type_seance, jour, departements=(), commentaire='Fait'):
        p = SuiviePointage.objects.create(
            prof=self.prof, em=self.em, institution=self.inst,
            annee_universitaire='2025-2026', numero_semaine=1, type_semestre='I',
            type_seance_fk=type_seance, duree_creneau=2.0,
            commentaire=commentaire, date_suivie=jour,
        )
        if departements:
            p.departements.set(departements)
        return p

    def _ligne_em(self):
        return _compute_avancement_em('2025-2026', 'I', ems=[self.em.pk])[0]

    def _ligne_prof(self):
        return _compute_avancement_prof('2025-2026', self.prof.pk)[0][0]

    # ── Le comptage ──────────────────────────────────────────────────────────
    def test_deux_ds_a_des_dates_differentes_comptent_pour_deux(self):
        self._pointer(self.ds, date(2025, 11, 4))
        self._pointer(self.ds, date(2025, 12, 2))

        self.assertEqual(self._ligne_em()['nb_ds'], 2)
        self.assertEqual(self._ligne_prof()['nb_ds'], 2)

    def test_une_epreuve_tenue_dans_plusieurs_classes_reste_UNE(self):
        """
        Le piège central. Le même DS passé le même jour à G1 et à G2 produit
        deux pointages — mais c'est une seule épreuve. Compter les pointages
        afficherait « 2 DS » là où il y en a eu un.
        """
        jour = date(2025, 11, 4)
        self._pointer(self.ds, jour, [self.g1])
        self._pointer(self.ds, jour, [self.g2])

        self.assertEqual(self._ligne_em()['nb_ds'], 1)
        self.assertEqual(self._ligne_prof()['nb_ds'], 1)

    def test_les_types_se_comptent_separement(self):
        self._pointer(self.ds, date(2025, 11, 4))
        self._pointer(self.ds, date(2025, 12, 2))
        self._pointer(self.ef, date(2026, 1, 20))

        ligne = self._ligne_em()
        self.assertEqual((ligne['nb_ds'], ligne['nb_exam'], ligne['nb_rat']), (2, 1, 0))

    def test_une_epreuve_non_pointee_ne_compte_pas(self):
        """Une épreuve planifiée n'est pas une épreuve tenue."""
        self._pointer(self.ds, date(2025, 11, 4), commentaire='Non fait')
        self.assertEqual(self._ligne_em()['nb_ds'], 0)

    def test_sans_epreuve_le_compte_est_zero(self):
        ligne = self._ligne_em()
        self.assertEqual((ligne['nb_ds'], ligne['nb_exam'], ligne['nb_rat']), (0, 0, 0))

    # ── Ce qui ne doit pas bouger ────────────────────────────────────────────
    def test_les_drapeaux_restent_servis(self):
        """
        Le PDF et les écrans se lisent aussi bien avec : `> 0` est la question
        qu'on posait, et la retirer casserait des lecteurs qu'on ne voit pas.
        """
        self._pointer(self.ds, date(2025, 11, 4))
        ligne = self._ligne_em()
        self.assertIs(ligne['ds_fait'], True)
        self.assertIs(ligne['exam_fait'], False)
        self.assertIs(ligne['rat_fait'], False)

    def test_un_ds_ne_deduit_RIEN_de_la_charge_de_la_matiere(self):
        """
        Le devoir surveillé comptait dans les heures de TD — il s'en tient un
        sur un créneau de TD, et l'assimilation venait de là. Mais un devoir
        n'avance pas le programme : la matière paraissait plus loin qu'elle ne
        l'était, et deux devoirs suffisaient à annoncer un semestre bouclé.
        """
        self._pointer(self.ds, date(2025, 11, 4))
        ligne = self._ligne_em()
        self.assertEqual((ligne['real_CM'], ligne['real_TD'],
                          ligne['real_TP'], ligne['real_PR']), (0, 0, 0, 0))
        # Il reste compté, mais À PART : c'est tout l'objet des colonnes
        # « Évaluations ».
        self.assertEqual(ligne['nb_ds'], 1)

    def test_la_vue_PAR_PROFESSEUR_continue_de_compter_le_DS_en_TD(self):
        """
        Volontairement inchangée : elle dit ce que CE professeur a assuré, non
        où en est le programme. Surveiller un devoir est du travail fait, et
        c'est sur elle que s'appuie la charge.
        """
        self._pointer(self.ds, date(2025, 11, 4))
        self.assertEqual(self._ligne_prof()['real_TD'], 2.0)

    def test_les_deux_ecrans_de_programme_tranchent_pareil(self):
        """
        « Avancement par matière » retient le MAXIMUM des unités que calcule
        « Avancement par groupe ». Un désaccord sur le DS ferait afficher deux
        volumes de TD différents pour la même matière — et personne ne saurait
        lequel croire.
        """
        from apps.avancement.par_groupe import avancement_par_groupe

        self._pointer(self.ds, date(2025, 11, 4), [self.g1])
        self._pointer(self.td, date(2025, 11, 6), [self.g1])

        par_matiere = self._ligne_em()['real_TD']
        lignes = avancement_par_groupe('2025-2026', 'I', ems=[self.em.pk])
        par_groupe = max((g['real_TD'] for ligne in lignes
                          for g in ligne['groupes']), default=0)

        self.assertEqual(par_matiere, 2.0, 'seul le TD compte')
        self.assertEqual(par_groupe, par_matiere)

    def test_un_examen_final_ne_compte_dans_aucune_famille_d_heures(self):
        """EF et ER sont des épreuves, pas du programme."""
        self._pointer(self.ef, date(2026, 1, 20))
        ligne = self._ligne_em()
        self.assertEqual((ligne['real_CM'], ligne['real_TD'],
                          ligne['real_TP'], ligne['real_PR']), (0, 0, 0, 0))
        self.assertEqual(ligne['nb_exam'], 1)
