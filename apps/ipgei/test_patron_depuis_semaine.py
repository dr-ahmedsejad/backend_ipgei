"""
Test — reprendre une semaine réelle dans le patron, le sens inverse de la
duplication.

Le patron n'appartient à aucun semestre : il tient à la classe et au TYPE de
semestre. Rempli une fois, il resservira l'année suivante, là où une semaine
réelle meurt avec son année. C'est sa seule raison d'être — et personne ne
compose un patron à vide, on bâtit une semaine puis on la promeut.

Ce que le test fige :

  1. une séance ANNULÉE n'entre pas dans le modèle : on reprend un emploi du
     temps, pas l'histoire de ses accidents ;
  2. une PERMUTATION est ramenée à son état normal — elle est par définition
     une exception à la semaine ordinaire, et la recopier telle quelle
     graverait l'exception dans le modèle. On retient `prof_initial` plutôt
     que d'écarter la case, qui laisserait un trou ;
  3. sans `ecraser`, une case déjà composée dans le patron reste intacte : on
     complète un modèle sans défaire ce qu'on y a réglé à la main ;
  4. une semaine d'un semestre de l'autre parité est refusée — un modèle ne
     traverse pas les parités ;
  5. une semaine vide est refusée avec son motif, plutôt que de rendre zéro.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase

from apps.ipgei import services
from apps.ipgei.models import (
    Classe, GrilleType, NiveauIPGEI, SeanceReelle, SeanceType, SemaineIPGEI,
    SemestreIPGEI, SousGroupeTP, Matiere,
)
from apps.parametres.models import Creneau, Institution, Jour, Niveau, Seance
from apps.prof.models import Prof, ProfTypeHistory


class PatronDepuisSemaineTest(TestCase):

    @classmethod
    def setUpClass(cls):
        # `prof_type_history` naît d'une migration SQL brute, absente sous
        # `--no-migrations` — et un signal l'alimente à chaque création de
        # `Prof`. Même parade que `test_duplication_conflits`.
        if ProfTypeHistory._meta.db_table not in connection.introspection.table_names():
            with connection.schema_editor() as se:
                se.create_model(ProfTypeHistory)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MPSI')
        cls.ref = NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI',
                                             libelle='MPSI', rang=1)
        cls.classe = Classe.objects.create(
            nom='MPSI A', institution=cls.inst, annee_universitaire='2025-2026',
            niveau=cls.socle, groupe='A')
        cls.grille = GrilleType.objects.create(
            classe=cls.classe, type_semestre='I', libelle='Grille MPSI A')
        cls.s1 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S1', annee_universitaire='2025-2026',
            date_debut=date(2025, 10, 6))
        cls.s2 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S2', annee_universitaire='2025-2026',
            date_debut=date(2026, 1, 26))
        cls.semaine = SemaineIPGEI.objects.create(
            semestre=cls.s1, numero=1, date_debut=date(2025, 10, 6),
            date_fin=date(2025, 10, 11), type_semaine=SemaineIPGEI.TYPE_COURS)
        cls.semaine_paire = SemaineIPGEI.objects.create(
            semestre=cls.s2, numero=1, date_debut=date(2026, 1, 26),
            date_fin=date(2026, 1, 31), type_semaine=SemaineIPGEI.TYPE_COURS)

        cls.lundi = Jour.objects.create(jour='Lundi')
        cls.matin = Creneau.objects.create(creneau='08h00-09h30', ordre=1)
        cls.tantot = Creneau.objects.create(creneau='09h45-11h15', ordre=2)
        cls.type_cours = Seance.objects.create(type_seance='CM')
        cls.matiere = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Mathématiques',
            code_semestre='S1', niveau_ref=cls.ref)
        cls.titulaire = Prof.objects.create(
            nom='TITULAIRE', NNI=1000000001, type='permanent')
        cls.remplacant = Prof.objects.create(
            nom='REMPLACANT', NNI=1000000002, type='permanent')

    def _seance(self, creneau, **extra):
        champs = dict(
            classe=self.classe, semaine=self.semaine, jour=self.lundi,
            creneau=creneau, matiere=self.matiere, type_seance=self.type_cours,
            prof=self.titulaire, date=self.semaine.date_debut)
        champs.update(extra)
        return SeanceReelle.objects.create(**champs)

    # ── 1. Le report ordinaire ──────────────────────────────────────────────
    def test_la_semaine_devient_le_patron(self):
        self._seance(self.matin)
        self._seance(self.tantot)

        r = services.alimenter_patron_depuis_semaine(self.grille, self.semaine)

        self.assertEqual(r['creees'], 2)
        self.assertEqual(self.grille.seances.count(), 2)
        # Le patron ne porte PAS de date : c'est ce qui lui permet de resservir.
        modele = self.grille.seances.first()
        self.assertFalse(hasattr(modele, 'date'),
                         "une case de patron n'a pas de date")

    def test_une_seance_annulee_n_entre_pas(self):
        self._seance(self.matin)
        self._seance(self.tantot, annulee=True)

        r = services.alimenter_patron_depuis_semaine(self.grille, self.semaine)
        self.assertEqual(r['creees'], 1)
        self.assertEqual(
            self.grille.seances.first().creneau_id, self.matin.pk)

    def test_les_sous_groupes_gardent_leur_case(self):
        """Deux sous-groupes d'un même créneau font deux cases, pas une."""
        a = SousGroupeTP.objects.create(classe=self.classe, groupe='A1')
        b = SousGroupeTP.objects.create(classe=self.classe, groupe='A2')
        self._seance(self.matin, sous_groupe=a)
        self._seance(self.matin, sous_groupe=b)

        r = services.alimenter_patron_depuis_semaine(self.grille, self.semaine)
        self.assertEqual(r['creees'], 2)

    # ── 2. La permutation ramenée à la normale ──────────────────────────────
    def test_une_permutation_revient_au_titulaire(self):
        """
        Une permutation est une exception à la semaine ordinaire. La recopier
        telle quelle graverait l'exception dans le modèle ; l'écarter y
        laisserait un trou. On retient donc l'enseignant d'origine.
        """
        self._seance(self.matin, prof=self.remplacant,
                     prof_initial=self.titulaire,
                     origine=SeanceReelle.ORIGINE_PERMUTATION)

        r = services.alimenter_patron_depuis_semaine(self.grille, self.semaine)

        self.assertEqual(r['permutations_normalisees'], 1,
                         'et cela doit se DIRE : le modèle ne reproduit pas '
                         'exactement la semaine affichée')
        self.assertEqual(self.grille.seances.first().prof_id, self.titulaire.pk)

    def test_une_permutation_sans_prof_initial_garde_le_prof_effectif(self):
        """Faute de titulaire connu, on ne peut que reprendre ce qu'on voit."""
        self._seance(self.matin, prof=self.remplacant,
                     origine=SeanceReelle.ORIGINE_PERMUTATION)

        r = services.alimenter_patron_depuis_semaine(self.grille, self.semaine)
        self.assertEqual(r['permutations_normalisees'], 0)
        self.assertEqual(self.grille.seances.first().prof_id, self.remplacant.pk)

    # ── 3. Ce que « écraser » commande ──────────────────────────────────────
    def test_sans_ecraser_le_patron_existant_tient(self):
        SeanceType.objects.create(
            grille=self.grille, jour=self.lundi, creneau=self.matin,
            matiere=self.matiere, type_seance=self.type_cours,
            prof=self.titulaire)
        self._seance(self.matin, prof=self.remplacant)
        self._seance(self.tantot)

        r = services.alimenter_patron_depuis_semaine(self.grille, self.semaine)

        self.assertEqual(r['ignorees'], 1)
        self.assertEqual(r['creees'], 1)
        self.assertEqual(
            self.grille.seances.get(creneau=self.matin).prof_id,
            self.titulaire.pk, 'la case composée à la main ne bouge pas')

    def test_avec_ecraser_le_patron_suit_la_semaine(self):
        SeanceType.objects.create(
            grille=self.grille, jour=self.lundi, creneau=self.matin,
            matiere=self.matiere, type_seance=self.type_cours,
            prof=self.titulaire)
        self._seance(self.matin, prof=self.remplacant)

        r = services.alimenter_patron_depuis_semaine(self.grille, self.semaine,
                                                     ecraser=True)
        self.assertEqual(r['remplacees'], 1)
        self.assertEqual(
            self.grille.seances.get(creneau=self.matin).prof_id,
            self.remplacant.pk)

    # ── 4. Les refus motivés ────────────────────────────────────────────────
    def test_une_semaine_de_l_autre_parite_est_refusee(self):
        """Le patron tient au TYPE de semestre : impair ne reprend pas pair."""
        SeanceReelle.objects.create(
            classe=self.classe, semaine=self.semaine_paire, jour=self.lundi,
            creneau=self.matin, matiere=self.matiere,
            type_seance=self.type_cours, date=self.semaine_paire.date_debut)

        with self.assertRaises(ValidationError) as cas:
            services.alimenter_patron_depuis_semaine(
                self.grille, self.semaine_paire)
        self.assertIn('parité', str(cas.exception))

    def test_une_semaine_vide_est_refusee(self):
        with self.assertRaises(ValidationError) as cas:
            services.alimenter_patron_depuis_semaine(self.grille, self.semaine)
        self.assertIn('aucune séance', str(cas.exception))
