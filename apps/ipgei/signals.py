"""
Maintien automatique du miroir IPGEI → socle SIGA.

Sans ces signaux, le miroir se désynchroniserait dès la première matière créée
depuis l'interface, et les écrans Suivi / Vacations se remettraient à afficher
des données incomplètes — le genre de dérive qu'on ne remarque que trop tard.

Les classes et les sous-groupes n'ont PAS de signal : ils héritent de
`departement.Departement`, ils n'ont donc pas de contrepartie à maintenir.

Le rattrapage global reste disponible via `manage.py sync_miroir_siga`.
"""
import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Matiere, SeanceType

logger = logging.getLogger('siga')


@receiver(post_save,   sender=SeanceType, dispatch_uid='ipgei_miroir_seance_type')
@receiver(post_delete, sender=SeanceType, dispatch_uid='ipgei_miroir_seance_type_suppr')
def _miroir_grille(sender, instance, **kwargs):
    """
    Reporte la grille type dans `emplois.Emplois` a chaque modification.

    C'est ce report qui alimente le Suivi : sans lui, « Generer la semaine »
    ne produirait aucune ligne et la chaine pointage -> charge -> vacations
    resterait vide, sans qu'aucun message ne le signale.
    """
    from .miroir import synchroniser_grille
    try:
        synchroniser_grille(instance.grille)
    except Exception:
        logger.exception('Miroir Emplois impossible pour la grille #%s', instance.grille_id)


# Pas de signal sur SemaineIPGEI : le calendrier du socle
# (« Parametres -> Semaines ») est desormais la SOURCE, et les semaines IPGEI
# en sont le reflet. Repousser dans l'autre sens recreerait au socle des
# semaines qu'on venait d'en tirer.


@receiver(post_save, sender=Matiere, dispatch_uid='ipgei_miroir_matiere')
def _miroir_matiere(sender, instance, **kwargs):
    from .miroir import synchroniser_matiere
    try:
        synchroniser_matiere(instance)
    except Exception:
        logger.exception('Miroir EM impossible pour la matiere #%s', instance.pk)
