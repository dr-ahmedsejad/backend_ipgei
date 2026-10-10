"""
Test — les fiches de pointage impriment « Cours » au lieu de « CM ».

Changement purement cosmétique, et volontairement confiné au gabarit : la
valeur tombe dans une cellule de tableau ordinaire, sans classe CSS. Rien
n'est touché côté Python, ni dans la charge utile, ni en base — « CM » reste
la clé partout où elle compte : familles d'avancement, équivalences horaires,
colonnes de volume de l'EM, recherche du taux de paiement.

Ce que les tests figent :

  1. « CM » s'imprime « Cours » sur les deux fiches ;
  2. les autres types gardent leur sigle ;
  3. aucun commentaire de gabarit ne fuit sur le document — `{# … #}` ne
     commente qu'UNE ligne, et la suite s'imprime (c'est arrivé le 10/10/2026
     sur l'emploi du temps). Le balayage général est dans
     `tests/test_commentaires_gabarits.py` ; ici on regarde le rendu.

Base SQLite en mémoire (siga.settings.test).
"""
from django.template.loader import render_to_string
from django.test import SimpleTestCase

INDIVIDUELLE = 'fiche_suivi_individuelle.html'
COLLECTIVE   = 'fiche_suivi_collective.html'


def rendu(gabarit, type_seance):
    """Le gabarit, avec une seule ligne de pointage portant ce type."""
    lignes = [{
        'jour': 'Lundi',
        'fiches': [{
            'jour': 'Lundi', 'creneau': '08h00-09h30',
            'prof_nom': 'Cheikh KHAIRY', 'tel_prof': '36656361',
            'em_intitule': 'Mathématiques', 'type_seance': type_seance,
            'salle_nom': 'A1', 'departement': 'MP', 'semestre': 'S3',
        }],
    }]
    # Les deux gabarits ne nomment pas la liste pareil : l'individuelle lit
    # `fiches_semaine`, la collective `fiches_tous_jours`. On fournit les deux.
    return render_to_string(gabarit, {
        'image_url': '', 'institution': None, 'annee_universitaire': '2026-2027',
        'semaine': 4, 'semestre': 'S3',
        'fiches_semaine': lignes, 'fiches_tous_jours': lignes,
    })


class FichesLibelleCoursTest(SimpleTestCase):
    def test_la_fiche_individuelle_imprime_cours(self):
        corps = rendu(INDIVIDUELLE, 'CM')
        self.assertIn('<td>Cours</td>', corps)
        self.assertNotIn('<td>CM</td>', corps)

    def test_la_fiche_collective_imprime_cours(self):
        corps = rendu(COLLECTIVE, 'CM')
        self.assertIn('<td>Cours</td>', corps)
        self.assertNotIn('<td>CM</td>', corps)

    def test_les_autres_types_gardent_leur_sigle(self):
        for gabarit in (INDIVIDUELLE, COLLECTIVE):
            for code in ('TD', 'TP', 'DS', 'EF'):
                with self.subTest(gabarit=gabarit, code=code):
                    self.assertIn(f'<td>{code}</td>', rendu(gabarit, code))

    def test_aucun_commentaire_ne_fuit_sur_le_document(self):
        for gabarit in (INDIVIDUELLE, COLLECTIVE):
            with self.subTest(gabarit=gabarit):
                corps = rendu(gabarit, 'CM').split('<body>', 1)[-1]
                self.assertNotIn('{#', corps)
                self.assertNotIn('#}', corps)
