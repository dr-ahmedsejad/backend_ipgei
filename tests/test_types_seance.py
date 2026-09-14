"""
Test — les codes de type de séance ne se dédoublent pas.

`core.types_seance` est le seul endroit où le triplet des épreuves est écrit.
Il l'a été cinq fois : deux constantes nommées et trois listes en clair au
milieu d'une condition. Chacune était une occasion de diverger le jour où un
quatrième type apparaît — celui qui ajoute le code ne peut pas deviner qu'il
reste quatre autres endroits à corriger.

Ces tests verrouillent les deux choses qu'une fusion ne garantit pas seule :
le modèle qui porte les mêmes codes ne doit pas s'en écarter, et aucune copie
ne doit revenir dans le code.

Base SQLite en mémoire (siga.settings.test).
"""
import io
import re
from pathlib import Path

from django.test import SimpleTestCase

from apps.ipgei.models import EpreuveIPGEI
from core.types_seance import (LIBELLES_EPREUVE, TYPES_EPREUVE,
                               TYPES_SURVEILLANCE)

RACINE = Path(__file__).resolve().parent.parent


class TypesSeanceTest(SimpleTestCase):
    def test_le_modele_porte_exactement_les_memes_codes_ET_libelles(self):
        """
        Les choix du modèle sont DÉRIVÉS du dictionnaire ; ce test le constate
        plutôt que de le supposer. Quelqu'un qui les réécrirait à la main pour
        « plus de lisibilité » rouvrirait la porte par laquelle « Épreuve » et
        « Examen » de rattrapage s'étaient séparés.
        """
        self.assertEqual(EpreuveIPGEI.TYPE_CHOICES,
                         list(LIBELLES_EPREUVE.items()))
        self.assertEqual(tuple(code for code, _ in EpreuveIPGEI.TYPE_CHOICES),
                         TYPES_EPREUVE)

    def test_chaque_code_a_son_libelle(self):
        """Le triplet découle du dictionnaire : aucun code ne peut rester muet."""
        for code in TYPES_EPREUVE:
            self.assertTrue(LIBELLES_EPREUVE.get(code), code)

    def test_la_surveillance_contient_les_epreuves(self):
        """
        Dérivée, non réécrite : ajouter un type d'épreuve doit le rendre
        payable en surveillance sans qu'on ait à y penser.
        """
        for code in TYPES_EPREUVE:
            self.assertIn(code, TYPES_SURVEILLANCE)
        self.assertIn('SURVEILLANCE', TYPES_SURVEILLANCE)

    def test_aucune_copie_du_triplet_ne_traine_dans_le_code(self):
        """
        Le garde-fou qui compte. Une fusion ne vaut que tant que personne ne
        récrit la liste ailleurs — et c'est exactement ce qui était arrivé
        cinq fois.

        Les migrations sont exclues : elles figent un état passé et ne se
        modifient jamais. Les tests aussi : celui-ci cite forcément les codes.

        Seuls les CODES sont cherchés, pas les libellés : « Devoir surveillé »
        et « Examen final » nomment aussi des types de NOTE dans
        `collecte.LIBELLES_TYPE`, une table de quatre codes différents. Un
        motif textuel ne sait pas distinguer le même concept des mêmes mots —
        c'est `test_le_modele_porte_exactement_les_memes_codes_ET_libelles`
        qui garantit l'accord des libellés, et lui est exact.
        """
        # Le TRIPLET, dans n'importe quel ordre. Un sous-ensemble délibéré —
        # `EPREUVES_ANONYMES = ('EF', 'ER')`, les seules épreuves corrigées
        # sous anonymat — dit autre chose et n'est pas une copie.
        code = r"""['"](?:DS|EF|ER)['"]"""
        motif = re.compile(rf'{code}\s*,\s*{code}\s*,\s*{code}')

        fautifs = []
        for chemin in list(RACINE.glob('apps/**/*.py')) + list(RACINE.glob('core/*.py')):
            parties = chemin.parts
            if 'migrations' in parties or chemin.name.startswith('test_'):
                continue
            if chemin.name == 'types_seance.py':
                continue                       # la définition elle-même
            texte = io.open(chemin, encoding='utf-8').read()
            if motif.search(texte):
                fautifs.append(str(chemin.relative_to(RACINE)))

        self.assertEqual(fautifs, [],
                         'importer core.types_seance plutôt que réécrire la liste')
