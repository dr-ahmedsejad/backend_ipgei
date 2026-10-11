"""Émission des notifications — reprise de l'ESP.

Deux règles : une notification ne fait jamais échouer l'acte métier (toute
erreur est journalisée, pas propagée) ; on ne se notifie pas soi-même.
"""
import logging

logger = logging.getLogger('siga')


def notifier(destinataires, titre, message, type='info', lien='', auteur=None):
    """
    Crée une notification pour chaque destinataire. Retourne le nombre émis.

    `destinataires` accepte un utilisateur, une liste ou un queryset.
    """
    from .models import Notification

    if destinataires is None:
        return 0
    if hasattr(destinataires, 'pk'):
        destinataires = [destinataires]

    lignes, vus = [], set()
    for u in destinataires:
        if u is None or u.pk in vus:
            continue
        if auteur is not None and u.pk == auteur.pk:
            continue          # ne pas s'annoncer à soi-même
        vus.add(u.pk)
        lignes.append(Notification(
            destinataire=u, titre=titre[:200], message=message,
            type=type, lien=lien[:500],
        ))

    if not lignes:
        return 0
    try:
        Notification.objects.bulk_create(lignes)
        return len(lignes)
    except Exception:
        logger.exception('notifier: echec de creation (%s destinataire(s))', len(lignes))
        return 0
