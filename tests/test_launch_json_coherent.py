"""
Le lancement des deux serveurs ne doit pas diverger en silence.

`.claude/launch.json` dit sur quels ports tournent le back (8001) et le front
(3010). Il existe à DEUX endroits, et c'est voulu faute de mieux :

  * `backend_ipgei/.claude/launch.json` — la référence, versionnée ici ;
  * `C:\\SIR\\IPGEI\\.claude\\launch.json` — la copie de travail, la SEULE que
    l'outil de prévisualisation lise.

Pourquoi deux. `C:\\SIR\\IPGEI` n'est pas un dépôt, donc la copie de travail
n'est sauvegardée nulle part ; et en faire un dépôt mettrait sous Git les 17 Mo
de listes nominatives, de PV et d'annuaires du personnel qui vivent à côté du
code — à une commande de distance d'un `push` vers GitHub. Le remède serait pire
que le mal.

Reste le défaut du montage : modifier une copie sans l'autre, et ne jamais le
savoir. C'est ce silence que ces tests suppriment. Ils ne suppriment pas la
duplication — ils la rendent bruyante.

Aucune base de données : de la lecture de fichiers, rien d'autre.
"""
import json
from pathlib import Path

import pytest

#: `tests/` → `backend_ipgei/` → `C:\SIR\IPGEI`
DEPOT   = Path(__file__).resolve().parents[1]
RACINE  = DEPOT.parent

REFERENCE = DEPOT / '.claude' / 'launch.json'
TRAVAIL   = RACINE / '.claude' / 'launch.json'

#: Les ports RÉELS de l'installation, ceux que portent les deux CLAUDE.md.
#: Les y figer ici fait qu'une faute de frappe dans la configuration se voit au
#: test plutôt qu'au premier écran blanc.
PORTS_ATTENDUS = {'ipgei-backend': 8001, 'ipgei-frontend': 3010}

RAPPEL_COPIE = (
    'Reportez la correction dans la référence, puis recopiez-la :\n'
    '    cp backend_ipgei/.claude/launch.json .claude/launch.json'
)


def _charger(chemin: Path) -> dict:
    return json.loads(chemin.read_text(encoding='utf-8'))


def _ports(config: dict) -> dict:
    return {c['name']: c['port'] for c in config['configurations']}


def test_la_reference_est_un_json_valide():
    """
    Elle est écrite à la main, et une virgule de trop la rend illisible SANS
    message : l'outil répond « aucun launch.json trouvé », ce qui envoie
    chercher le défaut à l'endroit exact où il n'est pas. C'est arrivé.
    """
    assert REFERENCE.exists(), f'Référence absente : {REFERENCE}'
    config = _charger(REFERENCE)
    assert config['configurations'], 'aucun serveur déclaré'


def test_la_reference_declare_les_ports_reels():
    assert _ports(_charger(REFERENCE)) == PORTS_ATTENDUS, (
        'Le back tourne sur 8001 (un autre projet occupe le 8000) et le front '
        'sur 3010. Si ces ports ont VRAIMENT changé, corrigez PORTS_ATTENDUS '
        'ici et les deux CLAUDE.md dans le même commit.'
    )


def test_les_deux_copies_disent_la_meme_chose():
    """
    Le test qui justifie ce fichier.

    On compare le JSON ANALYSÉ et non les octets : une indentation ou une fin de
    ligne qui diffère n'est pas une divergence, et faire échouer là-dessus
    apprendrait vite à ignorer ce test.

    Absent de la racine, on passe : ce dépôt se clone ailleurs — une CI, un
    autre poste — où la copie de travail n'a aucune raison d'exister.
    """
    if not TRAVAIL.exists():
        pytest.skip(f'Pas de copie de travail à la racine ({TRAVAIL}) — '
                    'rien à comparer.')

    reference, travail = _charger(REFERENCE), _charger(TRAVAIL)
    if reference == travail:
        return

    ecarts = []
    ports_ref, ports_trav = _ports(reference), _ports(travail)
    for nom in sorted(set(ports_ref) | set(ports_trav)):
        if ports_ref.get(nom) != ports_trav.get(nom):
            ecarts.append(f'  {nom} : référence {ports_ref.get(nom)} '
                          f'≠ racine {ports_trav.get(nom)}')

    detail = '\n'.join(ecarts) or '  (les ports concordent, autre chose diffère)'
    pytest.fail(
        'Les deux copies de launch.json ont divergé.\n'
        f'  référence : {REFERENCE}\n'
        f'  racine    : {TRAVAIL}\n'
        f'{detail}\n{RAPPEL_COPIE}'
    )
