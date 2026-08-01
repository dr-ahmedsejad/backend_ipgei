"""
Normalisation de l'arabe pour le rendu PDF.

Problème : des champs arabes sont parfois saisis/collés depuis un clavier ou une
source **Farsi/Ourdou**, introduisant des lettres qui RESSEMBLENT à de l'arabe
mais ont un codepoint Unicode différent (ex. ھ HEH DOACHASHMEE U+06BE au lieu de
ه HEH U+0647, ou ی FARSI YEH U+06CC au lieu de ي YEH U+064A). Les polices Arabic
« standard » du serveur (Arial de msttcorefonts sous Docker) n'ont PAS le glyphe
de ces variantes → elles s'affichent en « tofu » (□) dans le PDF, alors qu'elles
passent sur un poste Windows (police système plus complète).

Solution : `normaliser_arabe(html)` mappe ces variantes vers l'arabe STANDARD
JUSTE AVANT le rendu wkhtmltopdf. NON DESTRUCTIF (les données en base ne changent
pas), corrige tous les documents d'un coup et immunise contre les futurs
copier-coller. Le mapping ne concerne que des codepoints U+06xx : le HTML, le CSS
et les images base64 (ASCII) ne sont jamais affectés.
"""

# Variantes Farsi / Ourdou -> arabe standard (celles qui ont un equivalent sur).
_TABLE = {
    0x06CC: 0x064A,  # FARSI YEH        ی -> YEH  ي
    0x06D2: 0x064A,  # YEH BARREE       ے -> YEH  ي
    0x0649: 0x0649,  # ALEF MAKSURA     ى -> (deja standard, conserve)
    0x06BE: 0x0647,  # HEH DOACHASHMEE  ھ -> HEH  ه   (le cas signale)
    0x06C1: 0x0647,  # HEH GOAL         ہ -> HEH  ه
    0x06A9: 0x0643,  # KEHEH            ک -> KAF  ك
    0x06AA: 0x0643,  # SWASH KAF        ڪ -> KAF  ك
    0x06AB: 0x0643,  # KAF WITH RING    ګ -> KAF  ك
    0x06CB: 0x0648,  # VE               ۋ -> WAW  و
}


def normaliser_arabe(text):
    """Remplace les lettres Farsi/Ourdou par leur equivalent arabe standard.
    Sûr sur du HTML complet (n'affecte que des codepoints U+06xx)."""
    if not text:
        return text
    return text.translate(_TABLE)
