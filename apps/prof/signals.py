"""Signaux Django sur le modèle Prof.

1. Maintient prof_type_history en sync avec Prof.type :
   - pre_save : capture l'ancien type (snapshot avant modification)
   - post_save :
       * Si nouveau prof (created=True) : créer entrée initiale
       * Si type change (UPDATE) : fermer l'ancienne période et ouvrir la nouvelle

2. Crée automatiquement un CustomUser (rôle 'enseignant') quand un Prof est créé
   avec téléphone + NNI. Couvre :
     - Création unitaire via API REST (POST /profs/)
     - Création via shell Django, scripts custom, fixtures
     - PAS bulk_create (Django ne déclenche pas les signaux dessus) → utiliser
       l'endpoint /profs/generer-comptes/ pour rattraper.

Aucun ecrasement : on append uniquement, on ferme la periode precedente avec date_fin.
Sentinel attribute pour eviter les boucles infinies si signals re-modifient prof.
"""
import logging
import datetime as _dt

from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.utils import timezone

from .models import Prof, ProfTypeHistory

logger = logging.getLogger('siga')


def _annee_universitaire_start():
    """Date de debut de l'annee universitaire active.

    Sert a dater le statut INITIAL d'un nouveau prof a la rentree (et non a sa
    date de creation) : si on cree le prof / marque ses seances un mois apres son
    travail reel, il doit quand meme apparaitre dans les etats des mois precedents.
    Le filtre paie ne paie que les mois ou il a une activite reelle, donc rattacher
    le statut a la rentree est sans risque.

    Priorite : Year.date_debut -> parse 'YYYY-YYYY' (1er septembre) -> aujourd'hui.
    """
    try:
        from apps.parametres.models import Year
        y = (Year.objects.filter(est_active=True).first()
             or Year.objects.order_by('-annee').first())
        if y:
            if getattr(y, 'date_debut', None):
                return y.date_debut
            try:
                start = int(str(y.annee).split('-')[0])
                return _dt.date(start, 9, 1)
            except (ValueError, IndexError, TypeError):
                pass
    except Exception:
        pass
    return timezone.now().date()


@receiver(pre_save, sender=Prof)
def _capture_old_prof_type(sender, instance, **kwargs):
    """Stocke l'ancien type sur l'instance pour comparaison post_save."""
    if instance.pk:
        try:
            old = Prof.objects.only('type').get(pk=instance.pk)
            instance._old_type = old.type
        except Prof.DoesNotExist:
            instance._old_type = None
    else:
        instance._old_type = None


@receiver(post_save, sender=Prof)
def _sync_prof_type_history(sender, instance, created, **kwargs):
    """Cree ou met a jour prof_type_history selon la modification de prof.type.

    - Nouveau prof : insert 'Statut initial'
    - Type modifie : ferme l'ancienne periode + ouvre la nouvelle
    - Type identique : no-op
    """
    today = timezone.now().date()

    if created:
        # Nouveau prof : creer l'entree initiale, sauf si elle existe deja
        # (cas du backfill ou double signal)
        if not ProfTypeHistory.objects.filter(prof_id=instance.pk).exists():
            # Date de debut = rentree de l'annee active (pas la date de creation),
            # pour couvrir les seances saisies/marquees apres coup. Jamais > aujourd'hui.
            debut = min(_annee_universitaire_start(), today)
            ProfTypeHistory.objects.create(
                prof_id=instance.pk,
                type=instance.type,
                date_debut=debut,
                date_fin=None,
                motif='Statut initial (creation auto via signal)',
                cree_par='auto-signal',
            )
        return

    old_type = getattr(instance, '_old_type', None)
    if old_type is None or old_type == instance.type:
        # Pas de modification de type → rien a faire
        return

    # Type change : fermer l'ancienne periode ouverte (date_fin IS NULL)
    # et ouvrir la nouvelle.
    yesterday = today - timezone.timedelta(days=1)
    ProfTypeHistory.objects.filter(
        prof_id=instance.pk, date_fin__isnull=True
    ).update(date_fin=yesterday)

    ProfTypeHistory.objects.create(
        prof_id=instance.pk,
        type=instance.type,
        date_debut=today,
        date_fin=None,
        motif=f'Changement auto via signal : {old_type} -> {instance.type}',
        cree_par='auto-signal',
    )


@receiver(post_save, sender=Prof)
def _auto_create_account_for_prof(sender, instance, created, **kwargs):
    """Crée automatiquement un CustomUser pour un nouveau Prof.

    Délégué à apps.prof.services.creer_compte_pour_prof qui gère les conditions
    (téléphone + NNI obligatoires, username unique). Silent fail si conditions
    non remplies — le rattrapage se fait via l'endpoint /profs/generer-comptes/.

    Évite les boucles infinies via getattr(instance, '_skip_account_signal', False)
    : la fonction creer_compte_pour_prof fait elle-même prof.save(update_fields=['user'])
    qui re-déclenche post_save → on l'ignore quand le sentinel est posé.
    """
    if not created:
        return
    if getattr(instance, '_skip_account_signal', False):
        return
    if instance.user_id:
        return  # Déjà lié — rien à faire

    try:
        from .services import creer_compte_pour_prof
        # Sentinel pour éviter le re-déclenchement quand creer_compte_pour_prof
        # appelle prof.save(update_fields=['user']) en interne
        instance._skip_account_signal = True
        creer_compte_pour_prof(instance)
    except Exception as exc:
        # On ne casse jamais la création d'un Prof si le compte échoue
        # (ex: téléphone déjà pris) — l'admin pourra rattraper via l'endpoint.
        logger.warning(
            "Échec création auto compte pour prof #%s (%s) : %s",
            instance.pk, instance.nom, exc,
        )
    finally:
        instance._skip_account_signal = False
