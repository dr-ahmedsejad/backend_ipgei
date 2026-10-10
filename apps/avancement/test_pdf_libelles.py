"""
Test — les libellés du PDF d'avancement des matières.

Trois renommages d'AFFICHAGE, tous confinés au gabarit :

  1. « Avancement des Éléments de Module (EMs) » → « Avancement des matières » ;
  2. l'en-tête « Code EM » → « Code matière » ;
  3. les trois colonnes « CM » → « Cours ».

Rien n'est touché ailleurs. « CM » reste la clé qui range les heures en
familles, les pondère (CM = 1, TD et TP = 2/3) et sert à retrouver le taux de
paiement ; `code_em` reste le champ lu dans les données. Seuls les mots
imprimés changent.

Le gabarit porte trois blocs de colonnes — planification, réalisation,
pourcentage — d'où les trois « Cours » attendus.

Base SQLite en mémoire (siga.settings.test).
"""
from pathlib import Path

from django.template.loader import render_to_string
from django.test import SimpleTestCase

GABARITS = Path(__file__).resolve().parent.parent.parent / 'templates'

GABARIT = 'avancement_em_pdf.html'


def rendu():
    return render_to_string(GABARIT, {
        'image_url': '', 'institution': None,
        'annee_universitaire': '2026-2027', 'semaine': 4,
        'rows': [{
            'code_em': 'MATH-S3', 'intitule': 'Mathématiques',
            'semestre': 'S3',
            'plan_CM': 30, 'plan_TD': 15, 'plan_TP': 0, 'plan_PR': 0,
            'real_CM': 12, 'real_TD': 6, 'real_TP': 0, 'real_PR': 0,
            'pct_CM': 40, 'pct_TD': 40, 'pct_TP': 0, 'pct_PR': 0,
        }],
    })


class LibellesPdfAvancementTest(SimpleTestCase):
    def test_le_titre_parle_de_matieres(self):
        html = rendu()
        self.assertIn('Avancement des matières', html)
        self.assertNotIn('Éléments de Module', html)
        self.assertNotIn('(EMs)', html)

    def test_l_entete_dit_code_matiere(self):
        html = rendu()
        self.assertIn('Code matière', html)
        self.assertNotIn('>Code EM<', html)

    def test_les_trois_colonnes_disent_cours(self):
        """Un bloc par famille : planification, réalisation, pourcentage."""
        html = rendu()
        self.assertEqual(html.count('>Cours<'), 3, html.count('>Cours<'))
        self.assertNotIn('>CM<', html, 'le sigle ne doit plus être imprimé')

    def test_les_autres_familles_gardent_leur_sigle(self):
        html = rendu()
        for code in ('TD', 'TP'):
            self.assertIn(f'>{code}<', html)

    def test_aucun_commentaire_ne_fuit_sur_le_document(self):
        """`{# … #}` ne commente qu'UNE ligne — la suite s'imprime."""
        corps = rendu().split('<body>', 1)[-1]
        self.assertNotIn('{#', corps)
        self.assertNotIn('#}', corps)


class AutresPdfAvancementTest(SimpleTestCase):
    """Les quatre autres documents d'avancement, même règle.

    Le type de séance s'écrit « Cours » ; l'UNITÉ DE COMPTE — l'heure
    équivalent CM — garde son sigle. « Total éq. Cours » se lirait mal, et
    ce n'est pas un type de séance mais une conversion.
    """

    def _rendu(self, gabarit, contexte=None):
        base = {
            'image_url': '', 'institution': None,
            'annee_universitaire': '2026-2027', 'semaine': 4,
            'rows': [], 'items': [], 'all_acronyms': [],
            'totaux_globaux': {},
        }
        base.update(contexte or {})
        return render_to_string(gabarit, base)

    def test_la_pastille_des_fiches_prof_et_detail_dit_cours(self):
        """Le texte change ; la classe `badge-cm`, qui porte la couleur, non."""
        for gabarit in ('avancement_profs_pdf.html',
                        'avancement_details_pdf.html'):
            with self.subTest(gabarit=gabarit):
                # Lu dans la SOURCE : ces deux gabarits n'impriment la
                # pastille qu'au sein d'une boucle sur des lignes réelles,
                # qu'un rendu à vide ne produit pas.
                source = (GABARITS / gabarit).read_text(encoding='utf-8')
                self.assertIn('<span class="badge badge-cm">Cours</span>', source)
                self.assertNotIn('>CM</span>', source)

    def test_la_colonne_de_type_des_permanents_dit_cours(self):
        html = self._rendu('avancement_permanents_pdf.html')
        self.assertIn('<th rowspan="2">Cours</th>', html)
        self.assertIn('<th rowspan="2">TD</th>', html, 'les autres ne bougent pas')

    def test_l_unite_de_compte_garde_son_sigle(self):
        """« éq. CM » est une conversion, pas un type : on n'y touche pas."""
        html = self._rendu('avancement_permanents_pdf.html')
        self.assertIn('Total éq. CM', html)

    def test_le_detail_par_prof_dit_cours_et_code_matiere(self):
        html = self._rendu('avancement_prof_detail_pdf.html')
        self.assertEqual(html.count('>Cours<'), 3, 'un par bloc de colonnes')
        self.assertIn('Code matière', html)
        self.assertNotIn('>Code EM<', html)
        self.assertIn('Éq. CM', html, "l'unité de compte reste")

    def test_aucun_commentaire_ne_fuit_sur_ces_documents(self):
        for gabarit in ('avancement_permanents_pdf.html',
                        'avancement_prof_detail_pdf.html'):
            with self.subTest(gabarit=gabarit):
                corps = self._rendu(gabarit).split('<body>', 1)[-1]
                self.assertNotIn('{#', corps)
                self.assertNotIn('#}', corps)
