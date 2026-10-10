"""
Garde-fou — aucun commentaire Django à cheval sur plusieurs lignes.

`{# … #}` ne commente QU'UNE LIGNE. Étalé sur deux, Django referme le
commentaire à la fin de la première et **imprime le reste**. Dans un gabarit de
PDF, le texte du commentaire se retrouve au milieu de l'emploi du temps
distribué aux étudiants, et rien ne le signale : la page se génère sans erreur.

C'est arrivé le 10/10/2026 sur `ipgei/emploi_pdf.html` — un commentaire de
quatre lignes expliquant pourquoi la classe CSS garde le code `CM`. Les trois
dernières lignes se sont imprimées sur le document.

Le piège est documenté dans le CLAUDE.md du backend depuis longtemps ; il a
quand même été refait. Un avertissement qu'on relit ne vaut pas un test qui
échoue, d'où ce fichier.

La parade tient en un mot : pour plusieurs lignes, `{% comment %} …
{% endcomment %}`, que Django ferme explicitement.
"""
from pathlib import Path

import pytest

GABARITS = Path(__file__).resolve().parent.parent / 'templates'


def _tous_les_gabarits():
    return sorted(GABARITS.rglob('*.html'))


def test_il_y_a_bien_des_gabarits_a_verifier():
    """Sans cette garde, un mauvais chemin rendrait le test vert à vide."""
    assert len(_tous_les_gabarits()) > 10, GABARITS


@pytest.mark.parametrize('gabarit', _tous_les_gabarits(),
                         ids=lambda p: p.name)
def test_aucun_commentaire_a_cheval_sur_plusieurs_lignes(gabarit):
    """Chaque `{#` doit trouver son `#}` sur la MÊME ligne."""
    fautes = []
    for numero, ligne in enumerate(
            gabarit.read_text(encoding='utf-8').splitlines(), 1):
        if ligne.count('{#') != ligne.count('#}'):
            fautes.append(f'  ligne {numero} : {ligne.strip()[:80]}')

    assert not fautes, (
        f'\n{gabarit.relative_to(GABARITS.parent)} porte un commentaire '
        '`{# … #}` qui ne se ferme pas sur sa ligne.\n'
        'Django referme le commentaire en fin de ligne et IMPRIME la suite — '
        'dans un PDF, le texte atterrit sur le document distribué.\n'
        'Utiliser `{% comment %} … {% endcomment %}` pour plusieurs lignes.\n'
        + '\n'.join(fautes)
    )
