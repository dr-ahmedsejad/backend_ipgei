"""
Miroir IPGEI → socle SIGA.

Les écrans **Suivi**, **Vacations**, **Avancement** et **Statistiques** sont
repris de SIGA à l'identique. Or ils raisonnent en `departement.Departement` et
`em.EM`, là où IPGEI raisonne en `ipgei.Classe` et `ipgei.Matiere`. Sans
contrepartie, ces écrans resteraient désespérément vides.

Ce module tient donc à jour un équivalent SIGA des objets IPGEI qui n'en sont
pas déjà un :

    ipgei.Matiere     ──> em.EM  (+ parametres.Semestre, parametres.Niveau)
    ipgei.SeanceType  ──> emplois.Emplois      (grille type = EDT hebdomadaire)
    ipgei.SemaineIPGEI──> parametres.Semaine   (calendrier de datation)

Les deux derniers sont indispensables au **Suivi** : il génère ses lignes depuis
`Emplois`, qu'il date via `parametres.Semaine`. Sans eux, « Générer la semaine »
ne produit rien, et toute la chaîne pointage → charge → vacations reste morte.

**Les classes ne sont plus miroitées** : `ipgei.Classe` et `ipgei.SousGroupeTP`
héritent de `departement.Departement` (héritage multi-table), ils *sont* donc
des départements. Rien à copier, rien à resynchroniser, rien qui diverge.

La clé de liaison restante est portée côté IPGEI (`Matiere.em_miroir`) : le
socle reste inchangé, donc resynchronisable avec SIGA.

Particularités IPGEI respectées :
  * niveaux **MPSI** et **MP** (et non L1/L2/L3) ;
  * semestres **S1..S4**, S1/S3 impairs et S2/S4 pairs ;
  * une **filière technique unique** « IPGEI » porte départements et EM. Elle
    n'est pas un programme LMD : c'est la clé dont le socle a besoin, car il
    dérive le lien groupe ↔ EM de (filière + semestre). Sans elle, l'écran
    « Gérer les emplois » ne proposait aucune matière ;
  * un **TP dédoublé se planifie sur son sous-groupe**, qui est un département
    à part entière : les deux groupes restent distincts au pointage, dans le
    calcul de charge et sur l'attestation de vacation.
"""
import logging

from django.db import transaction

from datetime import timedelta

from apps.em.models import EM
from apps.emplois.models import Emplois
from apps.parametres.models import Jour, Semaine as SemaineSocle

from .models import (
    Classe, GrilleType, Matiere, SeanceReelle, SemaineIPGEI, SemestreIPGEI,
    SousGroupeTP,
)
from .socle import (
    INDEX_JOUR, SEMESTRES, filiere_socle, semestre_socle,
)

logger = logging.getLogger('siga')


# ═════════════════════════════════════════════════════════════════════════════
# Matiere → EM
# ═════════════════════════════════════════════════════════════════════════════
def _classe_ancre(matiere: Matiere):
    """
    Classe servant d'ancrage à l'EM miroir.

    Le socle rattache chaque EM à un département, et l'écran « Avancement par
    matière » s'en sert pour retrouver les matières : sans ce rattachement,
    aucune matière IPGEI n'y apparaissait.

    Une matière de prépa vaut pour tout un niveau et pour toutes les années :
    l'ancrage ne sert qu'à lui donner un niveau, jamais à la dater. D'où la
    règle de stabilité ci-dessous — **on garde l'ancrage existant tant qu'il
    reste valable**. Sans elle, créer une classe pour l'année suivante
    déplaçait toutes les matières vers cette classe à la première
    resynchronisation, et l'avancement de l'année en cours se vidait.
    """
    niveau_code = SEMESTRES[matiere.code_semestre][0]
    candidates = Classe.objects.filter(
        institution=matiere.institution, niveau__niveau=niveau_code, actif=True,
    )

    if matiere.em_miroir_id and matiere.em_miroir.departement_id:
        actuel = candidates.filter(pk=matiere.em_miroir.departement_id).first()
        if actuel is not None:
            return actuel

    return candidates.order_by('annee_universitaire', 'groupe').first()


