import logging
"""Actions d'écriture du PV de délibération extraites des vues.

Builders purs : prennent un PVDeliberation (+ son service de délibération)
et renvoient une Response. La logique de notation reste dans les services
de délibération ; ici on orchestre + on valide (cohérence maquette, etc.).
"""
from rest_framework import status
from rest_framework.response import Response

from apps.evaluations.services.calcul_notes import NoteCalculService
from apps.evaluations.models import MembreJury
from apps.evaluations.serializers import MembreJurySerializer, PVDeliberationSerializer

logger = logging.getLogger('siga')


def _recalculer_semestres_si_besoin(pv) -> int:
    """PV semestriel NORMALE : recalcule modules + semestres si les ResultatSemestre
    manquent / sont vides / invalides. Retourne le nombre de semestres recalculés.

    Rattrapage (Art. 18) : pas d'auto-recalcul (doit être fait manuellement avant).
    Retourne 0 si non applicable ou si aucun recalcul n'était nécessaire.
    """
    is_ratt = pv.session and pv.session.type_session == 'rattrapage'
    if not (pv.type_pv == 'semestriel' and pv.session_id and not is_ratt):
        return 0

    from apps.evaluations.models import ResultatSemestre, ResultatModule
    qs_sem = ResultatSemestre.objects.filter(session=pv.session)
    # Recalcul nécessaire si : pas de semestres, ou tous à 0/code vide,
    # ou des modules invalides (moyenne=0 suite à bug element__module).
    has_bad_modules = ResultatModule.objects.filter(session=pv.session, moyenne=0).exists()
    needs_recalc = (
        not qs_sem.exists()
        or not qs_sem.exclude(code_statut='').exists()
        or qs_sem.filter(moyenne=0).count() == qs_sem.count()
        or has_bad_modules
    )
    if not needs_recalc:
        return 0

    note_svc = NoteCalculService(pv.session)
    if has_bad_modules:  # purger les modules invalides avant recalcul
        ResultatModule.objects.filter(session=pv.session, moyenne=0).delete()
    note_svc.calculer_tous_modules_session()
    return len(note_svc.calculer_tous_semestres_session())


def _collecter_warnings_peuplement(pv, svc):
    """Avertissements non bloquants au peuplement du PV : sessions sources annuelles
    + cohérence maquette (coefficients, structure, minimum 2 notes par EM).

    Retourne (warnings: list[str], sessions_pretes: bool).
    """
    warnings = []
    sessions_pretes = True
    # PV annuel : vérifier que les 4 sessions sources (SN/SR × I/P) sont prêtes.
    if pv.type_pv == 'annuel' and hasattr(svc, 'verifier_sessions_pretes'):
        verif = svc.verifier_sessions_pretes()
        warnings = verif['warnings']
        sessions_pretes = verif['pretes']

    from apps.evaluations.services.coherence_maquette import (
        verifier_coherence_coefficients, warnings_coefficients,
        verifier_minimum_deux_notes, verifier_structure_maquette,
    )
    sem_code = pv.semestre_code if pv.type_pv == 'semestriel' else None

    # Cohérence des coefficients (Art. 14 / Art. 19).
    anomalies = verifier_coherence_coefficients(filiere=pv.filiere, semestre_code=sem_code)
    warnings.extend(warnings_coefficients(anomalies))

    # Structure de la maquette (Art. 8 / Art. 13-14) — limitée à 6 messages.
    struct_msgs = verifier_structure_maquette(filiere=pv.filiere, semestre_code=sem_code)
    warnings.extend(struct_msgs[:6])
    if len(struct_msgs) > 6:
        warnings.append(
            f'Structure — {len(struct_msgs) - 6} autre(s) anomalie(s). '
            f'Audit complet : python manage.py verifier_maquettes'
        )

    # Minimum deux notes par EM (Art. 12 / Art. 17) — PV semestriel uniquement.
    if pv.type_pv == 'semestriel' and pv.session_id:
        warnings.extend(verifier_minimum_deux_notes(
            session=pv.session, filiere=pv.filiere, semestre_code=pv.semestre_code,
        ))
    return warnings, sessions_pretes


def do_peupler(pv, svc):
    """Peuple le PV + recalcule les décisions.

    Orchestration : garde clôture → recalcul des semestres si besoin → collecte des
    avertissements (maquette) → peuplement des lignes + décisions + obligations
    (Art. 17, PV semestriel normale uniquement).
    """
    if pv.est_clos:
        return Response({'detail': 'PV déjà clos.'}, status=status.HTTP_400_BAD_REQUEST)

    recalc_sem = _recalculer_semestres_si_besoin(pv)
    warnings, sessions_pretes = _collecter_warnings_peuplement(pv, svc)

    lignes = svc.peupler_lignes()
    decisions = svc.calculer_decisions()

    nb_obligations = 0
    if (pv.type_pv == 'semestriel' and pv.session
            and pv.session.type_session == 'normale'):
        nb_obligations = svc.generer_obligations()

    return Response({
        'lignes_creees_ou_maj': lignes,
        'decisions_calculees':  decisions,
        'semestres_recalcules': recalc_sem,
        'obligations_generees': nb_obligations,
        'warnings':             warnings,
        'sessions_pretes':      sessions_pretes,
    })


