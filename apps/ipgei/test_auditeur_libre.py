"""
Test — un auditeur libre ne décale le rang de personne.

Un auditeur libre suit la scolarité sans être de la promotion : il compose, il
reçoit un relevé, et il n'entre dans aucun jury. Le risque n'est PAS qu'il
apparaisse là où il ne devrait pas — cela se verrait. C'est qu'il DÉCALE
SILENCIEUSEMENT le rang des autres : un auditeur troisième fait reculer d'un
cran tous ceux qui le suivent, sur un PV signé comme sur des relevés déjà
remis, sans que rien ne le signale — les rangs restent une suite continue.

Ce que le test fige :

  1. les rangs, les moyennes de classe et les effectifs sont IDENTIQUES selon
     que l'auditeur existe ou non — la seule assertion qui protège vraiment ;
  2. il n'entre ni dans le jury, ni dans le classement ;
  3. il a malgré tout son rang, PAR COMPARAISON : sa moyenne est située dans la
     promotion sans y entrer, quitte à partager son rang avec un diplômant ;
  4. il peut composer au rattrapage, bien que n'ayant aucune ligne de jury.

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


class AuditeurLibreTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MP')
        cls.ref = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                             libelle='MP', rang=2)
        cls.annee = '2025-2026'
        cls.s3 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S3', annee_universitaire=cls.annee,
            date_debut=date(2025, 10, 6))
        cls.s4 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S4', annee_universitaire=cls.annee,
            date_debut=date(2026, 1, 26))
        cls.math = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Mathématiques',
            code_semestre='S3', niveau_ref=cls.ref, coefficient=Decimal('4'))
        cls.classe = Classe.objects.create(
            nom='MP A', institution=cls.inst, annee_universitaire=cls.annee,
            niveau=cls.socle, groupe='A')

    # ── fabrique ────────────────────────────────────────────────────────────
    def _inscrit(self, matricule, nom, note, *, auditeur=False):
        etudiant = Etudiant.objects.create(matricule=matricule, nom=nom)
        inscription = InscriptionIPGEI.objects.create(
            etudiant=etudiant, classe=self.classe,
            annee_universitaire=self.annee, auditeur_libre=auditeur)
        Note.objects.create(
            inscription=inscription, matiere=self.math, semestre=self.s3,
            note_retenue=Decimal(note), moyenne=Decimal(note),
            coefficient=self.math.coefficient)
        return inscription

    def _promotion(self):
        """Trois diplômants : 16, 12, 08."""
        return [self._inscrit('900001', 'Ahmed',    '16.00'),
                self._inscrit('900002', 'Bilal',    '12.00'),
                self._inscrit('900003', 'Cheikh',   '08.00')]

    def _jury(self, portee=Deliberation.PORTEE_SEMESTRE):
        return Deliberation.objects.create(
            institution=self.inst, niveau='MP', classe=self.classe,
            annee_universitaire=self.annee, semestre=self.s3, portee=portee,
            seuil_validation=Decimal('10'))

    def _photo(self, inscriptions):
        """Rang, effectif et moyenne de classe de chacun — l'état à comparer."""
        photo = {}
        for i in inscriptions:
            stats = services.statistiques_classe(i, self.s3)[self.math.pk]
            photo[i.pk] = (
                services.rang_general_classe(i, self.s3),
                services.rang_general_promotion(i, self.s3),
                stats['moyenne_classe'], stats['rang'], stats['effectif'],
            )
        return photo

    # ── 1. L'assertion qui protège ──────────────────────────────────────────
    def test_un_auditeur_ne_decale_le_rang_de_personne(self):
        """
        Rangs, moyenne de classe et effectifs, avant puis après l'arrivée d'un
        auditeur qui se placerait DEUXIÈME. Sans exclusion, Bilal reculerait de
        2 à 3 et la moyenne de classe passerait de 12,00 à 12,75.
        """
        promotion = self._promotion()
        avant = self._photo(promotion)

        self._inscrit('900004', 'Auditeur', '15.00', auditeur=True)
        apres = self._photo(promotion)

        self.assertEqual(avant, apres)
        # Et l'état attendu, pour que le test dise aussi ce qu'il protège.
        rang_bilal = apres[promotion[1].pk][0]
        self.assertEqual(rang_bilal, {'rang': 2, 'effectif': 3})
        self.assertEqual(apres[promotion[1].pk][2], Decimal('12.00'))

    # ── 2. Absent des populations collectives ───────────────────────────────
    def test_absent_du_jury_et_du_classement(self):
        self._promotion()
        auditeur = self._inscrit('900004', 'Auditeur', '15.00', auditeur=True)

        jury = self._jury()
        services.calculer_deliberation(jury)
        self.assertEqual(jury.lignes.count(), 3)
        self.assertFalse(
            LigneDeliberation.objects.filter(inscription=auditeur).exists())

        cursus = InscriptionIPGEI.objects.cursus()
        self.assertEqual(cursus.count(), 3)
        self.assertNotIn(auditeur, cursus)

    # ── 3. Il a son rang, par comparaison ───────────────────────────────────
    def test_rang_par_comparaison(self):
        """
        L'auditeur à 15 se situe DEUXIÈME — un seul diplômant le devance. Bilal
        reste deuxième lui aussi : les deux relevés portent le même rang, et
        c'est voulu, ils ne relèvent pas du même classement.
        """
        promotion = self._promotion()
        auditeur = self._inscrit('900004', 'Auditeur', '15.00', auditeur=True)

        sien = services.rang_general_classe(auditeur, self.s3)
        self.assertEqual(sien, {'rang': 2, 'effectif': 3})
        self.assertEqual(services.rang_general_promotion(auditeur, self.s3)['rang'], 2)
        self.assertEqual(
            services.rang_general_classe(promotion[1], self.s3)['rang'], 2)

        # Et le rang par MATIÈRE suit la même règle.
        self.assertEqual(
            services.statistiques_classe(auditeur, self.s3)[self.math.pk]['rang'], 2)

    def test_le_releve_reste_servi(self):
        """Le relevé se bâtit depuis les notes, sans jamais lire un jury."""
        self._promotion()
        auditeur = self._inscrit('900004', 'Auditeur', '15.00', auditeur=True)

        releve = services.releve_semestre(auditeur, self.s3)
        self.assertEqual(releve['moyenne'], Decimal('15.00'))
        self.assertEqual(releve['rang_general']['rang'], 2)
        self.assertEqual(len(releve['notes']), 1)

    # ── 4. Le rattrapage lui est ouvert ─────────────────────────────────────
    def test_rattrapage_ouvert_sans_ligne_de_jury(self):
        """
        Il n'a aucune ligne de délibération — et doit pourtant pouvoir composer.
        La grille est un écran de SAISIE, pas une pièce de jury.
        """
        promotion = self._promotion()
        auditeur = self._inscrit('900004', 'Auditeur', '08.00', auditeur=True)

        annuel = self._jury(Deliberation.PORTEE_ANNUELLE)
        services.calculer_deliberation(annuel)
        LigneDeliberation.objects.filter(inscription=promotion[2]).update(
            decision=LigneDeliberation.DECISION_RATTRAPAGE)

        rattrapage = Deliberation.objects.create(
            institution=self.inst, niveau='MP', classe=self.classe,
            annee_universitaire=self.annee, semestre=self.s3,
            portee=Deliberation.PORTEE_RATTRAPAGE, jury_source=annuel,
            seuil_validation=Decimal('10'))
        services.calculer_deliberation(rattrapage)

        grille = services.grille_rattrapage(rattrapage)
        inscrits = {l['inscription']: l for l in grille['lignes']}
        self.assertIn(auditeur.pk, inscrits, "l'auditeur doit avoir sa rangée")
        self.assertTrue(inscrits[auditeur.pk]['auditeur_libre'])
        self.assertFalse(inscrits[promotion[2].pk]['auditeur_libre'])

        services.saisir_rattrapage(rattrapage, [
            {'inscription': auditeur.pk, 'matiere': self.math.pk,
             'note_rattrapage': '14.00'}])
        note = Note.objects.get(inscription=auditeur, matiere=self.math)
        self.assertEqual(note.note_rattrapage, Decimal('14.00'))
        # max(session normale, rattrapage) — la règle ne change pas pour lui.
        self.assertEqual(note.note_retenue, Decimal('14.00'))
        self.assertFalse(
            LigneDeliberation.objects.filter(inscription=auditeur).exists())

    # ── 5. Le cas qui bloquait ──────────────────────────────────────────────
    def test_un_ancien_recu_au_cnim_peut_revenir_en_auditeur(self):
        """
        Le cas le plus courant, et celui que le contrôle de réinscription
        refusait : un ancien reçu au CNIM, parti à l'ESP, qui revient suivre la
        2e année pour viser une bourse.

        Son inscription précédente porte « Autorisé à passer le CNIM », or
        `controler_reinscription` exige « Redoublant » — il réclamait donc une
        dérogation pour un droit de redoublement qui n'est pas en jeu. Un
        auditeur ne redouble pas : il n'est pas étudiant.
        """
        from django.core.exceptions import ValidationError

        etudiant = Etudiant.objects.create(matricule='900010', nom='Revenant')
        passee = Classe.objects.create(
            nom='MP A', institution=self.inst, annee_universitaire='2024-2025',
            niveau=self.socle, groupe='A')
        InscriptionIPGEI.objects.create(
            etudiant=etudiant, classe=passee, annee_universitaire='2024-2025',
            statut=InscriptionIPGEI.STATUT_CNIM)

        # Tel quel, le contrôle refuse — c'est bien lui qu'il faut contourner.
        self.ref.redoublement_autorise = True
        self.ref.save(update_fields=['redoublement_autorise'])
        with self.assertRaises(ValidationError):
            services.controler_reinscription(etudiant, self.classe)

        # En auditeur, l'inscription passe : ni redoublement, ni dérogation.
        revenant = InscriptionIPGEI.objects.create(
            etudiant=etudiant, classe=self.classe,
            annee_universitaire=self.annee, auditeur_libre=True,
            statut=InscriptionIPGEI.STATUT_ACTIF, nb_redoublements=0,
            montant_frais=0)
        self.assertEqual(revenant.etudiant.matricule, '900010',
                         'il garde son matricule')
        self.assertEqual(revenant.montant_frais, 0, 'il ne paie pas')
        self.assertNotIn(revenant, InscriptionIPGEI.objects.cursus())
