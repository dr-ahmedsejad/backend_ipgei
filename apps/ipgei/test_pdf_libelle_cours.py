"""
Test — le PDF de l'emploi du temps imprime « Cours » et garde sa couleur.

Changement purement cosmétique, et le piège tient en un mot : dans ce gabarit,
le type de séance sert À LA FOIS de texte et de CLASSE CSS. C'est
`.session-type.CM` qui porte le bleu, `.TD` le vert, `.TP` l'orange. Écrire
« Cours » dans la classe donnerait `session-type Cours`, que la feuille de
style ignore — pastille sans bordure ni couleur.

Les deux sont donc séparés dans le gabarit, et c'est cela qu'on fige ici :
la classe reste `CM`, le texte devient « Cours », et les autres types ne
bougent pas.

Base SQLite en mémoire (siga.settings.test).
"""
from django.template.loader import render_to_string
from django.test import SimpleTestCase

GABARIT = 'ipgei/emploi_pdf.html'


def rendu(type_seance):
    """Le gabarit, avec une seule case portant ce type."""
    return render_to_string(GABARIT, {
        'image_url': '', 'institution': None, 'annee_universitaire': '',
        'semaine': {'numero': 4}, 'creneaux': [],
        'rows': [{
            'jour': 'Lundi',
            'cells': [{
                'colspan': 1, 'epreuve': None,
                'blocs': [{
                    'type_seance': type_seance,
                    'type_seance_is_special': False,
                    'est_evaluation': False, 'est_speciale': False,
                    'em_code': 'MATH', 'em_intitule': 'Mathématiques',
                    'lignes': [],
                }],
            }],
        }],
    })


class PdfLibelleCoursTest(SimpleTestCase):
    def test_la_classe_css_garde_le_code(self):
        """Sans quoi la pastille perdrait sa couleur — tout l'enjeu."""
        self.assertIn('class="session-type CM"', rendu('CM'))

    def test_le_texte_imprime_est_cours(self):
        compact = ''.join(rendu('CM').split())
        self.assertIn('>Cours<', compact)
        self.assertNotIn('>CM<', compact, 'le sigle ne doit plus être imprimé')

    def test_aucun_commentaire_ne_fuit_sur_le_document(self):
        """`{# … #}` ne commente qu'UNE ligne — la suite s'imprime.

        C'est arrivé ici même : un commentaire de quatre lignes posé juste
        au-dessus de la pastille a envoyé trois lignes d'explication sur
        l'emploi du temps distribué. Le garde-fou général est dans
        `tests/test_commentaires_gabarits.py` ; celui-ci regarde le document
        rendu, qui est ce que les étudiants ont sous les yeux.
        """
        # On regarde le CORPS seulement : `.session-type.CM` est une règle
        # CSS légitime de l'en-tête, et l'y chercher ferait échouer le test
        # pour une bonne raison — c'était mon premier essai.
        corps = rendu('CM').split('<body>', 1)[1]
        for trace in ('{#', '#}', "RIEN D'AUTRE", 'Le reste de'):
            self.assertNotIn(trace, corps,
                             f'« {trace} » ne doit pas figurer sur le document')

    def test_les_autres_types_sont_intacts(self):
        for code in ('TD', 'TP', 'DS'):
            with self.subTest(code=code):
                compact = ''.join(rendu(code).split())
                self.assertIn(f'class="session-type{code}"', compact)
                self.assertIn(f'>{code}<', compact)
