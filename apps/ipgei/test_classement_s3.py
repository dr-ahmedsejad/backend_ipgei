"""
Test — le classement S3 (feuille d'affectation de deuxième année).

Ce que le test fige :
  1. `moyenne_classement = (moyenne de 1re année + moyenne S3) / 2` ;
  2. un étudiant dont la première année n'a AUCUNE note n'est pas classé —
     il ferme la marche, colonne vide. C'était le défaut : absents comptés
     zéro, une année vide valait 0,00 et non « rien », et l'étudiant se
     retrouvait classé à (0 + S3) / 2 ;
  3. seuls les inscrits de deuxième année figurent ;
  4. le paramètre `institution` filtre réellement (il était ignoré).

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.absence.models import Etudiant
from apps.ipgei import classement
from apps.ipgei.models import (
    Classe, InscriptionIPGEI, Matiere, NiveauIPGEI, Note, SemestreIPGEI,
)
from apps.parametres.models import Institution, Niveau


class ClassementS3Test(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.autre = Institution.objects.create(acronyme='AUT', nom='Autre Institut')
        cls.socle_mpsi = Niveau.objects.create(niveau='MPSI')
        cls.socle_mp   = Niveau.objects.create(niveau='MP')
        for inst in (cls.inst, cls.autre):
            NiveauIPGEI.objects.create(institution=inst, code='MPSI', libelle='MPSI', rang=1)
            NiveauIPGEI.objects.create(institution=inst, code='MP',   libelle='MP',   rang=2)

        def semestre(inst, code, annee):
            return SemestreIPGEI.objects.create(
                institution=inst, code=code, annee_universitaire=annee,
                date_debut=date(2024, 9, 1))

        cls.s1_24 = semestre(cls.inst, 'S1', '2024-2025')
        cls.s2_24 = semestre(cls.inst, 'S2', '2024-2025')
        cls.s3_25 = semestre(cls.inst, 'S3', '2025-2026')
        cls.s3_autre = semestre(cls.autre, 'S3', '2025-2026')

        ref = {n.institution_id: {} for n in NiveauIPGEI.objects.all()}
        for n in NiveauIPGEI.objects.all():
            ref[n.institution_id][n.code] = n

        def matiere(inst, code, code_semestre, niveau):
            return Matiere.objects.create(
                institution=inst, code=code, intitule=code, code_semestre=code_semestre,
                niveau_ref=ref[inst.pk][niveau], coefficient=Decimal(1))

        cls.m_s1 = matiere(cls.inst, 'MATH1', 'S1', 'MPSI')
        cls.m_s2 = matiere(cls.inst, 'MATH2', 'S2', 'MPSI')
        cls.m_s3 = matiere(cls.inst, 'MATH3', 'S3', 'MP')
        cls.m_s3_autre = matiere(cls.autre, 'MATH3', 'S3', 'MP')

        def classe(inst, nom, annee, niveau, groupe):
            return Classe.objects.create(nom=nom, institution=inst, annee_universitaire=annee,
                                         niveau=niveau, groupe=groupe)

        cls.mpsi_24 = classe(cls.inst, 'MPSI A', '2024-2025', cls.socle_mpsi, 'A')
        cls.mp_25   = classe(cls.inst, 'MP A',   '2025-2026', cls.socle_mp,   'A')
        cls.mpsi_25 = classe(cls.inst, 'MPSI A', '2025-2026', cls.socle_mpsi, 'A')
        cls.mp_autre = classe(cls.autre, 'MP A', '2025-2026', cls.socle_mp, 'A')

    def _inscrit(self, matricule, classe):
        etudiant, _ = Etudiant.objects.get_or_create(matricule=matricule,
                                                     defaults={'nom': f'Etudiant {matricule}'})
        return InscriptionIPGEI.objects.create(
            etudiant=etudiant, classe=classe, annee_universitaire=classe.annee_universitaire)

    def _note(self, inscription, matiere, semestre, valeur):
        Note.objects.update_or_create(
            inscription=inscription, matiere=matiere, semestre=semestre,
            defaults={'note_retenue': Decimal(valeur), 'coefficient': matiere.coefficient})

    def _ligne(self, lignes, matricule):
        return next(l for l in lignes if l['matricule'] == matricule)

    def test_la_moyenne_de_classement_est_la_moyenne_de_la_1re_annee_et_du_s3(self):
        premiere = self._inscrit('300001', self.mpsi_24)
        self._note(premiere, self.m_s1, self.s1_24, 10)
        self._note(premiere, self.m_s2, self.s2_24, 10)
        seconde = self._inscrit('300001', self.mp_25)
        self._note(seconde, self.m_s3, self.s3_25, 14)

        ligne = self._ligne(classement.lignes('2025-2026', institution=self.inst), '300001')
        self.assertEqual(ligne['moyenne_annee1'], Decimal('10.00'))
        self.assertEqual(ligne['moyenne_s3'], Decimal('14.00'))
        self.assertEqual(ligne['moyenne_classement'], Decimal('12.00'))
        self.assertEqual(ligne['rang'], 1)

    def test_une_premiere_annee_sans_aucune_note_ne_classe_pas(self):
        # Le défaut corrigé : inscription de 1re année bien présente, mais
        # jamais notée. Elle valait 0,00 et l'étudiant était classé à S3 / 2.
        self._inscrit('300002', self.mpsi_24)
        seconde = self._inscrit('300002', self.mp_25)
        self._note(seconde, self.m_s3, self.s3_25, 14)

        ligne = self._ligne(classement.lignes('2025-2026', institution=self.inst), '300002')
        self.assertIsNone(ligne['moyenne_annee1'], "l'établissement n'a pas cette note")
        self.assertIsNone(ligne['moyenne_classement'], 'une demi-moyenne passerait pour une vraie')
        self.assertIsNone(ligne['rang'], 'il ferme la marche, sans rang')

    def test_les_non_classes_ferment_la_marche(self):
        p1 = self._inscrit('300003', self.mpsi_24)
        self._note(p1, self.m_s1, self.s1_24, 8)
        self._note(p1, self.m_s2, self.s2_24, 8)
        s1 = self._inscrit('300003', self.mp_25)
        self._note(s1, self.m_s3, self.s3_25, 8)          # classé, moyenne 8

        self._inscrit('300004', self.mpsi_24)                # 1re année vide
        s2 = self._inscrit('300004', self.mp_25)
        self._note(s2, self.m_s3, self.s3_25, 19)         # excellent S3, mais non classable

        lignes = classement.lignes('2025-2026', institution=self.inst)
        self.assertEqual([l['matricule'] for l in lignes][:1], ['300003'])
        self.assertEqual(lignes[-1]['matricule'], '300004')
        self.assertIsNone(lignes[-1]['rang'])

    def test_seuls_les_inscrits_de_deuxieme_annee_figurent(self):
        self._inscrit('300005', self.mpsi_25)               # un MPSI de l'année classée
        seconde = self._inscrit('300006', self.mp_25)
        self._note(seconde, self.m_s3, self.s3_25, 12)

        matricules = {l['matricule'] for l in classement.lignes('2025-2026', institution=self.inst)}
        self.assertIn('300006', matricules)
        self.assertNotIn('300005', matricules, "un MPSI n'a rien à faire sur cette feuille")

    def test_le_parametre_institution_filtre_les_inscrits_et_le_semestre(self):
        chez_nous = self._inscrit('300007', self.mp_25)
        self._note(chez_nous, self.m_s3, self.s3_25, 12)
        ailleurs = self._inscrit('300008', self.mp_autre)
        self._note(ailleurs, self.m_s3_autre, self.s3_autre, 12)

        nous = {l['matricule'] for l in classement.lignes('2025-2026', institution=self.inst)}
        eux  = {l['matricule'] for l in classement.lignes('2025-2026', institution=self.autre)}
        self.assertEqual(nous, {'300007'})
        self.assertEqual(eux,  {'300008'})
        # Sans institution : tout le monde, comme avant — le comportement
        # historique n'est pas cassé, il est simplement devenu filtrable.
        tous = {l['matricule'] for l in classement.lignes('2025-2026')}
        self.assertEqual(tous, {'300007', '300008'})
