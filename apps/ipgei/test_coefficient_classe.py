"""
Test — une classe peut aussi PESER une matière autrement.

Ce n'est pas la même nature de décision que la pondération dérogatoire, qui vit
dans `test_ponderation_classe.py`. Celle-ci corrige la FIDÉLITÉ : MP A a tenu
une colle, les autres non, et sa moyenne de maths reste comparable à la leur. Le
coefficient est un choix de POLITIQUE : il change combien la matière pèse dans
la moyenne générale, donc ce que le rang récompense — et `classement.py` met
toutes les classes d'un niveau dans la MÊME liste pour le CNIM.

Les deux tiennent dans la même ligne de `PonderationClasse` : ce sont deux
façons de dire « cette classe-là s'écarte », et elles méritent un seul motif.

Ce que le test fige :

  1. vide, il ne déroge à rien — la matière fait foi ;
  2. renseigné, il ne vaut que pour sa classe ;
  3. l'instantané de la note le retient : le relevé dira ce qui a compté ;
  4. il pèse aussi pour un ABSENT, qui n'a pourtant aucune note où figer quoi
     que ce soit — c'est le seul chemin qui relit la règle en direct ;
  5. une dérogation dont SEUL le coefficient s'écarte est valide ; nulle ou
     négative, elle est refusée ;
  6. la feuille de classement AVERTIT quand une classe classée en use, et se
     tait pour une pondération seule.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.absence.models import Etudiant
from apps.ipgei import classement, services
from apps.ipgei.models import (
    Classe, Evaluation, InscriptionIPGEI, Matiere, NiveauIPGEI, Note,
    PonderationClasse, SemestreIPGEI,
)
from apps.parametres.models import Institution, Niveau


class CoefficientDerogatoireTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MP')
        cls.ref = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                             libelle='MP', rang=2)
        cls.s4 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S4', annee_universitaire='2025-2026',
            date_debut=date(2026, 1, 26))
        cls.math = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Mathematiques',
            code_semestre='S4', niveau_ref=cls.ref, coefficient=Decimal('4'),
            has_tp=False, pct_ds=Decimal('30'), pct_tp=Decimal('0'),
            pct_exam=Decimal('70'))
        cls.phys = Matiere.objects.create(
            institution=cls.inst, code='PHYS', intitule='Physique',
            code_semestre='S4', niveau_ref=cls.ref, coefficient=Decimal('6'),
            has_tp=False, pct_ds=Decimal('30'), pct_tp=Decimal('0'),
            pct_exam=Decimal('70'))
        cls.mpa = Classe.objects.create(
            nom='MP A', institution=cls.inst, annee_universitaire='2025-2026',
            niveau=cls.socle, groupe='A')
        cls.mpb = Classe.objects.create(
            nom='MP B', institution=cls.inst, annee_universitaire='2025-2026',
            niveau=cls.socle, groupe='B')

    # ── Fabriques ───────────────────────────────────────────────────────────
    def _inscrire(self, classe, matricule):
        etudiant = Etudiant.objects.create(matricule=matricule, nom='E' + matricule)
        return InscriptionIPGEI.objects.create(
            etudiant=etudiant, classe=classe, annee_universitaire='2025-2026')

    def _note(self, inscription, matiere, *, ds, exam):
        note = Note.objects.create(inscription=inscription, matiere=matiere,
                                   semestre=self.s4)
        Evaluation.objects.create(note=note, type_evaluation=Evaluation.TYPE_DS,
                                  numero=1, valeur=Decimal(ds))
        Evaluation.objects.create(note=note, type_evaluation=Evaluation.TYPE_EXAM,
                                  numero=1, valeur=Decimal(exam))
        services.calculer_note(note)
        return note

    def _deroger_coef(self, classe, matiere, valeur):
        return PonderationClasse.objects.create(
            matiere=matiere, classe=classe,
            pct_ds=matiere.pct_ds, pct_tp=matiere.pct_tp,
            pct_exam=matiere.pct_exam, has_tp=matiere.has_tp,
            coefficient=Decimal(valeur),
            motif='Decision du conseil pedagogique.')

    def _deroger_ponderation(self, classe, matiere):
        return PonderationClasse.objects.create(
            matiere=matiere, classe=classe, pct_ds=Decimal('20'),
            pct_tp=Decimal('10'), pct_exam=Decimal('70'), has_tp=True,
            motif='Colle tenue en MP A seulement.')

    # ── 1. Vide = la matière ────────────────────────────────────────────────
    def test_sans_derogation_le_coefficient_est_celui_de_la_matiere(self):
        self.assertEqual(services.coefficient_applicable(self.math, self.mpa),
                         Decimal('4'))
        self.assertEqual(services.coefficient_applicable(self.math, None),
                         Decimal('4'))

    def test_une_derogation_sans_coefficient_ne_touche_pas_au_poids(self):
        """La pondération seule ne doit rien changer au poids de la matière."""
        self._deroger_ponderation(self.mpa, self.math)
        self.assertEqual(services.coefficient_applicable(self.math, self.mpa),
                         Decimal('4'))

    # ── 2-3. Renseigné : sa classe seule, et figé ───────────────────────────
    def test_le_coefficient_ne_vaut_que_pour_sa_classe(self):
        self._deroger_coef(self.mpa, self.math, '2')
        self.assertEqual(services.coefficient_applicable(self.math, self.mpa),
                         Decimal('2'))
        self.assertEqual(services.coefficient_applicable(self.math, self.mpb),
                         Decimal('4'))

    def test_l_instantane_de_la_note_retient_le_coefficient(self):
        self._deroger_coef(self.mpa, self.math, '2')
        a = self._note(self._inscrire(self.mpa, '910001'), self.math,
                       ds='10', exam='10')
        b = self._note(self._inscrire(self.mpb, '910002'), self.math,
                       ds='10', exam='10')
        self.assertEqual(a.coefficient, Decimal('2'))
        self.assertEqual(b.coefficient, Decimal('4'),
                         "la classe qui ne deroge pas garde le coefficient general")

    def test_la_moyenne_du_semestre_suit_le_coefficient_derogatoire(self):
        """MATH a 20, PHYS a 10 : le poids de MATH decide du resultat."""
        self._deroger_coef(self.mpa, self.math, '2')
        for classe, matricule, coef_math in ((self.mpa, '910011', Decimal('2')),
                                             (self.mpb, '910012', Decimal('4'))):
            insc = self._inscrire(classe, matricule)
            self._note(insc, self.math, ds='20', exam='20')
            self._note(insc, self.phys, ds='10', exam='10')
            moyenne, poids, _ = services.moyenne_semestrielle(insc, self.s4)
            attendu = (Decimal('20') * coef_math + Decimal('10') * Decimal('6'))
            self.assertEqual(poids, coef_math + Decimal('6'), classe.nom)
            self.assertEqual(moyenne, round(attendu / poids, 2), classe.nom)

    # ── 4. L'absent : aucune note, donc aucun instantané ────────────────────
    def test_le_coefficient_derogatoire_pese_aussi_pour_un_absent(self):
        """
        Le seul chemin qui relit la regle EN DIRECT : il n'y a pas de note ou
        elle aurait pu etre figee. Sans lui, un absent de MP A serait penalise
        au coefficient general alors que sa classe en applique un autre.
        """
        self._deroger_coef(self.mpa, self.math, '2')
        insc = self._inscrire(self.mpa, '910021')
        self._note(insc, self.phys, ds='10', exam='10')   # MATH jamais passee

        moyenne, poids, _ = services.moyenne_semestrielle(insc, self.s4,
                                                      absents_a_zero=True)
        self.assertEqual(poids, Decimal('8'),
                         'PHYS 6 + MATH 2 (derogatoire), et non 6 + 4')
        self.assertEqual(moyenne, Decimal('7.50'))        # 10 x 6 / 8

    def test_un_absent_de_la_classe_qui_ne_deroge_pas_garde_le_poids_general(self):
        self._deroger_coef(self.mpa, self.math, '2')
        insc = self._inscrire(self.mpb, '910022')
        self._note(insc, self.phys, ds='10', exam='10')

        _, poids, _ = services.moyenne_semestrielle(insc, self.s4, absents_a_zero=True)
        self.assertEqual(poids, Decimal('10'))            # 6 + 4

    # ── 5. Ce que la validation accepte et refuse ───────────────────────────
    def test_deroger_le_seul_coefficient_est_accepte(self):
        """Elle deroge bel et bien : la refuser interdirait le cas d'usage."""
        self._deroger_coef(self.mpa, self.math, '2').full_clean()

    def test_une_derogation_vraiment_identique_reste_refusee(self):
        doublon = PonderationClasse(
            matiere=self.math, classe=self.mpa, pct_ds=self.math.pct_ds,
            pct_tp=self.math.pct_tp, pct_exam=self.math.pct_exam,
            has_tp=self.math.has_tp, coefficient=self.math.coefficient,
            motif='Aucun ecart.')
        with self.assertRaises(ValidationError):
            doublon.full_clean()

    def test_un_coefficient_nul_ou_negatif_est_refuse(self):
        for valeur in ('0', '-1'):
            candidat = PonderationClasse(
                matiere=self.math, classe=self.mpa, pct_ds=self.math.pct_ds,
                pct_tp=self.math.pct_tp, pct_exam=self.math.pct_exam,
                has_tp=self.math.has_tp, coefficient=Decimal(valeur),
                motif='Essai.')
            with self.assertRaises(ValidationError, msg=valeur):
                candidat.full_clean()

    # ── 6. La feuille de classement le dit ──────────────────────────────────
    def test_le_classement_avertit_quand_une_classe_classee_deroge(self):
        resultats = [{'classe': 'MP A'}, {'classe': 'MP B'}]
        self.assertEqual(classement.coefficients_derogatoires(resultats), [],
                         'rien a signaler tant que personne ne deroge')

        self._deroger_coef(self.mpa, self.math, '2')
        lignes = classement.coefficients_derogatoires(resultats)
        self.assertEqual(len(lignes), 1)
        self.assertIn('MP A', lignes[0])
        self.assertIn('MATH compte 2 au lieu de 4', lignes[0])
        self.assertIn('conseil pedagogique', lignes[0],
                      'le motif doit figurer, sinon personne ne saura pourquoi')

    def test_une_derogation_de_ponderation_seule_n_avertit_pas(self):
        """Elle ne change pas ce que le rang recompense : rien a signaler."""
        self._deroger_ponderation(self.mpa, self.math)
        self.assertEqual(
            classement.coefficients_derogatoires([{'classe': 'MP A'}]), [])

    def test_une_classe_hors_du_classement_edite_n_est_pas_signalee(self):
        """On n'avertit que sur les classes REELLEMENT presentes sur la feuille."""
        self._deroger_coef(self.mpb, self.math, '2')
        self.assertEqual(
            classement.coefficients_derogatoires([{'classe': 'MP A'}]), [])
