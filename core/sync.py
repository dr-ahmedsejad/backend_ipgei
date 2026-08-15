"""
Synchronisation LOCAL → VPS : publier l'état de l'établissement vers le miroir.

On construit un dump de la ZONE MIROIR — tout sauf les journaux et la boîte de
réception — puis, si une cible SSH est configurée, on le transfère au VPS où un
script receveur l'importe de façon atomique. Sans cible : le dump est construit
et décrit, rien n'est transféré. C'est le mode qui permet d'éprouver la chaîne
avant d'avoir un serveur en face.

Le sens de circulation ne s'inverse jamais : le local écrit, le miroir reflète.
D'où le refus net de pousser depuis une instance miroir — ce serait publier le
reflet par-dessus l'original.
"""
import gzip
import hashlib
import os
import subprocess
import tempfile
import time

from django.conf import settings


class SyncError(RuntimeError):
    """Échec de la publication — porté jusqu'à l'écran, et jusqu'au journal."""


def _exclusions():
    """
    Les deux formes d'exclusion, et elles ne disent pas la même chose.

    `--exclude-table-data` garde le schéma : la table est bien recréée à la
    restauration, mais vide. C'est ce qu'on veut des journaux, propres à chaque
    instance.

    `--exclude-table` retire la table du dump ENTIÈREMENT — ni schéma, ni DROP,
    ni données. C'est ce qui sauve la boîte de réception : la restauration ne la
    touche pas, donc ce qu'un enseignant a déposé en ligne survit au push.
    """
    args = []
    for table in getattr(settings, 'SYNC_EXCLUDE_TABLE_DATA', []):
        args += ['--exclude-table-data', table]
    for table in getattr(settings, 'SYNC_EXCLUDE_TABLE', []):
        args += ['--exclude-table', table]
    return args


def build_mirror_dump(dest_dir=None):
    """
    `pg_dump` de la zone miroir, gzippé, empreinte calculée.

    `--clean --if-exists` fait précéder chaque table d'un DROP : la restauration
    remplace le miroir au lieu de s'y ajouter. Les tables de la boîte de
    réception, absentes du dump, échappent à ce DROP — c'est tout le dessin.

    Rend `{path, sha256, size, tables_exclues}`.
    """
    base = settings.DATABASES['default']
    binaire = getattr(settings, 'PG_DUMP_BIN', 'pg_dump')
    dest_dir = (dest_dir or getattr(settings, 'SYNC_WORKDIR', None)
                or tempfile.gettempdir())
    os.makedirs(dest_dir, exist_ok=True)
    sortie = os.path.join(dest_dir, 'mirror_dump.sql.gz')

    env = os.environ.copy()
    env['PGPASSWORD'] = base.get('PASSWORD') or ''
    commande = [
        binaire, '--no-owner', '--no-privileges',
        '--clean', '--if-exists',
        '-h', base.get('HOST') or 'localhost',
        '-p', str(base.get('PORT') or 5432),
        '-U', base['USER'],
        *_exclusions(),
        base['NAME'],
    ]

    # Compression au fil de l'eau : une base d'établissement pèse assez pour
    # qu'on ne la charge pas en mémoire avant de l'écrire.
    with subprocess.Popen(commande, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env) as processus:
        with gzip.open(sortie, 'wb') as archive:
            for morceau in iter(lambda: processus.stdout.read(65536), b''):
                archive.write(morceau)
        erreur = processus.stderr.read()
        code = processus.wait()
    if code != 0:
        raise SyncError(f'pg_dump a échoué (code {code}) : '
                        f'{erreur.decode(errors="replace")[:400]}')

    # L'empreinte porte sur le fichier COMPRESSÉ, celui qui voyage : c'est elle
    # que le receveur recalculera avant de toucher à quoi que ce soit.
    empreinte = hashlib.sha256()
    with open(sortie, 'rb') as fichier:
        for morceau in iter(lambda: fichier.read(65536), b''):
            empreinte.update(morceau)

    return {
        'path':           sortie,
        'sha256':         empreinte.hexdigest(),
        'size':           os.path.getsize(sortie),
        'tables_exclues': list(getattr(settings, 'SYNC_EXCLUDE_TABLE_DATA', []))
                          + list(getattr(settings, 'SYNC_EXCLUDE_TABLE', [])),
    }


def _pousser_par_ssh(dump, hote, utilisateur, cle, commande_receveur):
    """
    Transfert par SSH avec commande forcée : l'empreinte passe en argument, le
    dump arrive sur l'entrée standard.

    La clé du VPS est restreinte à ce seul programme (`command=` dans
    `authorized_keys`) : même volée, elle n'ouvre pas un shell.
    """
    ssh = [
        'ssh', '-i', cle,
        '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
        f'{utilisateur}@{hote}', f'{commande_receveur} {dump["sha256"]}',
    ]
    with open(dump['path'], 'rb') as fichier:
        processus = subprocess.run(ssh, stdin=fichier, capture_output=True,
                                   timeout=600)
    if processus.returncode != 0:
        raise SyncError(f'Transfert ou restauration refusés par le VPS '
                        f'(code {processus.returncode}) : '
                        f'{processus.stderr.decode(errors="replace")[:500]}')
    return processus.stdout.decode(errors='replace').strip()