@transaction.atomic
def synchroniser_matiere(matiere: Matiere) -> EM:
    """
    Crée ou met à jour l'EM SIGA équivalent d'une matière.

    Les volumes CM / TD / TP sont repris tels quels : le calcul de vacation
    applique un tarif par type d'heure, une ventilation approximative fausserait
    donc directement la paie.

    Le code EM est suffixé par le semestre (« MATH-S1 »). Le socle impose en
    effet un code unique par filière, or la même matière existe aux quatre
    semestres du cursus : sans ce suffixe, MATH-S2 écraserait MATH-S1.

    Le niveau s'y ajoute dès que deux d'entre eux partagent un rang — MP et MPI
    suivent tous deux S3, et « MATH-S3 » désignerait alors deux matières
    distinctes. Le suffixe n'apparaît que dans ce cas : l'ajouter partout
    renommerait les EM du cursus d'origine, et avec eux les vacations déjà
    saisies.
    """
    code_em = f'{matiere.code}-{matiere.code_semestre}'
    if matiere.niveau_ref_id is not None:
        from .models import NiveauIPGEI
        rang_partage = NiveauIPGEI.objects.filter(
            institution=matiere.institution, rang=matiere.niveau_ref.rang,
        ).count() > 1
        if rang_partage:
            code_em = f'{code_em}-{matiere.niveau_ref.code}'

    valeurs = {
        'code_em':     code_em,
        'intitule':    matiere.intitule,
        'CM':          matiere.volume_cm,
        'TD':          matiere.volume_td,
        'TP':          matiere.volume_tp,
        'PR':          0,
        'semestre':    semestre_socle(matiere.code_semestre),
        'coefficient': int(matiere.coefficient),
        'credits':     None,
        'filiere':     filiere_socle(matiere.institution),
        'departement': _classe_ancre(matiere),
    }

    em = matiere.em_miroir
    if em is None:
        em = EM.objects.create(**valeurs)
        Matiere.objects.filter(pk=matiere.pk).update(em_miroir=em)
    else:
        for champ, valeur in valeurs.items():
            setattr(em, champ, valeur)
        em.save(update_fields=list(valeurs))
    return em


# ═════════════════════════════════════════════════════════════════════════════
# Calendrier : plus de miroir, le socle est la source
# ═════════════════════════════════════════════════════════════════════════════
# `parametres.Semaine` — l'écran « Paramètres → Semaines » — est désormais le
# SEUL calendrier. `services.generer_semaines()` en tire les semaines IPGEI.
#
# Le report inverse a été retiré : chacun générant ses semaines de son côté
# depuis les dates du semestre, les deux calendriers divergeaient en silence.
# On a vu des semaines présentes à l'écran du socle rester introuvables à la
# duplication de la grille.


# ═════════════════════════════════════════════════════════════════════════════
# SeanceType → emplois.Emplois  (EDT hebdomadaire lu par le Suivi)
# ═════════════════════════════════════════════════════════════════════════════
@transaction.atomic
def synchroniser_grille(grille: GrilleType) -> int:
    """
    Reporte la grille type d'une classe dans `emplois.Emplois`.

    `Emplois` est le patron hebdomadaire du socle : même granularité que la
    grille type IPGEI (jour × créneau), d'où une correspondance directe.

    On remplace l'intégralité du jeu pour ce (département, année, période)
    plutôt que de le modifier ligne à ligne : la grille type EST la définition
    complète de l'emploi du temps de cette classe, et un remplacement franc
    évite les résidus d'une séance supprimée entre deux synchronisations.

    Une séance rattachée à un sous-groupe est planifiée **sur ce sous-groupe**,
    qui est lui aussi un département : c'est ce qui permet de départager les
    deux moitiés d'un TP dédoublé au pointage, dans le calcul de charge et sur
    l'attestation. Les séances en classe entière restent sur la classe.
    """
    classe = grille.classe

    # Le nettoyage couvre la classe ET ses sous-groupes : une séance déplacée
    # de la classe vers un groupe (ou l'inverse) laisserait sinon un résidu.
    unites = [classe.pk, *classe.sous_groupes.values_list('pk', flat=True)]
    Emplois.objects.filter(
        departement_id__in=unites,
        annee_universitaire=classe.annee_universitaire,
        type_semestre=grille.type_semestre,
    ).delete()

    lignes = []
    seances = grille.seances.select_related(
        'matiere', 'prof', 'salle', 'jour', 'creneau', 'sous_groupe',
    )
    for seance in seances:
        # Une séance spéciale n'a pas de matière, donc pas d'EM : elle bloque
        # le créneau sans porter d'enseignement.
        matiere = seance.matiere
        em = (matiere.em_miroir or synchroniser_matiere(matiere)) if matiere else None
        lignes.append(Emplois(
            annee_universitaire=classe.annee_universitaire,
            type_semestre=grille.type_semestre,
            prof=seance.prof,
            em=em,
            departement_id=seance.sous_groupe_id or classe.pk,
            salle=seance.salle,
            semestre=semestre_socle(matiere.code_semestre) if matiere else None,
            creneau_fk=seance.creneau,
            type_seance_fk=seance.type_seance,
            jour_fk=seance.jour,
            institution=classe.institution,
        ))
    Emplois.objects.bulk_create(lignes)
    return len(lignes)


