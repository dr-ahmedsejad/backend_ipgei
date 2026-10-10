"""
Cache des consultations étudiantes (notes, emploi du temps) — des COPIES de
réponses, jamais la source : la base reste la seule vérité, vider le cache ne
perd rien, et Redis absent = calcul direct comme avant.

Une copie n'est valable que pour une « empreinte » des données :

  * la somme des compteurs d'écriture de PostgreSQL (`pg_stat_user_tables` :
    lignes insérées + modifiées + supprimées) de toutes les tables, sauf
    celles qui n'entrent dans aucune consultation (jetons, journaux,
    notifications…). Elle bouge à TOUTE écriture — y compris bulk_create,
    bulk_update, .update() et SQL direct, que les signaux Django ne voient
    pas. PostgreSQL publie ses compteurs à la fin des transactions (au plus
    quelques secondes après un enregistrement) ;
  * un compteur Redis, augmenté à CHAQUE save()/delete() fait par le code
    (signal) : un enregistrement ordinaire invalide les copies sur-le-champ.

Une note saisie, un suivi généré, une séance déplacée… changent l'empreinte :
la consultation suivante recalcule depuis la base. Pas de liste d'écritures à
tenir à jour à la main, donc pas d'oubli possible. Filet de sécurité : une
copie ne vit jamais plus de PORTAIL_CACHE_SECONDES (0 = cache coupé).

Sur un MIROIR, la base entière est remplacée à chaque publication : la date de
la dernière publication reçue entre aussi dans l'empreinte — aucune copie
d'avant la publication n'est jamais resservie.
"""
import json
import logging

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from rest_framework.response import Response
from rest_framework.utils.encoders import JSONEncoder

logger = logging.getLogger('siga')

# Tables écrites en continu (connexions, journaux, notifications, documents
# générés à la demande) et lues par AUCUNE consultation mise en cache : les
# compter invaliderait les copies à chaque connexion d'étudiant.
TABLES_EXCLUES_PREFIXES = (
    'token_blacklist_', 'axes_', 'django_session', 'django_admin_log',
    'core_audit_log', 'notifications_', 'backup_', 'documents_',
)
# last_login et le contexte d'année sont écrits à chaque connexion.
TABLES_EXCLUES = {'authentication_customuser', 'authentication_usercontexte'}

CLE_ECRITURES = 'portail:ecritures'


def table_exclue(nom):
    return nom in TABLES_EXCLUES or nom.startswith(TABLES_EXCLUES_PREFIXES)


def _compteurs_postgres():
    if connection.vendor != 'postgresql':
        return None
    with connection.cursor() as c:
        c.execute('SELECT relname, n_tup_ins + n_tup_upd + n_tup_del FROM pg_stat_user_tables')
        return sum(n or 0 for relname, n in c.fetchall() if not table_exclue(relname))


def _publication():
    """Miroir : date de la dernière publication reçue ('' ailleurs)."""
    try:
        from core.mirror import derniere_publication_recue, est_miroir
    except ImportError:
        return ''
    if not est_miroir():
        return ''
    d = derniere_publication_recue()
    return d.isoformat() if d else ''


def empreinte():
    """Empreinte des données, ou None (base non PostgreSQL) : sans empreinte
    fiable, on ne met rien en cache."""
    pg = _compteurs_postgres()
    if pg is None:
        return None
    return f'{pg}.{cache.get(CLE_ECRITURES, 0)}.{_publication()}'


def signaler_ecriture(sender, **kwargs):
    """Signal post_save / post_delete / m2m_changed : toute écriture ORM sur
    une table utile change l'empreinte immédiatement."""
    table = getattr(getattr(sender, '_meta', None), 'db_table', '') or ''
    if table_exclue(table):
        return
    try:
        cache.add(CLE_ECRITURES, 0, None)
        cache.incr(CLE_ECRITURES)
    except Exception:
        pass   # Redis indisponible : empreinte() échouera aussi → pas de cache


def reponse_en_cache(nom, parties, calcul):
    """Réponse de `calcul()` (une Response DRF), gardée pour cette empreinte.
    Seules les réponses 200 sont gardées ; toute erreur de cache → calcul."""
    duree = getattr(settings, 'PORTAIL_CACHE_SECONDES', 600)
    if duree <= 0:
        return calcul()
    try:
        emp = empreinte()
    except Exception:
        logger.warning('Cache portail : empreinte indisponible', exc_info=True)
        emp = None
    if emp is None:
        return calcul()

    cle = 'portail:%s:%s:%s' % (nom, ':'.join(str(p) for p in parties), emp)
    try:
        texte = cache.get(cle)
    except Exception:
        texte = None
    if texte is not None:
        return Response(json.loads(texte))

    reponse = calcul()
    if reponse.status_code == 200:
        try:
            # Le JSON exact que la réponse aurait envoyé (même encodeur que DRF).
            cache.set(cle, json.dumps(reponse.data, cls=JSONEncoder), duree)
        except Exception:
            logger.warning('Cache portail : copie non gardée (%s)', nom, exc_info=True)
    return reponse
