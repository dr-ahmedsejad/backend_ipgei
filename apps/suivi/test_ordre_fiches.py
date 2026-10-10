"""
Test — l'ordre des lignes sur une feuille de présence.

Le jour est porté par le regroupement ; à l'intérieur d'un jour, les lignes
étaient triées par CRÉNEAU seulement. Dans un même créneau, elles arrivaient
donc dans l'ordre que la base rendait : « MP B » pouvait précéder « MP A », et
la classe qu'on cherchait se trouvait au jugé — sur une feuille qu'on parcourt
debout, en salle, avant de la faire signer.

L'ordre attendu, à 08h00-09h30 :

    MP A  —  MP B  —  MP C

Une ligne sans département passe en dernier : c'est l'exception, elle n'a pas
à ouvrir le groupe.

Le même tri sert aux deux usages — la liste de l'écran et les lignes du PDF —
parce qu'une feuille imprimée qui ne se lirait pas comme l'écran obligerait à
chercher deux fois.

Base SQLite en mémoire (siga.settings.test).
"""
import inspect

from django.test import SimpleTestCase

from apps.suivi import views as vues_suivi
from apps.suivi.views import _ordre_fiche


def ligne(creneau, departement, prof=''):
    return {'creneau': creneau, 'departement': departement, 'prof_nom': prof}


def ordre(lignes):
    return [(f['creneau'], f['departement']) for f in sorted(lignes, key=_ordre_fiche)]


class OrdreDesFichesTest(SimpleTestCase):
    def test_a_creneau_egal_les_classes_se_suivent(self):
        melange = [ligne('08h00-09h30', 'MP C'),
                   ligne('08h00-09h30', 'MP A'),
                   ligne('08h00-09h30', 'MP B')]
        self.assertEqual([d for _, d in ordre(melange)], ['MP A', 'MP B', 'MP C'])

    def test_le_creneau_prime_sur_la_classe(self):
        """Une feuille se lit d'abord dans l'ordre des heures."""
        melange = [ligne('09h40-11h10', 'MP A'),
                   ligne('08h00-09h30', 'MP C')]
        self.assertEqual(ordre(melange),
                         [('08h00-09h30', 'MP C'), ('09h40-11h10', 'MP A')])

    def test_le_cas_de_l_enonce(self):
        """L'exemple donné : même heure, même matière, trois classes."""
        melange = [ligne('08h00-09h30', 'MP C', 'Cheikh KHAIRY'),
                   ligne('08h00-09h30', 'MP A', 'Ahmed SEJAD'),
                   ligne('08h00-09h30', 'MP B', 'El Veth SIDI')]
        self.assertEqual([f['prof_nom'] for f in sorted(melange, key=_ordre_fiche)],
                         ['Ahmed SEJAD', 'El Veth SIDI', 'Cheikh KHAIRY'])

    def test_une_ligne_sans_departement_passe_en_dernier(self):
        melange = [ligne('08h00-09h30', ''),
                   ligne('08h00-09h30', 'MP A')]
        self.assertEqual([d for _, d in ordre(melange)], ['MP A', ''])

    def test_un_departement_absent_ne_fait_pas_echouer_le_tri(self):
        """La clé manquante vaut chaîne vide, pas une exception."""
        melange = [{'creneau': '08h00-09h30'},
                   ligne('08h00-09h30', 'MP A')]
        self.assertEqual(sorted(melange, key=_ordre_fiche)[0]['departement'], 'MP A')

    def test_mp_passe_avant_mpsi(self):
        """L'espace précède les lettres : « MP A » avant « MPSI A »."""
        melange = [ligne('08h00-09h30', 'MPSI A'), ligne('08h00-09h30', 'MP A')]
        self.assertEqual([d for _, d in ordre(melange)], ['MP A', 'MPSI A'])


class LesDeuxUsagesPartagentLeTriTest(SimpleTestCase):
    """L'écran et le PDF doivent trier PAREIL.

    Le tri vit à deux endroits : la liste rendue à l'écran et les lignes
    construites pour le document. Les tests précédents ne regardent que la
    fonction — ils resteraient verts si l'un des deux appels revenait au tri
    par créneau seul, et la feuille imprimée se lirait alors autrement que
    l'écran. On lit donc la source pour vérifier qu'aucun des deux n'a dérivé.

    Un test de source est inhabituel ; il est ici le moyen le plus simple de
    figer « les deux partagent la même clé » sans monter tout un jeu de
    pointages en base.
    """

    def test_les_deux_tris_passent_par_la_meme_cle(self):
        source = inspect.getsource(vues_suivi)
        self.assertEqual(source.count('key=_ordre_fiche'), 2,
                         "la liste de l'écran ET les lignes du PDF")

    def test_plus_aucun_tri_par_creneau_seul(self):
        source = inspect.getsource(vues_suivi)
        self.assertNotIn("key=lambda f: f['creneau']", source,
                         'un tri par créneau seul a été réintroduit')
