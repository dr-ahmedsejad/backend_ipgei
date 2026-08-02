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

from .models import Matiere, SeanceType, SemestreIPGEI

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


@receiver(post_save, sender=SemestreIPGEI, dispatch_uid='ipgei_inscrire_semestre')
def _inscrire_au_nouveau_semestre(sender, instance, created, **kwargs):
    """
    Un semestre qui s'ouvre inscrit d'office les étudiants de son niveau.

    À l'IPGEI on s'inscrit à une année, pas à un semestre : le semestre pair,
    créé plus tard, ne doit pas obliger à repasser sur chaque dossier.
    """
    if not created:
        return
    from . import services
    try:
        n = services.rattacher_inscrits(instance)
        if n:
            logger.info('Semestre %s : %s ligne(s) de note créée(s).', instance.code, n)
    except Exception:
        logger.exception('Rattachement des inscrits au semestre %s impossible', instance.pk)


@receiver(post_save, sender=Matiere, dispatch_uid='ipgei_inscrire_matiere')
def _inscrire_a_la_nouvelle_matiere(sender, instance, created, **kwargs):
    """
    Une matière ajoutée après la rentrée vaut pour les inscrits du semestre.

    Sans cela elle n'existerait que pour les inscriptions postérieures, et deux
    étudiants de la même classe n'auraient pas la même maquette.
    """
    if not (created and instance.actif):
        return
    from . import services
    from .models import SemestreIPGEI as _Semestre
    try:
        for semestre in _Semestre.objects.filter(
            code=instance.code_semestre, institution=instance.institution,
        ):
            services.rattacher_inscrits(semestre)
    except Exception:
        logger.exception('Rattachement des inscrits à la matière %s impossible', instance.pk)
