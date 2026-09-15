"""
Test — une classe peut compter une matière autrement que sa promotion.

`Matiere` règle la pondération pour tout un niveau, et c'est le cas général. Mais
en S4 2025-2026, seule MP A a tenu une colle de maths : la colonne existe dans
son fichier de notes, elle est absente en MP B et vide en MP C et MP D. La
scolarité a donc compté 20/10/70 pour MP A et 30/0/70 pour les trois autres — et
nos moyennes s'écartaient des siennes pour 29 étudiants.

Ce que le test fige :

  1. sans dérogation, rien ne change — la règle générale s'applique ;
  2. avec, la classe concernée calcule autrement, et ELLE SEULE ;
  3. l'instantané de la note retient la pondération DÉROGATOIRE : l'historique
     est protégé, et le relevé dira ce qui a réellement compté ;
  4. `has_tp` suit la dérogation — sans quoi la classe aurait une part TP de
     10 % qu'elle ne pourrait jamais remplir ;
  5. une dérogation identique à la règle est refusée : elle figerait
     silencieusement l'ancienne valeur le jour où la règle change ;
  6. la somme fait 100, et la classe appartient au niveau de la matière.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.absence.models import Etudiant
from apps.ipgei import services
from apps.ipgei.models import (
    Classe, Evaluation, InscriptionIPGEI, Matiere, NiveauIPGEI, Note,
    PonderationClasse, SemestreIPGEI,
)
from apps.parametres.models import Institution, Niveau


class PonderationClasseTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MP')
        cls.ref = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                             libelle='MP', rang=2)
        cls.autre_socle = Niveau.objects.create(niveau='MPSI')
        cls.autre_ref = NiveauIPGEI.objects.create(
            institution=cls.inst, code='MPSI', libelle='MPSI', rang=1)
        cls.s4 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S4', annee_universitaire='2025-2026',
            date_debut=date(2026, 1, 26))
        # La règle générale : pas de TP, 30/0/70 — celle des trois classes qui
        # n'ont pas tenu de colle.
        cls.math = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Mathématiques',
            code_semestre='S4', niveau_ref=cls.ref, coefficient=Decimal('4'),
            has_tp=False, pct_ds=Decimal('30'), pct_tp=Decimal('0'),
            pct_exam=Decimal('70'))
        cls.mpa = Classe.objects.create(
            nom='MP A', institution=cls.inst, annee_universitaire='2025-2026',
            niveau=cls.socle, groupe='A')
        cls.mpb = Classe.objects.create(
            nom='MP B', institution=cls.inst, annee_universitaire='2025-2026',
            niveau=cls.socle, groupe='B')

    def _note(self, classe, matricule, *, ds, tp, exam):
        etudiant = Etudiant.objects.create(matricule=matricule, nom=f'E{matricule}')
        inscription = InscriptionIPGEI.objects.create(
            etudiant=etudiant, classe=classe, annee_universitaire='2025-2026')
        note = Note.objects.create(
            inscription=inscription, matiere=self.math, semestre=self.s4,
            note_tp=Decimal(tp) if tp is not None else None)
        Evaluation.objects.create(note=note, type_evaluation=Evaluation.TYPE_DS,
                                  numero=1, valeur=Decimal(ds))
        Evaluation.objects.create(note=note, type_evaluation=Evaluation.TYPE_EXAM,
                                  numero=1, valeur=Decimal(exam))
        return note

    def _deroger(self, classe=None):
        return PonderationClasse.objects.create(
            matiere=self.math, classe=classe or self.mpa,
            pct_ds=Decimal('20'), pct_tp=Decimal('10'), pct_exam=Decimal('70'),
            has_tp=True, motif='Colle de maths tenue en MP A seulement.')

    # ── 1. Sans dérogation, rien ne bouge ───────────────────────────────────
    def test_sans_derogation_la_regle_generale_s_applique(self):
        note = self._note(self.mpa, '900001', ds='10', tp='20', exam='10')
        services.calculer_note(note)

        # 30/0/70 : le TP est ignoré, la note ne vaut que DS et examen.
        self.assertEqual(note.pct_tp, Decimal('0'))
        self.assertEqual(note.moyenne, Decimal('10.00'))

    def test_le_resolveur_rend_la_matiere_a_defaut(self):
        self.assertIs(services.ponderation_applicable(self.math, self.mpa),
                      self.math)
        self.assertIs(services.ponderation_applicable(self.math, None), self.math)

    # ── 2. Avec dérogation, et elle seule ───────────────────────────────────
    def test_la_classe_qui_deroge_compte_autrement(self):
        self._deroger()
        a = self._note(self.mpa, '900001', ds='10', tp='20', exam='10')
        b = self._note(self.mpb, '900002', ds='10', tp='20', exam='10')
        services.calculer_note(a)
        services.calculer_note(b)

        # MP A : 20×10 + 10×20 + 70×10 = 1000 -> 10,00… mais le TP à 20 tire
        # vers le haut : (200 + 200 + 700) / 100 = 11,00.
        self.assertEqual(a.moyenne, Decimal('11.00'))
        # MP B ignore le TP : (300 + 700) / 100 = 10,00.
        self.assertEqual(b.moyenne, Decimal('10.00'),
                         "la classe qui n'a pas tenu de colle ne bouge pas")

    def test_l_instantane_retient_la_derogation(self):
        """
        C'est ce qui protège l'historique ET ce qui fait dire au relevé ce qui a
        réellement compté — non la règle d'aujourd'hui.
        """
        self._deroger()
        note = self._note(self.mpa, '900001', ds='10', tp='20', exam='10')
        services.calculer_note(note)

        self.assertEqual(
            (note.pct_ds, note.pct_tp, note.pct_exam),
            (Decimal('20'), Decimal('10'), Decimal('70')))

        # La dérogation disparaît : la note déjà calculée ne bouge pas.
        PonderationClasse.objects.all().delete()
        note.refresh_from_db()
        self.assertEqual(note.pct_tp, Decimal('10'))
        self.assertEqual(note.moyenne, Decimal('11.00'))

    def test_has_tp_suit_la_derogation(self):
        """
        Sans ce drapeau, la classe aurait une part TP de 10 % qu'elle ne
        pourrait jamais remplir : la saisie refuserait la fiche.
        """
        derogation = self._deroger()
        self.assertFalse(self.math.has_tp)
        self.assertTrue(
            services.ponderation_applicable(self.math, self.mpa).has_tp)
        self.assertFalse(
            services.ponderation_applicable(self.math, self.mpb).has_tp)
        self.assertEqual(derogation.ponderation(),
                         {'pct_ds': Decimal('20'), 'pct_tp': Decimal('10'),
                          'pct_exam': Decimal('70')})

    def test_sans_note_de_tp_la_ponderation_se_renormalise(self):
        """
        Un étudiant absent à la colle ne doit pas être puni : la part TP se
        redistribue sur les composantes présentes, comme partout ailleurs.
        """
        self._deroger()
        note = self._note(self.mpa, '900001', ds='10', tp=None, exam='20')
        services.calculer_note(note)
        # (20×10 + 70×20) / 90 = 17,78
        self.assertEqual(note.moyenne, Decimal('17.78'))

    # ── 3. Ce que le modèle refuse ──────────────────────────────────────────
    def test_une_derogation_qui_ne_deroge_a_rien_est_refusee(self):
        """
        Elle survivrait à un changement de la règle générale et figerait
        l'ancienne valeur en silence — le pire des deux mondes.
        """
        jumelle = PonderationClasse(
            matiere=self.math, classe=self.mpa, has_tp=False,
            pct_ds=Decimal('30'), pct_tp=Decimal('0'), pct_exam=Decimal('70'),
            motif='Identique à la règle.')
        with self.assertRaises(ValidationError) as cas:
            jumelle.full_clean()
        self.assertIn('dérogation', str(cas.exception))

    def test_la_somme_doit_faire_cent(self):
        mauvaise = PonderationClasse(
            matiere=self.math, classe=self.mpa, has_tp=True,
            pct_ds=Decimal('20'), pct_tp=Decimal('10'), pct_exam=Decimal('60'),
            motif='Somme fausse.')
        with self.assertRaises(ValidationError):
            mauvaise.full_clean()

    def test_sans_tp_la_part_tp_doit_etre_nulle(self):
        mauvaise = PonderationClasse(
            matiere=self.math, classe=self.mpa, has_tp=False,
            pct_ds=Decimal('20'), pct_tp=Decimal('10'), pct_exam=Decimal('70'),
            motif='Part TP sans TP.')
        with self.assertRaises(ValidationError):
            mauvaise.full_clean()

    def test_la_classe_doit_appartenir_au_niveau_de_la_matiere(self):
        etrangere = Classe.objects.create(
            nom='MPSI A', institution=self.inst, annee_universitaire='2025-2026',
            niveau=self.autre_socle, groupe='A')
        mauvaise = PonderationClasse(
            matiere=self.math, classe=etrangere, has_tp=True,
            pct_ds=Decimal('20'), pct_tp=Decimal('10'), pct_exam=Decimal('70'),
            motif='Mauvais niveau.')
        with self.assertRaises(ValidationError) as cas:
            mauvaise.full_clean()
        self.assertIn('MPSI', str(cas.exception))

    def test_une_seule_derogation_par_matiere_et_classe(self):
        from django.db.utils import IntegrityError

        self._deroger()
        with self.assertRaises(IntegrityError):
            PonderationClasse.objects.create(
                matiere=self.math, classe=self.mpa, has_tp=True,
                pct_ds=Decimal('25'), pct_tp=Decimal('5'), pct_exam=Decimal('70'),
                motif='Doublon.')
