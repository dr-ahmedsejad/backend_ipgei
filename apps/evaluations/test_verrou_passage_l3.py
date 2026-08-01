"""
Test de non-régression — verrou de passage L3 / S5
(DeliberationAnnuelleService._credits_capitalises_niveau).

Le verrou compte les crédits capitalisés d'un niveau (ex. L1) via la
consolidation PAR SEMESTRE (calculer_resultat_semestre_consolide : max SN/SR
Art. 18 + report des EM capitalisés des années antérieures) — le MÊME moteur que
les crédits annuels (calculer_moyenne_annuelle) et le relevé.

Régression corrigée : un redoublant peut valider les semestres d'un niveau sous
l'inscription d'un AUTRE niveau (ex. S1/S2 portés par l'inscription L2 d'un
étudiant en L2). L'ancien code ne lisait que les inscriptions de niveau=N des
années STRICTEMENT antérieures (et un ResultatSemestre stocké par tentative) :
il ratait cette validation et verrouillait à tort des étudiants ayant pourtant
validé leur L1, alors que leurs crédits ANNUELS, eux, comptaient déjà ces
semestres (incohérence verrou ↔ crédits).

La consolidation elle-même est couverte par tests/test_calcul_notes.py ; ici on
la MOCKE pour tester la sélection/agrégation des semestres du niveau.

Exécution :  pytest apps/evaluations/test_verrou_passage_l3.py
Base SQLite en mémoire (siga.settings.test) — aucun contact avec la BD réelle.
"""
from unittest.mock import patch

from django.test import TestCase

from apps.parametres.models import Year, Niveau, Semestre, Institution
from apps.departement.models import Departement
from apps.scolarite.models import Filiere
from apps.absence.models import Etudiant
from apps.inscriptions.models import InscriptionAdministrative, InscriptionPedagogique
from apps.evaluations.services.deliberation_annuelle import (
    DeliberationAnnuelleService, DeliberationAnnuelleIngenieur,
)

# Cible du mock : import paresseux fait DANS _credits_capitalises_niveau via
# `from apps.documents.services import calculer_resultat_semestre_consolide`.
_CONSO = 'apps.documents.services.calculer_resultat_semestre_consolide'


class VerrouPassageL3Test(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.instA = Institution.objects.create(acronyme='INSA', nom='Institut A', est_principale=True)
        cls.instB = Institution.objects.create(acronyme='INSB', nom='Institut B', est_principale=False)
        cls.y2023 = Year.objects.create(annee='2023-2024')
        cls.y2024 = Year.objects.create(annee='2024-2025')
        cls.niveau1 = Niveau.objects.create(niveau='1')
        cls.dept = Departement.objects.create(nom='Dept A', institution=cls.instA)
        cls.filiere = Filiere.objects.create(
            code='LP', intitule_fr='Licence', type_diplome='LP', institution=cls.instA,
        )
        cls.etu = Etudiant.objects.create(
            matricule='ETU-V', nom='Etudiant Verrou', departement=cls.dept, genre='M',
        )
        # Semestres L1 (S1, S2) + un S2 du niveau 2 (S3) + un homonyme de S1.
        cls.s1 = Semestre.objects.create(
            code_semestre='S1', semestre='Semestre 1', niveau_semestre=cls.niveau1,
            type_semestre='I', credits=30,
        )
        cls.s2 = Semestre.objects.create(
            code_semestre='S2', semestre='Semestre 2', niveau_semestre=cls.niveau1,
            type_semestre='P', credits=30,
        )
        cls.s3 = Semestre.objects.create(
            code_semestre='S3', semestre='Semestre 3', niveau_semestre=cls.niveau1,
            type_semestre='I', credits=30,
        )

    def _insc(self, year, institution, niveau, semestres):
        """Inscription (niveau quelconque) portant les `semestres` donnés."""
        insc = InscriptionAdministrative.objects.create(
            etudiant=self.etu, annee_univ=year, filiere=self.filiere,
            institution=institution, niveau=niveau,
            numero_inscription=f'{niveau}-{institution.acronyme}-{year.annee}-{len(semestres)}',
        )
        for sem in semestres:
            InscriptionPedagogique.objects.create(inscription_admin=insc, semestre=sem)
        return insc

    @staticmethod
    def _conso(mapping):
        """side_effect mockant la consolidation : crédits par id de semestre."""
        return lambda etu, sem, annee: {'credits_valides': mapping.get(sem.id, 0)}

    def test_l1_validee_sous_inscription_l2_compte(self):
        """Régression verrou L3 : un redoublant valide S1/S2 sous son inscription
        L2 de l'année courante → ces crédits L1 DOIVENT compter (verrou levé).
        L'ancien code, borné aux inscriptions niveau=1 antérieures, renvoyait < 60."""
        self._insc(self.y2023, self.instA, 1, [self.s1, self.s2])           # L1 tentée
        self._insc(self.y2024, self.instA, 2, [self.s1, self.s2, self.s3])  # L2 portant S1/S2
        with patch(_CONSO, side_effect=self._conso({self.s1.id: 30, self.s2.id: 30, self.s3.id: 30})):
            credits = DeliberationAnnuelleService._credits_capitalises_niveau(
                self.etu, self.y2024, 1, institution=self.instA,
            )
        self.assertEqual(credits, 60)   # S1(30)+S2(30) ; S3 (niveau 2) ignoré

    def test_scoping_institution(self):
        """L1 uniquement à instB ; délibération à instA → ne compte pas (0)."""
        self._insc(self.y2023, self.instB, 1, [self.s1, self.s2])
        with patch(_CONSO, side_effect=self._conso({self.s1.id: 30, self.s2.id: 30})):
            credits = DeliberationAnnuelleService._credits_capitalises_niveau(
                self.etu, self.y2024, 1, institution=self.instA,
            )
        self.assertEqual(credits, 0)

    def test_meme_semestre_compte_une_fois(self):
        """Le même objet-semestre porté par plusieurs inscriptions (redoublant :
        S1 tenté en L1 puis re-porté sous l'inscription L2) n'est compté QU'UNE
        fois (dédup par id ; la consolidation couvre déjà toutes les tentatives)."""
        self._insc(self.y2023, self.instA, 1, [self.s1])
        self._insc(self.y2024, self.instA, 2, [self.s1])
        with patch(_CONSO, side_effect=self._conso({self.s1.id: 30})):
            credits = DeliberationAnnuelleService._credits_capitalises_niveau(
                self.etu, self.y2024, 1, institution=self.instA,
            )
        self.assertEqual(credits, 30)   # S1 compté une seule fois, pas 60

    def test_l1_incomplete_verrou_maintenu(self):
        """L1 partielle (S1=30, S2=18 → 48 < 60) → verrou maintenu (comportement sûr)."""
        self._insc(self.y2024, self.instA, 2, [self.s1, self.s2])
        with patch(_CONSO, side_effect=self._conso({self.s1.id: 30, self.s2.id: 18})):
            credits = DeliberationAnnuelleService._credits_capitalises_niveau(
                self.etu, self.y2024, 1, institution=self.instA,
            )
        self.assertEqual(credits, 48)

    def test_ingenieur_delegue_meme_logique(self):
        """Le verrou S5 Ingénieur (_credits_s1_s2) délègue à la même logique."""
        self._insc(self.y2024, self.instA, 2, [self.s1, self.s2])
        with patch(_CONSO, side_effect=self._conso({self.s1.id: 30, self.s2.id: 30})):
            credits = DeliberationAnnuelleIngenieur._credits_s1_s2(
                self.etu, self.y2024, self.instA,
            )
        self.assertEqual(credits, 60)
