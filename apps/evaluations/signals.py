"""
Signaux Django — recalcul automatique des Résultats après modification d'une Note.

Quand une Note est créée/modifiée/supprimée, on recalcule en cascade :
  ResultatElement (de cet inscription_element + session)
  ResultatModule  (du module de l'EM correspondant + session)
  ResultatSemestre (de l'inscription_ped + session)

Pour les imports bulk, désactiver via le contexte threading :
    from apps.evaluations.signals import disable_recalcul_signal
    with disable_recalcul_signal():
        # ... saves bulk ...
    # Puis recalcul global manuel
"""
import logging
import threading
from contextlib import contextmanager

from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

# IMPORTANT : ne PAS importer Note au top — provoque un import circulaire pendant
# AppConfig.ready(). On utilise sender='evaluations.Note' (string) qui est resolu
# par Django via apps.get_model.

logger = logging.getLogger('siga')

# Contexte threading pour desactiver les signals lors d'imports bulk
_local = threading.local()


def _signal_disabled() -> bool:
    return getattr(_local, 'disabled', False)


@contextmanager
def disable_recalcul_signal():
    """Context manager pour desactiver le recalcul auto pendant un import bulk."""
    previous = getattr(_local, 'disabled', False)
    _local.disabled = True
    try:
        yield
    finally:
        _local.disabled = previous


def _recalcul_scope_note(note) -> None:
    """Recalcule ResultatElement + ResultatModule + ResultatSemestre pour le scope de cette note."""
    if _signal_disabled():
        return

    try:
        from .services.calcul_notes import NoteCalculService
        from .services.calcul_module import ResultatModuleService

        ie = note.inscription_element
        session = note.session
        if ie is None or session is None:
            return

        svc_notes = NoteCalculService(session)
        # 1. Recalcul element
        try:
            svc_notes.calculer_element(ie)
        except Exception as exc:
            logger.warning('Signal recalcul element echec : %s', exc)

        # 2. Recalcul module si EM rattache
        em = ie.em
        if em and em.module_lmd_id:
            try:
                svc_mod = ResultatModuleService(session)
                svc_mod.calculer(ie.inscription_ped, em.module_lmd)
            except Exception as exc:
                logger.warning('Signal recalcul module echec : %s', exc)

        # 3. Recalcul semestre
        try:
            svc_notes.calculer_semestre(ie.inscription_ped)
        except Exception as exc:
            logger.warning('Signal recalcul semestre echec : %s', exc)
    except Exception as exc:
        logger.error('Signal recalcul Note echec global : %s', exc)


@receiver(post_save, sender='evaluations.Note')
def note_post_save(sender, instance, created, **kwargs):
    _recalcul_scope_note(instance)


@receiver(post_delete, sender='evaluations.Note')
def note_post_delete(sender, instance, **kwargs):
    _recalcul_scope_note(instance)
