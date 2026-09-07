"""
Test — le classement ne s'assoit plus sur des moyennes arrondies.

`_agreger` arrondissait au centième AVANT que `moyenne_annuelle` ne combine les
semestres. Deux étudiants séparés de quelques millièmes tombaient donc sur la
même valeur : l'égalité était fausse, et le tri de Python étant stable, le
départage retombait sur l'ordre du modèle — `['classe', 'numero_ordre',
'etudiant__nom']`. Autrement dit, sur le nom.

Constaté sur le PV signé de 2025-2026 : deux rangs faux en MPSIA, entre deux
étudiants dont les moyennes exactes valaient 8,6538 et 8,6549. Après
correction, les 154 rangs concordent.

Ce que le test fige :
  1. deux étudiants séparés de moins d'un centième sont classés dans le bon
     ordre, même quand l'alphabet dit l'inverse ;
  2. la valeur STOCKÉE et AFFICHÉE reste au centième — rien ne change pour qui
     lit un bulletin ;
  3. la DÉCISION continue de se comparer au seuil sur la valeur arrondie ;
  4. les appelants qui ne demandent rien reçoivent, comme avant, une valeur
     arrondie.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.absence.models import Etudiant
from apps.ipgei import services
from apps.ipgei.models import (
    Classe, Deliberation, InscriptionIPGEI, LigneDeliberation, Matiere,
    NiveauIPGEI, Note, SemestreIPGEI,
)
from apps.parametres.models import Institution, Niveau


class ArrondiEtRangTest(TestCase):
    """
    Deux étudiants dont les moyennes semestrielles ARRONDIES sont identiques,
    mais dont les valeurs exactes diffèrent au millième.
    """

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MPSI')
        cls.ref = NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI',
                                             libelle='MPSI', rang=1)
        cls.annee = '2025-2026'
        cls.s1 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S1', annee_universitaire=cls.annee,
            date_debut=date(2025, 10, 6))
        cls.s2 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S2', annee_universitaire=cls.annee,
            date_debut=date(2026, 1, 26))

        # Des coefficients qui NE divisent pas rond : 5 et 20 sur 25. C'est là
        # que l'arrondi intermédiaire mordait.
        def matiere(code, semestre, coef):
            return Matiere.objects.create(
                institution=cls.inst, code=code, intitule=code,
                code_semestre=semestre, niveau_ref=cls.ref,
                coefficient=Decimal(coef))

        cls.p = matiere('P', 'S1', 5)
        cls.q = matiere('Q', 'S1', 20)
        cls.r = matiere('R', 'S2', 1)

        cls.classe = Classe.objects.create(
            nom='MPSI A', institution=cls.inst, annee_universitaire=cls.annee,
            niveau=cls.socle, groupe='A')

    def _inscrit(self, matricule, nom, notes):
        """`notes` : {matière: (semestre, note retenue)}."""
        etudiant = Etudiant.objects.create(matricule=matricule, nom=nom)
        inscription = InscriptionIPGEI.objects.create(
            etudiant=etudiant, classe=self.classe,
            annee_universitaire=self.annee)
        for matiere, (semestre, valeur) in notes.items():
            Note.objects.create(
                inscription=inscription, matiere=matiere, semestre=semestre,
                note_retenue=Decimal(valeur),
                coefficient=matiere.coefficient)
        return inscription

    def _les_deux(self):
        """
        Le meilleur porte un nom qui le met DERNIER dans l'ordre du modèle :
        sans le correctif, l'alphabet le classerait second.

        S1 exact : (11.12×5 + 11.00×20)/25 = 11.024   -> arrondi 11.02
                   (11.08×5 + 11.00×20)/25 = 11.016   -> arrondi 11.02
        Les deux semestriels arrondis sont donc ÉGAUX ; les exacts non.
        """
        meilleur = self._inscrit('900001', 'Zeidane', {
            self.p: (self.s1, '11.12'), self.q: (self.s1, '11.00'),
            self.r: (self.s2, '12.00')})
        second = self._inscrit('900002', 'Abdallahi', {
            self.p: (self.s1, '11.08'), self.q: (self.s1, '11.00'),
            self.r: (self.s2, '12.00')})
        return meilleur, second

    def _jury(self):
        return Deliberation.objects.create(
            institution=self.inst, niveau='MPSI', classe=self.classe,
            annee_universitaire=self.annee,
            portee=Deliberation.PORTEE_ANNUELLE,
            seuil_validation=Decimal('10'))

    # ── 1. Le classement ────────────────────────────────────────────────────
    def test_un_ecart_au_millieme_departage_vraiment(self):
        meilleur, second = self._les_deux()
        services.calculer_deliberation(self._jury())

        self.assertEqual(LigneDeliberation.objects.get(inscription=meilleur).rang, 1,
                         'le meilleur au millième doit être premier')
        self.assertEqual(LigneDeliberation.objects.get(inscription=second).rang, 2)

    def test_deux_lignes_ne_portent_pas_le_meme_rang(self):
        self._les_deux()
        services.calculer_deliberation(self._jury())

        rangs = list(LigneDeliberation.objects.values_list('rang', flat=True))
        self.assertEqual(len(rangs), len(set(rangs)),
                         'un faux ex æquo produisait deux fois le même rang')

    def test_le_semestriel_arrondi_est_bien_le_meme_pour_les_deux(self):
        """Sans quoi le test ne prouverait rien : c'est là qu'était le piège."""
        meilleur, second = self._les_deux()
        a, _, _ = services.moyenne_semestrielle(meilleur, self.s1)
        b, _, _ = services.moyenne_semestrielle(second, self.s1)
        self.assertEqual(a, b, 'les moyennes ARRONDIES doivent être égales')
        self.assertEqual(a, Decimal('11.02'))

        a_exact, _, _ = services.moyenne_semestrielle(meilleur, self.s1,
                                                      arrondi=False)
        b_exact, _, _ = services.moyenne_semestrielle(second, self.s1,
                                                      arrondi=False)
        self.assertGreater(a_exact, b_exact, 'les exactes, elles, diffèrent')

    # ── 2. Ce qu'on stocke et affiche ───────────────────────────────────────
    def test_la_moyenne_stockee_reste_au_centieme(self):
        meilleur, _ = self._les_deux()
        services.calculer_deliberation(self._jury())

        ligne = LigneDeliberation.objects.get(inscription=meilleur)
        self.assertEqual(ligne.moyenne_generale, Decimal('11.61'),
                         "le bulletin ne montre pas plus de deux décimales")

    def test_sans_rien_demander_la_moyenne_arrive_arrondie(self):
        """Les autres appelants ne doivent voir aucun changement."""
        meilleur, _ = self._les_deux()
        moyenne, _, _ = services.moyenne_annuelle(meilleur, self.annee)
        self.assertEqual(moyenne, Decimal('11.61'))
        self.assertEqual(moyenne.as_tuple().exponent, -2)

    # ── 3. La décision ──────────────────────────────────────────────────────
    def test_la_decision_se_compare_au_seuil_sur_la_valeur_arrondie(self):
        """
        Un étudiant dont la moyenne EXACTE est 7,4976 est admis au seuil 7,50 :
        sa moyenne publiée vaut 7,50. Gagner en précision sur le classement ne
        doit pas retourner un sort déjà prononcé.
        """
        limite = self._inscrit('900003', 'Limite', {
            self.p: (self.s1, '7.47'), self.q: (self.s1, '7.50'),
            self.r: (self.s2, '7.50')})
        jury = Deliberation.objects.create(
            institution=self.inst, niveau='MPSI', classe=self.classe,
            annee_universitaire=self.annee,
            portee=Deliberation.PORTEE_ANNUELLE,
            seuil_validation=Decimal('7.50'))
        services.calculer_deliberation(jury)

        exacte, _, _ = services.moyenne_annuelle(limite, self.annee,
                                                 absents_a_zero=True,
                                                 arrondi=False)
        self.assertLess(exacte, Decimal('7.50'), 'la valeur exacte est en deçà')

        ligne = LigneDeliberation.objects.get(inscription=limite)
        self.assertEqual(ligne.moyenne_generale, Decimal('7.50'))
        self.assertEqual(ligne.decision, LigneDeliberation.DECISION_ADMIS,
                         'le seuil se compare à la moyenne publiée')
