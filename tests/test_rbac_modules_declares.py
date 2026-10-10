"""
Test — chaque module RBAC déclaré dans le code est bien semé par une migration.

Le mécanisme du défaut : une vue déclare `required_module = 'x'`, personne ne
sème `x` dans `authentication_module`, et `RBACPermission` — fail-closed —
répond 403 à TOUT LE MONDE, sans message. Le seul détecteur était un
utilisateur devant un écran vide. Ce test le remplace : il confronte les codes
déclarés dans `apps/` aux codes que les migrations de données créent.

Il est STATIQUE (lecture des sources), parce que la suite tourne avec
`SEEDS_DESACTIVES_POUR_TESTS = True` : la base de test n'a aucun module. Les
formes de seed reconnues sont celles du dépôt — tuple `('code', 'Nom',
'Icone', ordre)` et `Module.objects.get_or_create(code='…')`. Une nouvelle
forme ferait passer un vrai module pour un orphelin : le test échouerait dans
le bon sens (bruyamment), et il suffirait d'ajouter la forme ici.

`ORPHELINS_CONNUS` documente l'existant : des modules du socle déclarés et
jamais semés, donc en 403 pour tous depuis le fork. Les retirer de la liste le
jour où une migration les sème — ou où leurs vues disparaissent.

« banques » en est sorti le 10/10/2026 : signalé parce que sa ligne, sur
l'écran des permissions, s'affichait sans être cliquable. La migration
`authentication/0024_module_banques` le sème désormais, sans accorder aucun
droit — l'interrupteur devient utilisable, c'est à l'administrateur de le
mettre. Restent trois orphelins, dont `emplois`, qui n'est pas des moindres.
"""
import io
import os
import re
from glob import glob
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent

#: Déclarés dans le code, semés par aucune migration — état constaté, pas
#: souhaité. Chacun rend son application inaccessible à tous les rôles.
ORPHELINS_CONNUS = {'departements', 'em', 'emplois'}

# Ancre en debut de ligne (hors commentaire) : une declaration mise en
# commentaire n'ouvre aucun ecran, elle ne doit pas passer pour un orphelin.
MOTIF_DECLARATION = re.compile(r"^[ 	]*required_module\s*=\s*'([a-z_]+)'", re.M)
MOTIFS_SEED = [
    re.compile(r"\(\s*'([a-z_]+)'\s*,\s*'[^']*'\s*,\s*'[A-Za-z0-9]*'\s*,\s*\d+\s*\)"),
    re.compile(r"Module\.objects\.(?:get_or_create|create|update_or_create)\(\s*code\s*=\s*'([a-z_]+)'"),
]


def _lire(chemin):
    return io.open(chemin, encoding='utf-8').read()


def modules_declares() -> dict:
    """code → fichiers qui le déclarent (hors migrations et tests)."""
    declares = {}
    for chemin in glob(str(RACINE / 'apps' / '**' / '*.py'), recursive=True):
        relatif = os.path.relpath(chemin, RACINE).replace(os.sep, '/')
        if '/migrations/' in relatif or os.path.basename(relatif).startswith('test'):
            continue
        for m in MOTIF_DECLARATION.finditer(_lire(chemin)):
            declares.setdefault(m.group(1), set()).add(relatif)
    return declares


def modules_semes() -> set:
    semes = set()
    for chemin in glob(str(RACINE / 'apps' / '*' / 'migrations' / '*.py')):
        source = _lire(chemin)
        for motif in MOTIFS_SEED:
            semes.update(motif.findall(source))
    return semes


def test_les_formes_de_seed_sont_reconnues():
    # Garde-fou du garde-fou : si le scan ne trouve presque rien, c'est lui
    # qui est cassé, pas le dépôt.
    semes = modules_semes()
    assert len(semes) >= 40, f'scan des migrations suspect : {len(semes)} codes trouvés'
    assert {'ipgei_notes', 'ipgei_sessions', 'doc_registre', 'profs'} <= semes


def test_tout_module_declare_est_seme():
    declares = modules_declares()
    semes    = modules_semes()
    orphelins = {code: sorted(fichiers) for code, fichiers in declares.items()
                 if code not in semes and code not in ORPHELINS_CONNUS}
    assert not orphelins, (
        'Modules RBAC déclarés dans le code mais semés par aucune migration — '
        'leurs écrans répondent 403 à tous les rôles, sans message :\n'
        + '\n'.join(f'  {code} ← {", ".join(f)}' for code, f in sorted(orphelins.items()))
    )


def test_les_orphelins_connus_le_sont_encore():
    # Quand un orphelin est enfin semé, on le retire de la liste : la liste
    # documente un défaut, elle ne doit pas survivre à sa correction.
    corriges = ORPHELINS_CONNUS & modules_semes()
    assert not corriges, f'à retirer de ORPHELINS_CONNUS, désormais semés : {sorted(corriges)}'
