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
from core.types_seance import TYPES_EPREUVE, TYPES_SURVEILLANCE

RACINE = Path(__file__).resolve().parent.parent


class TypesSeanceTest(SimpleTestCase):
    def test_le_modele_porte_exactement_les_memes_codes(self):
        """
        `EpreuveIPGEI.TYPE_CHOICES` garde ses libellés, mais ses codes doivent
        rester ceux de la constante. Ajouter un type d'un seul côté ferait une
        épreuve que le reste du code ne reconnaîtrait pas comme telle.
        """
        codes = tuple(code for code, _ in EpreuveIPGEI.TYPE_CHOICES)
        self.assertEqual(sorted(codes), sorted(TYPES_EPREUVE))

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
