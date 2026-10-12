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

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.parametres.models import Semaine as SemaineSocle

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

# ═════════════════════════════════════════════════════════════════════════════
# Calendrier : le socle pose les semaines, IPGEI les reflète
# ═════════════════════════════════════════════════════════════════════════════
#: Couples (année, type de semestre) touchés dans la transaction en cours.
#: Poser un calendrier crée UNE LIGNE PAR JOUR — 96 pour seize semaines de six
#: jours. Refléter à chaque ligne referait quatre-vingt-seize fois le même
#: travail ; on retient le couple et l'on reflète UNE fois, après le commit,
#: quand les semaines sont complètes et leurs bornes connues.
_A_REFLETER: set = set()


def _refleter_calendrier():
    """Reflète dans les semestres IPGEI les calendriers accumulés."""
    from . import services
    from .models import SemestreIPGEI

    couples = list(_A_REFLETER)
    _A_REFLETER.clear()
    # `type_semestre` est une PROPRIÉTÉ dérivée du code, pas une colonne : on
    # filtre donc sur les codes. S1 et S3 sont impairs, S2 et S4 pairs.
    codes = {'I': ('S1', 'S3'), 'P': ('S2', 'S4')}
    for annee, type_semestre in couples:
        for semestre in SemestreIPGEI.objects.filter(
            annee_universitaire=annee,
            code__in=codes.get(type_semestre, ()),
        ):
            try:
                bilan = services.synchroniser_calendrier(semestre)
                if bilan['creees'] or bilan['modifiees'] or bilan['supprimees']:
                    logger.info(
                        'Calendrier %s %s → %s : %s créée(s), %s corrigée(s), '
                        '%s retirée(s)', annee, type_semestre, semestre.code,
                        bilan['creees'], bilan['modifiees'], bilan['supprimees'])
                if bilan['retenues']:
                    logger.warning(
                        'Calendrier %s → %s : semaine(s) %s retirée(s) du socle '
                        'mais CONSERVÉE(s) — des séances y sont rattachées.',
                        annee, semestre.code,
                        ', '.join(str(n) for n in bilan['retenues']))
            except Exception:
                logger.exception('Reflet du calendrier impossible pour %s', semestre)


@receiver(post_save,   sender=SemaineSocle, dispatch_uid='ipgei_refleter_semaine')
@receiver(post_delete, sender=SemaineSocle, dispatch_uid='ipgei_refleter_semaine_suppr')
def _semaine_socle_touchee(sender, instance, created=None, **kwargs):
    """
    Une semaine posée au calendrier du socle apparaît d'office côté IPGEI.

    Il fallait jusqu'ici cliquer « Reprendre » sur chaque semestre, alors que
    le module fait déjà ce rattrapage ailleurs — ouvrir un semestre inscrit ses
    étudiants aux matières, créer une matière l'ajoute aux inscrits. Le
    calendrier avait été oublié, et une semaine ajoutée restait invisible aux
    séances tant qu'on n'y pensait pas.

    Le reflet est différé au commit : la table du socle porte une ligne par
    JOUR, et refléter dès la première laisserait une semaine bornée au lundi.
    Après le commit, les six jours sont là et les bornes sont justes.

    La SUPPRESSION est suivie elle aussi : une semaine ajoutée par erreur puis
    retirée du calendrier disparaît d'ici. Avec une réserve — si des séances
    s'y sont accrochées, elle est CONSERVÉE : on ne fait pas disparaître un
    emploi du temps par ricochet. Le journal le signale, et il revient à
    l'agent de vider la semaine avant de la retirer.
    """
    # `created` vaut None sur une suppression : on reflète alors aussi, c'est
    # le troisième mouvement du calendrier — retirer une semaine posée par
    # erreur. Une simple modification (created=False) ne change rien aux
    # bornes tant que la date ne bouge pas ; on reflète quand même, la
    # synchronisation étant sans effet s'il n'y a rien à corriger.
    if not (instance.numero_semaine and instance.annee_universitaire):
        return
    cle = (instance.annee_universitaire, instance.type_semestre)
    if cle in _A_REFLETER:
        return
    premier = not _A_REFLETER
    _A_REFLETER.add(cle)
    if premier:
        transaction.on_commit(_refleter_calendrier)


@receiver(post_save, sender=SemestreIPGEI, dispatch_uid='ipgei_decalage_semestre')
def _decalage_du_semestre(sender, instance, **kwargs):
    """Descend le décalage de rentrée sur les départements du niveau.

    Le socle convertit le numéro de semaine à partir de
    `Departement.decalage_impair` / `decalage_pair`, un champ par groupe. Le
    régler une fois sur le semestre et le laisser descendre évite la quinzaine
    de saisies identiques — et l'oubli silencieux qui irait avec.

    Après le commit : une répétition en transaction annulée ne doit pas laisser
    de trace sur les départements, et la propagation n'a de sens que si le
    semestre est bien enregistré.
    """
    from . import services

    def propager():
        try:
            corriges = services.appliquer_decalage(instance)
            if corriges:
                logger.info('Décalage %s %s = %s semaine(s) → %s département(s)',
                            instance.annee_universitaire, instance.code,
                            instance.decalage_semaines, corriges)
        except Exception:
            logger.exception('Propagation du décalage impossible pour %s', instance)

    transaction.on_commit(propager)
