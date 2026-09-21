"""
Test — rang de PROMOTION, non tassé, sur les PV annuel et de rattrapage (`pv._poser_rang_promotion_annuel`).

Ce que le test fige, à l'image des PV que l'IPGEI édite :
  1. le rang couvre toutes les classes du niveau, pas la seule classe du jury ;
  2. il n'est PAS tassé : deux 2ᵉ ex æquo, puis un 4ᵉ ;
  3. l'égalité se lit au centième publié ;
  4. un PV de rattrapage classe sur la moyenne finale (ligne de rattrapage si
     elle existe, sinon l'annuelle) ; un PV annuel sur les lignes annuelles ;
  5. un inscrit sans jury est calculé à la règle du jury ;
  6. l'auditeur libre n'entre pas dans l'effectif ;
  7. le PDF (contexte) et l'Excel impriment ce rang, pas le rang de classe.
"""
from io import BytesIO

import openpyxl

from apps.ipgei import pv
from apps.ipgei.models import Deliberation, LigneDeliberation
from apps.ipgei.test_regles_jury_et_cloture import _Base
from decimal import Decimal


class RangPromotionAnnuelTest(_Base):
    def _jury(self, classe, portee=Deliberation.PORTEE_ANNUELLE, source=None):
        return Deliberation.objects.create(
            institution=self.inst, niveau='MPSI', classe=classe,
            annee_universitaire=self.annee, portee=portee, jury_source=source,
            statut=Deliberation.STATUT_CALCULEE)

    def _ligne(self, jury, inscription, moyenne, rang=None):
        return LigneDeliberation.objects.create(
            deliberation=jury, inscription=inscription,
            moyenne_generale=Decimal(moyenne), rang=rang)

    def setUp(self):
        self.a1 = self._inscrit('800001', self.mpsi_a)
        self.a2 = self._inscrit('800002', self.mpsi_a)
        self.a3 = self._inscrit('800003', self.mpsi_a)
        self.b1 = self._inscrit('800011', self.mpsi_b)
        self.b2 = self._inscrit('800012', self.mpsi_b)
        self.ja = self._jury(self.mpsi_a)
        self.jb = self._jury(self.mpsi_b)
        # Promotion : B1 15 · A1 12.50 · B2 12.50 · A2 12.00 · A3 9
        self._ligne(self.ja, self.a1, '12.50', rang=1)
        self._ligne(self.ja, self.a2, '12.00', rang=2)
        self._ligne(self.ja, self.a3, '9.00', rang=3)
        self._ligne(self.jb, self.b1, '15.00', rang=1)
        self._ligne(self.jb, self.b2, '12.50', rang=2)

    def _rangs(self, jury):
        ctx = pv.contexte(jury)
        return ({l.inscription.etudiant.matricule: l.rang_promotion for l in ctx['lignes']},
                ctx['effectif_promotion'])

    def test_rang_sur_la_promotion_non_tasse(self):
        rangs, effectif = self._rangs(self.ja)
        self.assertEqual(effectif, 5)
        # B1 1er ; A1 et B2 ex æquo 2ᵉ ; A2 4ᵉ (pas 3ᵉ) ; A3 5ᵉ.
        self.assertEqual(rangs, {'800001': 2, '800002': 4, '800003': 5})
        self.assertEqual(self._rangs(self.jb)[0], {'800011': 1, '800012': 2})

    def test_egalite_au_centieme(self):
        LigneDeliberation.objects.filter(inscription=self.b2).update(moyenne_generale=Decimal('12.00'))
        LigneDeliberation.objects.filter(inscription=self.a2).update(moyenne_generale=Decimal('12.00'))
        rangs, _ = self._rangs(self.ja)
        self.assertEqual(rangs['800002'], 3)

    def test_rattrapage_classe_sur_la_moyenne_finale(self):
        ratt_a = self._jury(self.mpsi_a, Deliberation.PORTEE_RATTRAPAGE, self.ja)
        self._ligne(ratt_a, self.a3, '12.75')
        # B2 remonte aussi, par le rattrapage de SA classe : c'est sa moyenne
        # finale (13) qui compte, pas son annuelle (12,50).
        ratt_b = self._jury(self.mpsi_b, Deliberation.PORTEE_RATTRAPAGE, self.jb)
        self._ligne(ratt_b, self.b2, '13.00')
        rangs, _ = self._rangs(ratt_a)
        self.assertEqual(rangs, {'800003': 3})
        # Le PV ANNUEL, lui, reste sur les moyennes annuelles.
        annuel, _ = self._rangs(self.ja)
        self.assertEqual(annuel, {'800001': 2, '800002': 4, '800003': 5})

    def test_inscrit_sans_jury_calcule(self):
        b3 = self._inscrit('800013', self.mpsi_b)
        self._note(b3, self.math1, self.s1, '20')
        self._note(b3, self.math2, self.s2, '20')
        rangs, effectif = self._rangs(self.ja)
        self.assertEqual(effectif, 6)
        self.assertEqual(rangs['800001'], 3)

    def test_auditeur_libre_hors_effectif(self):
        aud = self._inscrit('800099', self.mpsi_b)
        aud.auditeur_libre = True
        aud.save(update_fields=['auditeur_libre'])
        self._note(aud, self.math1, self.s1, '20')
        self._note(aud, self.math2, self.s2, '20')
        rangs, effectif = self._rangs(self.ja)
        self.assertEqual(effectif, 5)
        self.assertEqual(rangs['800001'], 2)

    def test_excel_imprime_le_rang_de_promotion(self):
        classeur = openpyxl.load_workbook(BytesIO(pv.generer_excel(self.ja)))
        feuille = classeur['Résultats']
        lignes = list(feuille.iter_rows(values_only=True))
        entete = next(i for i, l in enumerate(lignes) if l and l[0] == 'Rang promotion')
        valeurs = {l[1]: l[0] for l in lignes[entete + 1:] if l and l[1]}
        self.assertEqual(valeurs, {'800001': 2, '800002': 4, '800003': 5})
