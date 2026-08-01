"""
Test de non-régression — les crédits/moyenne ANNUELS d'un niveau ne comptent que
les semestres DU niveau (S{2n-1}, S{2n}).

Régression corrigée : un redoublant dont l'inscription de niveau N porte aussi
des semestres d'un niveau INFÉRIEUR (ex. rattrapage de L1 — S1/S2 — sous
l'inscription L2) voyait ces crédits ajoutés à son total annuel (> 60 crédits),
avec `passage_droit` accordé à tort (ex. PV SEA L2 : un étudiant à 120 crédits
au lieu de 60). `calculer_moyenne_annuelle` borne désormais au niveau délibéré.

La consolidation par semestre est mockée (couverte par tests/test_calcul_notes).

Exécution :  pytest apps/evaluations/test_credits_annuels_niveau.py
"""
from unittest.mock import patch

from django.test import TestCase

from apps.parametres.models import Year, Niveau, Semestre, Institution
from apps.departement.models import Departement
from apps.scolarite.models import Filiere
from apps.absence.models import Etudiant
from apps.inscriptions.models import InscriptionAdministrative, InscriptionPedagogique
from apps.evaluations.services.calcul_notes import NoteCalculService

_CONSO = 'apps.documents.services.calculer_resultat_semestre_consolide'


class CreditsAnnuelsBorneNiveauTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='INS', nom='Institut', est_principale=True)
        cls.y2024 = Year.objects.create(annee='2024-2025')
        cls.n1 = Niveau.objects.create(niveau='1')
        cls.n2 = Niveau.objects.create(niveau='2')
        cls.dept = Departement.objects.create(nom='Dept', institution=cls.inst)
        cls.fil = Filiere.objects.create(
            code='SEA', intitule_fr='SEA', type_diplome='LP', institution=cls.inst,
        )
        cls.etu = Etudiant.objects.create(
            matricule='E1', nom='Etu', departement=cls.dept, genre='M',
        )
        cls.s1 = Semestre.objects.create(code_semestre='S1', semestre='S1', niveau_semestre=cls.n1, type_semestre='I', credits=30)
        cls.s2 = Semestre.objects.create(code_semestre='S2', semestre='S2', niveau_semestre=cls.n1, type_semestre='P', credits=30)
        cls.s3 = Semestre.objects.create(code_semestre='S3', semestre='S3', niveau_semestre=cls.n2, type_semestre='I', credits=30)
        cls.s4 = Semestre.objects.create(code_semestre='S4', semestre='S4', niveau_semestre=cls.n2, type_semestre='P', credits=30)

    @staticmethod
    def _conso(mapping):
        """side_effect mockant la consolidation : crédits par id de semestre."""
        def _f(etu, sem, annee):
            cv = mapping.get(sem.id, 0)
            return {
                'credits_valides':  cv,
                'moyenne_semestre': 12.0 if cv else None,
                'est_admis':        cv >= 30,
            }
        return _f

    def _insc_l2(self, semestres):
        insc = InscriptionAdministrative.objects.create(
            etudiant=self.etu, annee_univ=self.y2024, filiere=self.fil,
            institution=self.inst, niveau=2, numero_inscription='L2-2024',
        )
        for s in semestres:
            InscriptionPedagogique.objects.create(inscription_admin=insc, semestre=s)
        return insc

    def test_l2_borne_a_s3_s4_ignore_rattrapage_l1(self):
        """Inscription L2 portant S1,S2,S3,S4 (rattrapage L1) → crédits annuels
        L2 = S3+S4 (60), surtout PAS 120."""
        self._insc_l2([self.s1, self.s2, self.s3, self.s4])
        with patch(_CONSO, side_effect=self._conso({
            self.s1.id: 30, self.s2.id: 30, self.s3.id: 30, self.s4.id: 30,
        })):
            _moy, credits, _tous = NoteCalculService.calculer_moyenne_annuelle(
                self.etu, self.y2024, 2,
            )
        self.assertEqual(credits, 60)

    def test_l2_partielle_reste_sous_60(self):
        """L2 partielle (S3=18, S4=30) sous une inscription portant aussi S1/S2
        validés → 48 (pas 108) : passage conditionnel, pas de droit."""
        self._insc_l2([self.s1, self.s2, self.s3, self.s4])
        with patch(_CONSO, side_effect=self._conso({
            self.s1.id: 30, self.s2.id: 30, self.s3.id: 18, self.s4.id: 30,
        })):
            _moy, credits, _tous = NoteCalculService.calculer_moyenne_annuelle(
                self.etu, self.y2024, 2,
            )
        self.assertEqual(credits, 48)
