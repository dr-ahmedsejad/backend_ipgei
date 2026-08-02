"""
Archivage de l'emploi du temps au moment où le suivi en est tiré.

`SeanceReelle` est modifiée sur place : rien n'y garde la version d'avant. Or
c'est sur une version précise que les heures ont été pointées, et parfois
payées. Ce module fige cette version-là, classe par classe, à l'instant où le
suivi est généré.

L'archive est autoportante : aucun lien vers le référentiel, uniquement des
libellés figés. Renommer une salle ou supprimer un enseignant ne doit pas
récrire le passé.
"""
import logging

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .models import EmploiArchive, SeanceReelle, SemaineIPGEI
from .socle import SEMESTRES

logger = logging.getLogger(__name__)


def _instantane(seance, semaine, version, genere_le):
    """Une séance, recopiée en valeurs — plus aucune clé étrangère à suivre."""
    return EmploiArchive(
        semaine=semaine,
        classe_id=seance.classe_id,
        version=version,
        genere_le=genere_le,

        jour_ref=seance.jour_id,
        creneau_ref=seance.creneau_id,
        creneau_ordre=seance.creneau.ordre if seance.creneau_id else 0,
        matiere_ref=seance.matiere_id,
        type_seance_ref=seance.type_seance_id,
        sous_groupe_ref=seance.sous_groupe_id,
        prof_ref=seance.prof_id,
        salle_ref=seance.salle_id,

        jour_libelle=seance.jour.jour if seance.jour_id else '',
        creneau_libelle=seance.creneau.creneau if seance.creneau_id else '',
        classe_nom=seance.classe.nom if seance.classe_id else '',
        sous_groupe_libelle=(seance.sous_groupe.groupe
                             if seance.sous_groupe_id else ''),
        matiere_code=seance.matiere.code if seance.matiere_id else '',
        matiere_intitule=seance.matiere.intitule if seance.matiere_id else '',
        type_seance_libelle=(seance.type_seance.type_seance
                             if seance.type_seance_id else ''),
        type_seance_special=bool(seance.type_seance_id
                                 and seance.type_seance.is_special),
        prof_nom=seance.prof.nom if seance.prof_id else '',
        prof_initial_nom=seance.prof_initial.nom if seance.prof_initial_id else '',
        salle_nom=seance.salle.nom if seance.salle_id else '',

        date_seance=seance.date,
        origine=seance.origine,
        annulee=seance.annulee,
    )


@transaction.atomic
def archiver_semaine(annee: str, type_semestre: str, numero_semaine: int) -> int:
    """
    Fige l'emploi du temps de la semaine, une version par classe.

    Appelé après la génération du suivi. Idempotent sur le principe qu'une
    régénération produit une NOUVELLE version : l'ancienne n'est jamais
    écrasée, sinon l'archive ne servirait qu'une fois — celle où l'on ne s'est
    pas trompé.

    Une classe sans séance cette semaine-là n'est pas archivée : une grille
    vide n'apprend rien et polluerait le sélecteur de versions.

    Retourne le nombre de séances figées. Sans effet hors IPGEI.
    """
    codes = [c for c, (_, t) in SEMESTRES.items() if t == type_semestre]
    semaines = SemaineIPGEI.objects.filter(
        numero=numero_semaine,
        semestre__annee_universitaire=annee,
        semestre__code__in=codes,
    )
    if not semaines:
        return 0

    genere_le = timezone.now()
    total = 0

    for semaine in semaines:
        seances = list(
            SeanceReelle.objects
            .filter(semaine=semaine)
            .select_related('classe', 'jour', 'creneau', 'matiere', 'prof',
                            'prof_initial', 'salle', 'sous_groupe', 'type_seance')
        )
        if not seances:
            continue

        # Une version par classe : deux responsables peuvent générer leur
        # semaine à des moments différents, et chacun doit retrouver la sienne.
        deja = {
            ligne['classe_id']: ligne['v']
            for ligne in (EmploiArchive.objects
                          .filter(semaine=semaine)
                          .values('classe_id')
                          .annotate(v=Max('version')))
        }

        lot = []
        for seance in seances:
            version = deja.get(seance.classe_id, 0) + 1
            lot.append(_instantane(seance, semaine, version, genere_le))

        EmploiArchive.objects.bulk_create(lot)
        total += len(lot)

    logger.info('Archivage EDT : %s séance(s) figée(s) pour la semaine %s (%s %s)',
                total, numero_semaine, annee, type_semestre)
    return total
