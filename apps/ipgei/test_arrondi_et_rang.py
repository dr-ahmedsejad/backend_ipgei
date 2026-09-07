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

Depuis, la scolarité a arrêté sa règle : le partage se lit sur la moyenne
PUBLIÉE. Deux étudiants qui affichent la même moyenne sont donc ex æquo, quelles
que soient les décimales que le calcul a vues — et l'alphabet ne départage plus
rien.

Ce que le test fige :
  1. même moyenne publiée, même rang ;
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
    def test_meme_moyenne_publiee_donc_meme_rang(self):
        """
        Leurs valeurs exactes diffèrent au millième, mais leur moyenne PUBLIÉE
        est la même : ils sont ex æquo, et le PV doit le dire.

        C'est la règle arrêtée par la scolarité — le partage se lit sur la
        moyenne générale, celle que l'étudiant a sous les yeux.
        """
        meilleur, second = self._les_deux()
        services.calculer_deliberation(self._jury())

        a = LigneDeliberation.objects.get(inscription=meilleur)
        b = LigneDeliberation.objects.get(inscription=second)
        self.assertEqual(a.moyenne_generale, b.moyenne_generale)
        self.assertEqual(a.rang, b.rang, 'même moyenne publiée, même rang')
        self.assertEqual(a.rang, 1)

    def test_le_rang_ne_depend_plus_de_l_ordre_alphabetique(self):
        """
        Le meilleur au millième porte un nom qui le mettrait DERNIER dans
        l'ordre du modèle. Avant, ce nom décidait de son rang.
        """
        meilleur, second = self._les_deux()
        services.calculer_deliberation(self._jury())

        rangs = {l.inscription_id: l.rang for l in LigneDeliberation.objects.all()}
        self.assertEqual(rangs[meilleur.pk], rangs[second.pk],
                         "l'alphabet ne départage plus rien")

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


class RangTasseTest(TestCase):
    """
    Les ex æquo partagent leur rang, et le suivant prend le rang d'après.

    Règle de la scolarité : deux cinquièmes, puis un SIXIÈME — pas un septième.
    Le partage porte sur la moyenne générale publiée, celle que lit l'étudiant.

    Deux mécanismes calculaient le rang, et ni l'un ni l'autre ne suivait cette
    règle : le jury énumérait 1, 2, 3 sans jamais partager — le départage
    retombant en silence sur l'ordre alphabétique — et le PV de semestre
    comptait les étudiants strictement meilleurs, ce qui partage bien mais
    saute ensuite.
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
        cls.m1 = Matiere.objects.create(
            institution=cls.inst, code='M1', intitule='M1', code_semestre='S1',
            niveau_ref=cls.ref, coefficient=Decimal(1))
        cls.m2 = Matiere.objects.create(
            institution=cls.inst, code='M2', intitule='M2', code_semestre='S2',
            niveau_ref=cls.ref, coefficient=Decimal(1))
        cls.classe = Classe.objects.create(
            nom='MPSI A', institution=cls.inst, annee_universitaire=cls.annee,
            niveau=cls.socle, groupe='A')

    def _inscrit(self, matricule, moyenne):
        """La même note aux deux semestres : la moyenne annuelle la vaut."""
        etudiant = Etudiant.objects.create(matricule=matricule, nom=matricule)
        inscription = InscriptionIPGEI.objects.create(
            etudiant=etudiant, classe=self.classe,
            annee_universitaire=self.annee)
        for matiere, semestre in ((self.m1, self.s1), (self.m2, self.s2)):
            Note.objects.create(inscription=inscription, matiere=matiere,
                                semestre=semestre,
                                note_retenue=Decimal(moyenne),
                                coefficient=matiere.coefficient)
        return inscription

    def _jury(self):
        jury = Deliberation.objects.create(
            institution=self.inst, niveau='MPSI', classe=self.classe,
            annee_universitaire=self.annee,
            portee=Deliberation.PORTEE_ANNUELLE,
            seuil_validation=Decimal('10'))
        services.calculer_deliberation(jury)
        return jury

    def _rangs(self):
        return {l.inscription.etudiant.matricule: l.rang
                for l in LigneDeliberation.objects.select_related(
                    'inscription__etudiant')}

    def test_deux_ex_aequo_partagent_leur_rang(self):
        self._inscrit('700001', '15.00')
        self._inscrit('700002', '12.50')
        self._inscrit('700003', '12.50')
        self._jury()

        rangs = self._rangs()
        self.assertEqual(rangs['700001'], 1)
        self.assertEqual(rangs['700002'], 2)
        self.assertEqual(rangs['700003'], 2, 'même moyenne, même rang')

    def test_le_suivant_prend_le_rang_d_apres_et_ne_saute_pas(self):
        """Deux cinquièmes, puis un SIXIÈME — c'est toute la règle."""
        for matricule, moyenne in (('700010', '18.00'), ('700011', '16.00'),
                                   ('700012', '14.00'), ('700013', '12.50'),
                                   ('700014', '12.50'), ('700015', '10.00')):
            self._inscrit(matricule, moyenne)
        self._jury()

        rangs = self._rangs()
        self.assertEqual(rangs['700013'], 4)
        self.assertEqual(rangs['700014'], 4, 'les deux quatrièmes')
        self.assertEqual(rangs['700015'], 5,
                         'le suivant est cinquième, pas sixième')

    def test_trois_ex_aequo_comptent_pour_un_seul_rang(self):
        for matricule, moyenne in (('700020', '17.00'), ('700021', '11.00'),
                                   ('700022', '11.00'), ('700023', '11.00'),
                                   ('700024', '9.00')):
            self._inscrit(matricule, moyenne)
        self._jury()

        rangs = self._rangs()
        self.assertEqual({rangs['700021'], rangs['700022'], rangs['700023']}, {2})
        self.assertEqual(rangs['700024'], 3)

    def test_le_partage_se_lit_sur_la_moyenne_publiee(self):
        """
        Deux étudiants qui lisent 12,50 sur leur PV sont ex æquo, quelles que
        soient les décimales que le calcul a vues.
        """
        self._inscrit('700030', '12.50')
        self._inscrit('700031', '12.50')
        self._jury()

        lignes = LigneDeliberation.objects.all()
        self.assertEqual({l.moyenne_generale for l in lignes},
                         {Decimal('12.50')})
        self.assertEqual({l.rang for l in lignes}, {1})

    def test_sans_ex_aequo_les_rangs_restent_consecutifs(self):
        for matricule, moyenne in (('700040', '15.00'), ('700041', '14.00'),
                                   ('700042', '13.00')):
            self._inscrit(matricule, moyenne)
        self._jury()

        self.assertEqual(sorted(self._rangs().values()), [1, 2, 3])