def _pousser_les_fichiers(hote, utilisateur, cle):
    """
    Recopie `/media` vers le portail — la base seule ne suffit pas.

    Une pièce jointe, une photo, un justificatif : la base n'en porte que le
    chemin. Publier sans les fichiers donne un portail plein de liens morts, et
    le défaut ne se voit qu'au clic, longtemps après.

    `--ignore-existing` plutôt qu'un miroir strict : on AJOUTE ce qui manque, on
    n'efface jamais. Ce qui a été téléversé en ligne — les pièces jointes des
    cours, qui n'existent que là — doit survivre à la publication, exactement
    comme les tables de la boîte de réception.

    Rend le compte-rendu de rsync, ou une phrase si l'outil est absent : le
    transfert des fichiers ne doit pas faire échouer une publication de base
    réussie.
    """
    import shutil

    media = getattr(settings, 'MEDIA_ROOT', '')
    if not media or not os.path.isdir(media):
        return 'aucun dossier media à transférer'
    if shutil.which('rsync') is None:
        return ('rsync introuvable — les fichiers /media n’ont pas été '
                'transférés (base publiée malgré tout)')

    cible = getattr(settings, 'SYNC_MEDIA_DEST', '') or '/opt/ipgei/media/'
    commande = [
        'rsync', '-az', '--ignore-existing',
        '-e', f'ssh -i {cle} -o BatchMode=yes -o StrictHostKeyChecking=accept-new',
        f'{media.rstrip("/")}/', f'{utilisateur}@{hote}:{cible}',
    ]
    # Tout est rattrapé, y compris l'impossibilité de lancer le programme : la
    # base est déjà publiée à ce stade, et faire échouer l'opération entière
    # pour des pièces jointes en retard serait un mauvais échange. Le compte
    # rendu part dans le journal, où il sera lu.
    try:
        processus = subprocess.run(commande, capture_output=True, timeout=1800)
    except Exception as exc:                                   # noqa: BLE001
        return f'fichiers /media NON transférés : {type(exc).__name__} — {exc}'
    if processus.returncode != 0:
        return (f'fichiers /media NON transférés (code {processus.returncode}) : '
                f'{processus.stderr.decode(errors="replace")[:300]}')
    return 'fichiers /media synchronisés'


def push_to_mirror():
    """Construit le dump, puis le pousse si une cible est configurée."""
    if getattr(settings, 'MIRROR_MODE', False):
        raise SyncError('La publication ne part que du serveur LOCAL — cette '
                        'instance est un miroir.')

    depart = time.time()
    dump = build_mirror_dump()
    hote = getattr(settings, 'SYNC_SSH_HOST', '') or ''

    if not hote:
        return {
            'status':         'dump_ready',
            'transferred':    False,
            'size':           dump['size'],
            'sha256':         dump['sha256'],
            'tables_exclues': dump['tables_exclues'],
            'duree_s':        round(time.time() - depart, 2),
            'detail':         'Dump miroir construit. Aucune cible SSH '
                              '(SYNC_SSH_HOST) : rien n’a été transféré.',
        }

    utilisateur = getattr(settings, 'SYNC_SSH_USER', 'ipgei-deploy')
    cle         = getattr(settings, 'SYNC_SSH_KEY', '')
    reponse = _pousser_par_ssh(
        dump, hote, utilisateur, cle,
        getattr(settings, 'SYNC_RECEIVER_CMD', 'sync'),
    )
    # Les fichiers APRÈS la base, et sans pouvoir la faire échouer : une base
    # publiée avec des pièces jointes en retard vaut mieux qu'un portail resté
    # sur l'état de la semaine dernière.
    media = _pousser_les_fichiers(hote, utilisateur, cle)

    return {
        'status':      'synced',
        'transferred': True,
        'size':        dump['size'],
        'sha256':      dump['sha256'],
        'duree_s':     round(time.time() - depart, 2),
        'vps':         f'{reponse} — {media}',
    }


# ── Points d'entrée HTTP ─────────────────────────────────────────────────────
from rest_framework.response import Response       # noqa: E402
from rest_framework.views import APIView           # noqa: E402

from core.permissions import IsAdmin               # noqa: E402


class SyncPushView(APIView):
    """
    `POST /api/v1/sync/push` — publie l'état local vers le portail.

    Réservé aux administrateurs, et doublement barré sur un miroir : par le
    middleware, puis par `push_to_mirror`. Chaque tentative laisse une trace,
    succès comme échec — c'est la seule façon de savoir, trois semaines plus
    tard, ce que le portail reflète.
    """
    permission_classes = [IsAdmin]

    def post(self, request):
        from core.models import SyncLog

        par = getattr(request.user, 'name', '') or request.user.get_username()
        try:
            resultat = push_to_mirror()
            SyncLog.objects.create(
                par=par, statut=resultat.get('status', 'synced'),
                transferred=bool(resultat.get('transferred')),
                size=resultat.get('size', 0) or 0,
                sha256=resultat.get('sha256', '') or '',
                duree_s=resultat.get('duree_s', 0) or 0,
                vps=resultat.get('vps', '') or '',
            )
            return Response(resultat)
        except SyncError as exc:
            SyncLog.objects.create(par=par, statut='error', erreur=str(exc))
            return Response({'detail': str(exc), 'code': 'sync_error'}, status=400)


class SyncHistoryView(APIView):
    """`GET /api/v1/sync/history` — les trente dernières publications."""
    permission_classes = [IsAdmin]

    def get(self, request):
        from core.models import SyncLog

        return Response([{
            'id':          ligne.id,
            'cree_le':     ligne.cree_le.isoformat(),
            'par':         ligne.par,
            'statut':      ligne.statut,
            'transferred': ligne.transferred,
            'size':        ligne.size,
            'sha256':      ligne.sha256,
            'duree_s':     ligne.duree_s,
            'vps':         ligne.vps,
            'erreur':      ligne.erreur,
        } for ligne in SyncLog.objects.all()[:30]])
