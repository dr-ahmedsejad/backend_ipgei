"""
Test — dupliquer une grille ne crée pas de double affectation.

`dupliquer_grille` appelait `SeanceReelle.objects.create()` en direct. Django
n'exécute pas `full_clean()` sur ce chemin, et la règle « un enseignant, une
salle » vit dans le serializer : la duplication ne la traversait donc jamais.
Les contraintes de base ne rattrapent rien — elles protègent la CASE
(classe, semaine, jour, créneau, sous-groupe), pas l'enseignant ni la salle.

Le scénario reproduit ici : une séance ajoutée à la main occupe M. SEJAD en
MPSI B le lundi premier créneau ; on duplique ensuite la grille de MPSI A, qui
place le même enseignant au même moment. Avant correction : deux séances, un
enseignant, et ses heures comptées deux fois jusque dans la vacation.

Ce que le test fige :
  1. la case en collision est ÉCARTÉE, pas créée ;
  2. le reste de la duplication passe — une collision n'annule pas tout ;
  3. l'appelant reçoit de quoi le dire à l'utilisateur ;
  4. la même règle vaut pour une salle ;
  5. une séance ANNULÉE libère son créneau, comme le veut le serializer ;
  6. sans collision, rien ne change au comportement d'origine.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.db import connection
from django.test import TestCase

from apps.ipgei import services
from apps.ipgei.models import (
    Classe, GrilleType, Matiere, NiveauIPGEI, SeanceReelle, SeanceType,
    SemaineIPGEI, SemestreIPGEI,
)
from apps.parametres.models import Creneau, Institution, Jour, Niveau, Seance
from apps.salle.models import Salle
from apps.prof.models import Prof, ProfTypeHistory


class DuplicationSansDoubleAffectationTest(TestCase):
    @classmethod
    def setUpClass(cls):
        # `prof_type_history` naît d'une migration SQL brute, absente sous
        # `--no-migrations`. Même parade que `test_avancement_realise` : la
        # vérification d'existence avant `schema_editor` vaut pour SQLite
        # comme pour PostgreSQL.
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
        cls.annee = '2025-2026'
        cls.s1 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S1', annee_universitaire=cls.annee,
            date_debut=date(2025, 9, 1))
        cls.semaine = SemaineIPGEI.objects.create(
            semestre=cls.s1, numero=1, date_debut=date(2025, 9, 1),
            date_fin=date(2025, 9, 6), type_semaine=SemaineIPGEI.TYPE_COURS)

        cls.lundi = Jour.objects.create(jour='Lundi')
        cls.c1 = Creneau.objects.create(creneau='08:00-10:00', ordre=1)

        cls.prof = Prof.objects.create(nom='SEJAD', NNI=1234567890, type='permanent')
        cls.salle = Salle.objects.create(nom='A1')
        cls.matiere = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Mathématiques',
            code_semestre='S1', niveau_ref=cls.ref, coefficient=Decimal('4'))

        cls.mpsi_a = Classe.objects.create(nom='MPSI A', institution=cls.inst,
                                           annee_universitaire=cls.annee,
                                           niveau=cls.socle, groupe='A')
        cls.mpsi_b = Classe.objects.create(nom='MPSI B', institution=cls.inst,
                                           annee_universitaire=cls.annee,
                                           niveau=cls.socle, groupe='B')

    def setUp(self):
        self.grille = GrilleType.objects.create(
            classe=self.mpsi_a, type_semestre='I')

    def _case_type(self, prof=None, salle=None):
        return SeanceType.objects.create(
            grille=self.grille, jour=self.lundi, creneau=self.c1,
            matiere=self.matiere, prof=prof, salle=salle,
            type_seance=_type_cours())

    def _seance_voisine(self, prof=None, salle=None, annulee=False):
        """Ce que MPSI B occupe déjà sur le même créneau."""
        return SeanceReelle.objects.create(
            classe=self.mpsi_b, semaine=self.semaine, jour=self.lundi,
            creneau=self.c1, matiere=self.matiere, prof=prof, salle=salle,
            type_seance=_type_cours(), date=date(2025, 9, 1),
            origine=SeanceReelle.ORIGINE_MANUELLE, annulee=annulee)

    def _seances_de_a(self):
        return SeanceReelle.objects.filter(classe=self.mpsi_a)

    # ── Enseignant ──────────────────────────────────────────────────────────
    def test_un_enseignant_deja_pris_ailleurs_ecarte_la_case(self):
        self._case_type(prof=self.prof)
        self._seance_voisine(prof=self.prof)

        resultat = services.dupliquer_grille(self.grille, [self.semaine])

        self.assertEqual(self._seances_de_a().count(), 0,
                         "la case en collision ne doit pas être créée")
        self.assertEqual(resultat['conflits'], 1)
        self.assertEqual(resultat['creees'], 0)

    def test_le_conflit_est_nommable_a_l_utilisateur(self):
        self._case_type(prof=self.prof)
        self._seance_voisine(prof=self.prof)

        resultat = services.dupliquer_grille(self.grille, [self.semaine])

        detail = resultat['details_conflits'][0]
        self.assertIn('SEJAD', detail, "dire QUI est déjà pris")
        self.assertIn('MPSI B', detail, "dire OÙ il est pris")
        self.assertIn('Lundi', detail, "dire QUAND")

    def test_une_collision_n_annule_pas_le_reste_de_la_duplication(self):
        """Sur seize semaines, tout rejeter serait pire que la collision."""
        autre_creneau = Creneau.objects.create(creneau='10:00-12:00', ordre=2)
        self._case_type(prof=self.prof)                      # en collision
        SeanceType.objects.create(                           # sans collision
            grille=self.grille, jour=self.lundi, creneau=autre_creneau,
            matiere=self.matiere, prof=self.prof, type_seance=_type_cours())
        self._seance_voisine(prof=self.prof)

        resultat = services.dupliquer_grille(self.grille, [self.semaine])

        self.assertEqual(resultat['creees'], 1, 'la case libre doit passer')
        self.assertEqual(resultat['conflits'], 1)

    # ── Salle ───────────────────────────────────────────────────────────────
    def test_une_salle_deja_occupee_ecarte_aussi_la_case(self):
        self._case_type(salle=self.salle)
        self._seance_voisine(salle=self.salle)

        resultat = services.dupliquer_grille(self.grille, [self.semaine])

        self.assertEqual(self._seances_de_a().count(), 0)
        self.assertIn('A1', resultat['details_conflits'][0])

    # ── Séance annulée ──────────────────────────────────────────────────────
    def test_une_seance_annulee_libere_son_creneau(self):
        """La règle du serializer ignore les séances annulées : ici aussi."""
        self._case_type(prof=self.prof)
        self._seance_voisine(prof=self.prof, annulee=True)

        resultat = services.dupliquer_grille(self.grille, [self.semaine])

        self.assertEqual(resultat['creees'], 1)
        self.assertEqual(resultat['conflits'], 0)

    # ── Non-régression ──────────────────────────────────────────────────────
    def test_sans_collision_le_comportement_ne_change_pas(self):
        self._case_type(prof=self.prof, salle=self.salle)

        resultat = services.dupliquer_grille(self.grille, [self.semaine])

        self.assertEqual(resultat['creees'], 1)
        self.assertEqual(resultat['conflits'], 0)
        self.assertEqual(resultat['details_conflits'], [])
        seance = self._seances_de_a().get()
        self.assertEqual(seance.prof, self.prof)
        self.assertEqual(seance.origine, SeanceReelle.ORIGINE_GRILLE)

    def test_une_case_deja_posee_reste_ignoree_et_non_comptee_en_conflit(self):
        """`ignorees` et `conflits` disent deux choses différentes."""
        self._case_type(prof=self.prof)
        SeanceReelle.objects.create(
            classe=self.mpsi_a, semaine=self.semaine, jour=self.lundi,
            creneau=self.c1, matiere=self.matiere, type_seance=_type_cours(),
            date=date(2025, 9, 1), origine=SeanceReelle.ORIGINE_MANUELLE)

        resultat = services.dupliquer_grille(self.grille, [self.semaine])

        self.assertEqual(resultat['ignorees'], 1)
        self.assertEqual(resultat['conflits'], 0)


def _type_cours():
    """Le type « Cours » du référentiel, créé au besoin."""
    obj, _ = Seance.objects.get_or_create(type_seance='Cours')
    return obj
