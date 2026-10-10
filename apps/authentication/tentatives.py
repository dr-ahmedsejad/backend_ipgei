"""
Tentatives de connexion : ce qu'axes ne fait pas seul derrière une connexion JWT.

Deux verrous, tous deux calculés sur la table d'axes (`axes_accessattempt`),
donc partagés par tous les processus du serveur :

- le COMPTE depuis une adresse : 5 échecs → ce compte est refusé depuis cette
  adresse pendant 15 minutes. C'est axes qui le pose (AXES_LOCKOUT_PARAMETERS
  = couple compte + IP) ; on ne fait ici que le constater pour le DIRE ;
- l'ADRESSE, tous comptes confondus : LOGIN_ECHECS_PAR_IP échecs → l'adresse
  est refusée. C'est ce qui arrête celui qui essaie un mot de passe courant sur
  des centaines de comptes, un essai chacun.

Seuls les ÉCHECS comptent : une salle entière peut se connecter en même temps
derrière une seule adresse.

Ce que l'on corrige (mesuré le 04/10/2026) :
- le blocage répondait « Aucun compte actif n'a été trouvé… », comme un simple
  mot de passe faux — l'utilisateur réessayait et prolongeait son blocage ;
- une connexion réussie ne remettait pas le compteur à zéro : axes attend le
  signal `user_logged_in`, qu'une connexion par jeton n'envoie jamais.
"""
import math

from django.conf import settings
from django.db.models import Max, Sum
from django.utils import timezone


def actif():
    return getattr(settings, 'AXES_ENABLED', True)


def _fenetre():
    return settings.AXES_COOLOFF_TIME


def _minutes_restantes(tentatives, seuil):
    """Minutes de blocage restantes, ou None si `tentatives` reste sous `seuil`."""
    debut = timezone.now() - _fenetre()
    agg = tentatives.filter(attempt_time__gte=debut).aggregate(
        echecs=Sum('failures_since_start'), derniere=Max('attempt_time'))
    if (agg['echecs'] or 0) < seuil:
        return None
    reste = agg['derniere'] + _fenetre() - timezone.now()
    return max(1, math.ceil(reste.total_seconds() / 60))


def adresse_bloquee(ip):
    if not actif() or not ip:
        return None
    from axes.models import AccessAttempt
    return _minutes_restantes(AccessAttempt.objects.filter(ip_address=ip),
                              settings.LOGIN_ECHECS_PAR_IP)


def compte_bloque(username, ip):
    if not actif() or not username or not ip:
        return None
    from axes.models import AccessAttempt
    return _minutes_restantes(
        AccessAttempt.objects.filter(username=username, ip_address=ip),
        settings.AXES_FAILURE_LIMIT)


def remettre_a_zero(username, ip):
    """Après une connexion réussie : les fautes de frappe de ce compte, depuis
    cette adresse, sont oubliées — et ne pèsent plus sur l'adresse."""
    if not actif() or not username or not ip:
        return
    from axes.models import AccessAttempt
    AccessAttempt.objects.filter(username=username, ip_address=ip).delete()


def message(minutes, pour_le_compte):
    duree = '%d minute%s' % (minutes, 's' if minutes > 1 else '')
    if pour_le_compte:
        return ('Trop de tentatives échouées pour ce compte. '
                'Accès bloqué pendant %s.' % duree)
    return ('Trop de tentatives échouées depuis cette connexion. '
            'Accès bloqué pendant %s.' % duree)
