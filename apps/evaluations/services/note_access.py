"""Contrôle d'accès par EM pour la consultation/saisie des notes.

Helper partagé extrait de NoteViewSet._peut_acceder_em : utilisé par les lectures
(feuille) et les écritures (saisir/importer/bulk) du domaine notes.
"""


def peut_acceder_em(user, em_id) -> bool:
    """Autorisation feuille/saisie pour un EM donné :
      - accès RBAC `eval_saisie` (admin / scolarite / DE...) -> tous les EMs ;
      - sinon enseignant -> uniquement SES EMs (issus de ses pointages).
    Empêche un prof de lire/saisir les notes d'un EM qu'il n'enseigne pas.
    """
    from core.permissions import _has_access
    if getattr(user, 'is_superuser', False) or getattr(user, 'role', None) == 'admin':
        return True
    if _has_access(user, 'eval_saisie', 'voir'):
        return True
    prof = getattr(user, 'prof_profile', None)
    if not prof or not em_id:
        return False
    from apps.suivi.models import SuiviePointage
    return SuiviePointage.objects.filter(prof_id=prof.pk, em_id=em_id).exists()