def _sessions_a_recalculer(pv):
    """Sessions à recalculer pour ce PV.

    Semestriel rattrapage (Art. 18) : inclut aussi la session normale correspondante
    (recalculée d'abord, la rattrapage applique ensuite le max). Annuel : toutes les
    sessions de l'année. Retourne une liste (éventuellement vide).
    """
    from apps.evaluations.models import SessionEvaluation
    if pv.session_id:
        s = pv.session
        if s.type_session == 'rattrapage':
            session_normale = SessionEvaluation.objects.filter(
                annee_univ=s.annee_univ,
                type_semestre=s.type_semestre,
                type_session='normale',
            ).first()
            if session_normale:
                return [session_normale, s]
        return [s]
    if pv.annee_univ_id:
        return list(SessionEvaluation.objects.filter(annee_univ_id=pv.annee_univ_id))
    return []


def _recalculer_chaine(sessions):
    """Recalcule éléments → modules → semestres pour chaque session (purge les
    ResultatModule avant). Retourne (nb_elements, nb_modules, nb_semestres)."""
    from apps.evaluations.models import ResultatModule
    nb_elems = nb_mods = nb_sems = 0
    for session in sessions:
        svc = NoteCalculService(session)
        nb_elems += len(svc.calculer_tous_elements_session())
        ResultatModule.objects.filter(session=session).delete()
        nb_mods += len(svc.calculer_tous_modules_session())
        nb_sems += len(svc.calculer_tous_semestres_session())
    return nb_elems, nb_mods, nb_sems


def do_recalculer_tout(pv, svc_pv):
    """Recalcule toute la chaîne (éléments → modules → semestres → lignes + décisions).

    Orchestration sur les sessions concernées (PV semestriel ET annuel).
    """
    if pv.est_clos:
        return Response({'detail': 'PV déjà clos.'}, status=status.HTTP_400_BAD_REQUEST)

    nb_elems, nb_mods, nb_sems = _recalculer_chaine(_sessions_a_recalculer(pv))

    lignes    = svc_pv.peupler_lignes()
    decisions = svc_pv.calculer_decisions()

    # Generation auto des ObligationRattrapage (Art. 17) pour PV semestriel
    # de session normale. Sans ca, "Recalculer tout" ne recalcule pas vraiment
    # tout : les obligations restent celles d'avant le recalcul (ou vides),
    # ce qui desynchronise la session de rattrapage.
    nb_obligations = 0
    if (pv.type_pv == 'semestriel' and pv.session
            and pv.session.type_session == 'normale'):
        try:
            nb_obligations = svc_pv.generer_obligations()
        except Exception as exc:
            logger.warning('recalculer_tout PV %s: generer_obligations a echoue: %s', pv.id, exc)

    return Response({
        'elements_recalcules':  nb_elems,
        'modules_recalcules':   nb_mods,
        'semestres_recalcules': nb_sems,
        'lignes_maj':           lignes,
        'decisions_calculees':  decisions,
        'obligations_generees': nb_obligations,
    })


def do_signer(pv, request):
    """
    POST /api/v1/evaluations/pvs/{id}/signer/
    Body: { role: 'president'|'membre'|'secretaire' }
    Enregistre la signature du membre jury appelant.
    """
    if pv.est_clos:
        return Response({'detail': 'PV déjà clos.'}, status=status.HTTP_400_BAD_REQUEST)
    role = request.data.get('role', 'membre')
    from django.utils import timezone
    membre, _ = MembreJury.objects.get_or_create(
        pv=pv, user=request.user,
        defaults={'role': role},
    )
    membre.signature_at = timezone.now()
    membre.role = role
    membre.save(update_fields=['signature_at', 'role'])
    return Response(MembreJurySerializer(membre).data)


def do_clore(pv):
    if pv.est_clos:
        return Response({'detail': 'PV déjà clos.'}, status=status.HTTP_400_BAD_REQUEST)

    # Generation auto des ObligationRattrapage avant cloture (Art. 17).
    # Sans ca, un user qui cloture sans avoir au prealable appele peupler-lignes
    # se retrouve avec un PV clos mais 0 obligations -> session de rattrapage vide.
    # Le service est idempotent (delete + recreate par ligne), safe a appeler ici.
    nb_obligations = 0
    if (pv.type_pv == 'semestriel' and pv.session
            and pv.session.type_session == 'normale'):
        from apps.evaluations.services.deliberation_semestre import DeliberationSemestreService
        try:
            nb_obligations = DeliberationSemestreService(pv).generer_obligations()
        except Exception as exc:
            logger.warning('clore PV %s: generer_obligations a echoue: %s', pv.id, exc)

    pv.est_clos = True
    pv.save(update_fields=['est_clos'])
    data = PVDeliberationSerializer(pv).data
    if isinstance(data, dict):
        data['obligations_generees'] = nb_obligations
    return Response(data)


def do_rouvrir(pv):
    if not pv.est_clos:
        return Response({'detail': 'PV déjà ouvert.'}, status=status.HTTP_400_BAD_REQUEST)
    pv.est_clos = False
    pv.save(update_fields=['est_clos'])
    return Response(PVDeliberationSerializer(pv).data)