@transaction.atomic
def projeter_semaine_reelle(annee_universitaire: str, type_semestre: str,
                            numero_semaine) -> int:
    """
    Projette dans `emplois.Emplois` les séances RÉELLES d'une semaine donnée.

    C'est le raccord qui manquait entre l'emploi du temps et le Suivi. Ce
    dernier génère ses lignes depuis `Emplois` ; tant qu'`Emplois` était une
    copie de la grille type, tout ce qui distingue une semaine du patron —
    permutation d'enseignant, séance annulée, séance ajoutée — restait invisible
    du Suivi, donc de la charge et des vacations. Le remplaçant n'était pas
    payé, le remplacé l'était.

    Deux règles portent tout le correctif :
      * le professeur projeté est `SeanceReelle.prof`, c'est-à-dire l'enseignant
        EFFECTIF après permutation ;
      * les séances annulées ne sont pas projetées — il n'y a ni cours à
        pointer, ni heure à payer.

    Renvoie le nombre de lignes projetées, 0 si la semaine n'a aucune séance
    réelle (l'appelant retombe alors sur la grille type).
    """
    # `SemestreIPGEI.type_semestre` est une propriété dérivée du code, pas un
    # champ : le filtre doit passer par les codes (S1/S3 impairs, S2/S4 pairs).
    codes = [code for code, (_, type_) in SEMESTRES.items() if type_ == type_semestre]

    seances = list(
        SeanceReelle.objects
        .filter(
            annulee=False,
            semaine__numero=numero_semaine,
            semaine__semestre__annee_universitaire=annee_universitaire,
            semaine__semestre__code__in=codes,
        )
        .select_related('classe', 'matiere', 'prof', 'salle',
                        'jour', 'creneau', 'sous_groupe')
    )
    if not seances:
        return 0

    # Le remplacement couvre les classes concernées ET leurs sous-groupes : une
    # séance déplacée d'un groupe à l'autre laisserait sinon un résidu.
    classes = {s.classe_id for s in seances}
    unites = set(classes) | set(
        SousGroupeTP.objects.filter(classe_id__in=classes).values_list('pk', flat=True)
    )
    Emplois.objects.filter(
        departement_id__in=unites,
        annee_universitaire=annee_universitaire,
        type_semestre=type_semestre,
    ).delete()

    lignes = []
    for seance in seances:
        # Une séance spéciale n'a pas de matière, donc pas d'EM : elle bloque
        # le créneau sans porter d'enseignement.
        matiere = seance.matiere
        em = (matiere.em_miroir or synchroniser_matiere(matiere)) if matiere else None
        lignes.append(Emplois(
            annee_universitaire=annee_universitaire,
            type_semestre=type_semestre,
            prof=seance.prof,
            em=em,
            departement_id=seance.sous_groupe_id or seance.classe_id,
            salle=seance.salle,
            semestre=semestre_socle(matiere.code_semestre) if matiere else None,
            creneau_fk=seance.creneau,
            type_seance_fk=seance.type_seance,
            jour_fk=seance.jour,
            institution=seance.classe.institution,
        ))
    Emplois.objects.bulk_create(lignes)
    return len(lignes)


