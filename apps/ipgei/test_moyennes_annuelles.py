"""
Test — la moyenne annuelle IPGEI et sa pondération par semestre.

Ce que le test fige :
  1. `PONDERATION_ANNUELLE` : 1re année (S1 × 2 + S2 × 3) / 5, 2e année (S3 + S4) / 2 ;
  2. la moyenne annuelle est la moyenne PONDÉRÉE DES MOYENNES SEMESTRIELLES,
     pas l'agrégation plate de toutes les matières — les deux nombres diffèrent
     dès que les semestres n'ont pas le même total de coefficients ;
  3. absents comptés zéro (règle du jury), un semestre muet vaut zéro avec
     son poids ; sans ce drapeau (relevés), il s'efface et les poids se
     renormalisent.

Ces règles ne se devinent pas à la lecture du code : c'est précisément pour
cela qu'elles sont ici. Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.absence.models import Etudiant
from apps.ipgei import services
from apps.ipgei.models import (
    Classe, InscriptionIPGEI, Matiere, NiveauIPGEI, Note, SemestreIPGEI,
)
from apps.parametres.models import Institution, Niveau


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle_mpsi = Niveau.objects.create(niveau='MPSI')
        cls.socle_mp   = Niveau.objects.create(niveau='MP')
        cls.ref_mpsi = NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI',
                                                  libelle='MPSI', rang=1)
        cls.ref_mp   = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                                  libelle='MP', rang=2)
        cls.annee = '2025-2026'
        cls.semestres = {}
        for code in ('S1', 'S2', 'S3', 'S4'):
            cls.semestres[code] = SemestreIPGEI.objects.create(
                institution=cls.inst, code=code, annee_universitaire=cls.annee,
                date_debut=date(2025, 9, 1))

        def matiere(code, code_semestre, niveau, coef):
            return Matiere.objects.create(
                institution=cls.inst, code=code, intitule=code, code_semestre=code_semestre,
                niveau_ref=niveau, coefficient=Decimal(coef))

        # Totaux de coefficients VOLONTAIREMENT différents entre semestres :
        # c'est ce qui fait diverger la pondération de l'agrégation plate.
        cls.m_s1 = matiere('MATH1', 'S1', cls.ref_mpsi, 1)
        cls.m_s2 = matiere('MATH2', 'S2', cls.ref_mpsi, 3)
        cls.m_s3 = matiere('MATH3', 'S3', cls.ref_mp, 1)
        cls.m_s4 = matiere('MATH4', 'S4', cls.ref_mp, 3)

        cls.mpsi_a = Classe.objects.create(nom='MPSI A', institution=cls.inst,
                                           annee_universitaire=cls.annee,
                                           niveau=cls.socle_mpsi, groupe='A')
        cls.mp_a   = Classe.objects.create(nom='MP A', institution=cls.inst,
                                           annee_universitaire=cls.annee,
                                           niveau=cls.socle_mp, groupe='A')

    def _inscrit(self, matricule, classe):
        etudiant = Etudiant.objects.create(matricule=matricule, nom=f'Etudiant {matricule}')
        return InscriptionIPGEI.objects.create(etudiant=etudiant, classe=classe,
                                               annee_universitaire=self.annee)

    def _note(self, inscription, matiere, valeur):
        # update_or_create : les signaux d'inscription peuvent avoir déjà créé
        # la ligne vide — on pose la note retenue, on ne duplique pas.
        Note.objects.update_or_create(
            inscription=inscription, matiere=matiere, semestre=self.semestres[matiere.code_semestre],
            defaults={'note_retenue': Decimal(valeur) if valeur is not None else None,
                      'coefficient': matiere.coefficient})


class PonderationAnnuelleTest(_Base):
    def test_la_table_de_ponderation_est_celle_de_l_etablissement(self):
        self.assertEqual(services.PONDERATION_ANNUELLE[1], {'S1': Decimal('2'), 'S2': Decimal('3')})
        self.assertEqual(services.PONDERATION_ANNUELLE[2], {'S3': Decimal('1'), 'S4': Decimal('1')})

    def test_premiere_annee_s1_fois_2_plus_s2_fois_3_sur_5(self):
        i = self._inscrit('100001', self.mpsi_a)
        self._note(i, self.m_s1, 10)
        self._note(i, self.m_s2, 15)
        moyenne, _, _ = services.moyenne_annuelle(i, self.annee)
        # (10 × 2 + 15 × 3) / 5 = 13,00
        self.assertEqual(moyenne, Decimal('13.00'))
        # …et NON l'agrégation plate (10 × 1 + 15 × 3) / 4 = 13,75, ni la
        # moyenne simple 12,50.
        self.assertNotEqual(moyenne, Decimal('13.75'))
        self.assertNotEqual(moyenne, Decimal('12.50'))

    def test_deuxieme_annee_s3_et_s4_a_egalite(self):
        i = self._inscrit('200001', self.mp_a)
        self._note(i, self.m_s3, 10)
        self._note(i, self.m_s4, 15)
        moyenne, _, _ = services.moyenne_annuelle(i, self.annee)
        # (10 + 15) / 2 = 12,50 — pas (10 × 1 + 15 × 3) / 4 = 13,75.
        self.assertEqual(moyenne, Decimal('12.50'))

    def test_le_total_des_coefficients_reste_celui_des_matieres(self):
        i = self._inscrit('100002', self.mpsi_a)
        self._note(i, self.m_s1, 10)
        self._note(i, self.m_s2, 15)
        _, poids, _ = services.moyenne_annuelle(i, self.annee)
        self.assertEqual(poids, Decimal('4'), 'colonne « Total coef. » du PV : 1 + 3')


class AbsentsComptesZeroTest(_Base):
    def test_au_jury_un_semestre_muet_vaut_zero_avec_son_poids(self):
        i = self._inscrit('100003', self.mpsi_a)
        self._note(i, self.m_s1, 10)          # S2 jamais noté
        moyenne, _, _ = services.moyenne_annuelle(i, self.annee, absents_a_zero=True)
        # (10 × 2 + 0 × 3) / 5 = 4,00
        self.assertEqual(moyenne, Decimal('4.00'))

    def test_sur_un_releve_un_semestre_muet_s_efface(self):
        i = self._inscrit('100004', self.mpsi_a)
        self._note(i, self.m_s1, 10)
        moyenne, _, _ = services.moyenne_annuelle(i, self.annee)
        self.assertEqual(moyenne, Decimal('10.00'), 'les poids se renormalisent sur S1 seul')

    def test_au_jury_une_matiere_attendue_sans_note_compte_zero(self):
        # Une seconde matière en S1, jamais saisie : elle pèse quand même.
        Matiere.objects.create(institution=self.inst, code='PHYS1', intitule='PHYS1',
                               code_semestre='S1', niveau_ref=self.ref_mpsi,
                               coefficient=Decimal(1))
        i = self._inscrit('100005', self.mpsi_a)
        self._note(i, self.m_s1, 10)
        moyenne_s1, _, _ = services.moyenne_semestrielle(i, self.semestres['S1'],
                                                         absents_a_zero=True)
        self.assertEqual(moyenne_s1, Decimal('5.00'), '(10 × 1 + 0 × 1) / 2')

    def test_sans_aucune_note_la_moyenne_est_nulle_et_non_zero(self):
        i = self._inscrit('100006', self.mpsi_a)
        moyenne, _, _ = services.moyenne_annuelle(i, self.annee)
        self.assertIsNone(moyenne, 'hors jury, rien de noté = pas de moyenne')
