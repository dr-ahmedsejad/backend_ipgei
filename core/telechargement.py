"""
L'en-tête qui nomme un fichier téléchargé.

`Content-Disposition: attachment; filename="…"` ne tolère QUE de l'ASCII. Dès
qu'un nom porte un accent ou un tiret long, Django encode l'en-tête ENTIER au
format des courriels (RFC 2047) :

    =?utf-8?b?YXR0YWNobWVudDsgZmlsZW5hbWU9ImVtcGxvaV9TdGF0aXN0aXF1ZXMs…?=

Aucun navigateur ne décode cela. L'en-tête devient illisible : le fichier n'est
plus reconnu comme une pièce jointe, et son nom est perdu. Relevé le 01/10/2026
sur l'emploi du temps de « Statistiques, Economie et Applications — G1 », dont
le tiret long suffisait à déclencher l'encodage.

La forme correcte est celle de la RFC 6266 : deux noms côte à côte.

  * `filename="…"`   — une translittération ASCII, pour les clients anciens ;
  * `filename*=UTF-8''…` — le nom exact, pourcent-encodé, que tous les
    navigateurs actuels préfèrent.

Les deux sont de l'ASCII pur : Django n'a donc plus rien à encoder, et l'en-tête
part tel qu'on l'écrit.
"""
import unicodedata
from urllib.parse import quote


def entete_piece_jointe(nom: str, *, inline: bool = False) -> str:
    """La valeur de `Content-Disposition` pour `nom`, accents compris.

    `inline=True` pour un document que le navigateur affiche au lieu de le
    télécharger (l'aperçu d'un PDF officiel, par exemple).
    """
    nom = (nom or '').strip() or 'document'
    # Le guillemet et le point-virgule fermeraient le paramètre en plein milieu :
    # un nom qui en contient casserait l'en-tête, voire y glisserait une
    # directive. Le saut de ligne, lui, permettrait d'injecter un en-tête entier.
    nom = nom.replace('"', '').replace(';', ',').replace('\r', '').replace('\n', '')

    # Translittération : « Économie — G1 » → « Economie - G1 ». NFKD sépare la
    # lettre de son accent, et `ignore` laisse tomber ce qui n'a pas d'équivalent.
    secours = (unicodedata.normalize('NFKD', nom)
               .encode('ascii', 'ignore').decode('ascii').strip())
    if not secours:
        secours = 'document'

    disposition = 'inline' if inline else 'attachment'
    return "%s; filename=\"%s\"; filename*=UTF-8''%s" % (
        disposition, secours, quote(nom, safe=''))