def rematerialiser_emplois(annee_universitaire: str, type_semestre: str,
                           numero_semaine=None) -> int:
    """
    Reconstruit `emplois.Emplois` avant la génération d'une semaine de Suivi.

    La génération archive puis VIDE `Emplois` — c'est le fonctionnement de
    SIGA, où l'emploi du temps se ressaisit chaque période. À l'IPGEI la grille
    type est permanente : on la reprojette donc avant chaque génération, faute
    de quoi la deuxième semaine ne produirait que des lignes vides.

    Quand la semaine est précisée, la projection part de ses séances RÉELLES —
    seule façon de faire parvenir permutations et annulations jusqu'au Suivi.
    À défaut de séances réelles pour cette semaine (grille jamais dupliquée),
    on retombe sur la grille type : mieux vaut un suivi fondé sur le patron
    qu'aucun suivi du tout.

    Idempotent, et sans effet si aucune grille ne correspond.
    """
    if numero_semaine is not None:
        projetees = projeter_semaine_reelle(
            annee_universitaire, type_semestre, numero_semaine,
        )
        if projetees:
            return projetees

    total = 0
    grilles = (
        GrilleType.objects
        .filter(actif=True, type_semestre=type_semestre,
                classe__annee_universitaire=annee_universitaire)
        .select_related('classe')
    )
    for grille in grilles:
        total += synchroniser_grille(grille)
    return total


def diagnostic_edt(annee_universitaire: str, type_semestre: str) -> str:
    """
    Explique, en vocabulaire IPGEI, pourquoi aucun emploi du temps n'est
    disponible pour une période.

    Le message natif du socle parle d'« importer ou restaurer un EDT » : à
    l'IPGEI on ne fait ni l'un ni l'autre, on remplit la grille type de la
    période. Un message juste mais intraduisible dans les gestes de
    l'utilisateur ne l'aide pas.
    """
    periode = 'impaire (S1 / S3)' if type_semestre == 'I' else 'paire (S2 / S4)'
    classes = Classe.objects.filter(
        annee_universitaire=annee_universitaire, actif=True,
    )
    if not classes.exists():
        return (f"Aucune classe active en {annee_universitaire}. "
                "Créez-en une dans « Classes & sous-groupes ».")

    grilles = GrilleType.objects.filter(
        type_semestre=type_semestre,
        classe__annee_universitaire=annee_universitaire,
        classe__actif=True,
    )
    remplies = [g for g in grilles if g.seances.exists()]
    if remplies:
        return ''                      # il y a de quoi générer

    sans = ', '.join(c.nom for c in classes)
    return (
        f"Aucune grille type renseignée pour la période {periode} en "
        f"{annee_universitaire}. Ouvrez « Gérer les emplois », sélectionnez une "
        f"classe ({sans}) et renseignez sa grille, puis revenez générer le suivi."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Rattrapage global
# ═════════════════════════════════════════════════════════════════════════════
def synchroniser_tout(institution=None) -> dict:
    """
    Rejoue le miroir sur l'ensemble des matières, du calendrier et des grilles.

    Les classes n'y figurent plus : elles héritent de `Departement` et n'ont
    donc pas de contrepartie à tenir à jour. Sert au premier branchement comme
    après un import : la synchronisation est idempotente, relancer ne duplique
    rien.
    """
    classes  = Classe.objects.all()
    matieres = Matiere.objects.all()
    if institution is not None:
        classes  = classes.filter(institution=institution)
        matieres = matieres.filter(institution=institution)

    for matiere in matieres:
        synchroniser_matiere(matiere)

    # Les matières doivent exister en EM avant que les grilles ne les référencent.
    # Le calendrier, lui, se reprend du socle : `services.generer_semaines()`
    # en tire les semaines IPGEI, il n'y a rien à repousser dans l'autre sens.
    from . import services      # import local : `services` importe ce module

    semaines = 0
    for semestre in SemestreIPGEI.objects.all():
        semaines += services.generer_semaines(semestre)

    seances = 0
    for grille in GrilleType.objects.select_related('classe'):
        seances += synchroniser_grille(grille)

    return {
        'classes':  classes.count(),
        'matieres': matieres.count(),
        'semaines': semaines,
        'seances':  seances,
    }
