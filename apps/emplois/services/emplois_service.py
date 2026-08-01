"""
Service layer — operations atomiques sur Emplois / EmploisArchive.
Appele uniquement par le module suivi (jamais expose directement en HTTP).

Apres Phase 5 : 100% relationnel, plus aucun CharField legacy.
"""
from __future__ import annotations
import logging

from apps.emplois.models import Emplois, EmploisArchive
from core.audit_helpers import audit_aggregate_block, write_audit

logger = logging.getLogger('siga')


def archiver_emplois(annee: str, type_semestre: str, emplois_entries: list) -> int:
    """
    Copie la liste d'entrees Emplois vers EmploisArchive.
    N'ecrase l'archive existante QUE si des entrees sont fournies
    (c'est-a-dire lors de la toute premiere generation de suivi,
    quand la table Emplois contient encore des donnees).

    Le delete d'archive prealable est scope sur les `departement_id` presents
    dans `emplois_entries` : si un autre user a deja archive ses groupes,
    son archive n'est pas effacee. Permet la generation partielle multi-user.

    A appeler a l'interieur d'un transaction.atomic() existant.
    Retourne le nombre d'entrees archivees.
    """
    if not emplois_entries:
        logger.debug('archiver_emplois: emplois_entries vide pour annee=%s ts=%s', annee, type_semestre)
        return 0

    # Departements presents dans le lot a archiver -> scope du delete
    dept_ids_lot = {e.departement_id for e in emplois_entries if e.departement_id}

    # Premiere generation : remplacer l'archive existante POUR CES DEPTS uniquement
    with audit_aggregate_block():
        del_qs = EmploisArchive.objects.filter(
            annee_universitaire=annee,
            type_semestre=type_semestre,
        )
        if dept_ids_lot:
            del_qs = del_qs.filter(departement_id__in=list(dept_ids_lot))
        del_qs.delete()

        archives = [
            EmploisArchive(
                annee_universitaire=e.annee_universitaire,
                type_semestre=e.type_semestre,
                taux_paiement=e.taux_paiement,
                prof_id=e.prof_id, em_id=e.em_id, salle_id=e.salle_id,
                departement_id=e.departement_id, semestre_id=e.semestre_id,
                creneau_fk_id=e.creneau_fk_id,
                type_seance_fk_id=e.type_seance_fk_id,
                jour_fk_id=e.jour_fk_id,
                institution_id=e.institution_id,
            )
            for e in emplois_entries
        ]
        EmploisArchive.objects.bulk_create(archives)
    logger.info('archiver_emplois: %d entrees archivees pour annee=%s ts=%s',
                len(archives), annee, type_semestre)
    write_audit(
        action='ARCHIVE',
        model_name='EmploisArchive',
        object_id='0',
        changes={'annee': annee, 'type_semestre': type_semestre, 'count': len(archives)},
        label=f'Archivage EDT {annee} {type_semestre}',
    )
    return len(archives)


def restaurer_depuis_archive(annee: str, type_semestre: str,
                              dept_ids: list | None = None) -> int:
    """
    Restaure EmploisArchive -> Emplois lorsque tout le suivi
    d'une annee/type_semestre est supprime.

    Si `dept_ids` est fourni (liste d'ids), ne restaure QUE les archives
    de ces departements et ne vide QUE leur portion d'archive. Permet la
    restauration partielle multi-user (chaque responsable annule sa
    semaine sans affecter le perimetre d'un collegue).

    A appeler a l'interieur d'un transaction.atomic() existant.
    Retourne le nombre d'entrees restaurees.
    """
    qs = EmploisArchive.objects.filter(annee_universitaire=annee)
    if type_semestre:
        qs = qs.filter(type_semestre=type_semestre)
    if dept_ids is not None:
        qs = qs.filter(departement_id__in=list(dept_ids))

    archives = list(qs)
    logger.info('restaurer_depuis_archive: %d entrees trouvees pour annee=%s ts=%s depts=%s',
                len(archives), annee, type_semestre, dept_ids or 'tous')
    if not archives:
        return 0

    with audit_aggregate_block():
        emplois = [
            Emplois(
                annee_universitaire=a.annee_universitaire,
                type_semestre=a.type_semestre,
                taux_paiement=a.taux_paiement,
                prof_id=a.prof_id, em_id=a.em_id, salle_id=a.salle_id,
                departement_id=a.departement_id, semestre_id=a.semestre_id,
                creneau_fk_id=a.creneau_fk_id,
                type_seance_fk_id=a.type_seance_fk_id,
                jour_fk_id=a.jour_fk_id,
                institution_id=a.institution_id,
            )
            for a in archives
        ]
        created = Emplois.objects.bulk_create(emplois)
        logger.info('restaurer_depuis_archive: %d Emplois restaures', len(created))

        # Vider l'archive APRES restauration (scope sur les memes depts si fourni)
        del_qs = EmploisArchive.objects.filter(annee_universitaire=annee)
        if type_semestre:
            del_qs = del_qs.filter(type_semestre=type_semestre)
        if dept_ids is not None:
            del_qs = del_qs.filter(departement_id__in=list(dept_ids))
        del_qs.delete()

    write_audit(
        action='RESTORE',
        model_name='Emplois',
        object_id='0',
        changes={'annee': annee, 'type_semestre': type_semestre, 'count': len(created)},
        label=f'Restauration EDT {annee} {type_semestre}',
    )
    return len(created)
