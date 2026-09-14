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
    # La CLASSE D'ATTENTE est écartée. Elle porte bien le niveau, mais c'est un
    # conteneur : elle reste hors des emplois du temps, du suivi et des listes
    # d'appel, et son `groupe` vide la faisait élire la PREMIÈRE par le tri
    # ci-dessous. Les EM de S3 s'ancraient donc sur « MP » plutôt que sur
    # « MP A », et tout écran joignant l'EM à son département en excluant les
    # conteneurs les aurait perdus.
    candidates = Classe.objects.filter(
        institution=matiere.institution, niveau__niveau=niveau_code, actif=True,
    ).exclude(groupe='')

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

    Les ÉPREUVES de la semaine y sont projetées aussi, sans quoi elles
    resteraient impointables : le Suivi ne lit que `Emplois`, et une épreuve
    n'est pas une séance. Une ligne par (épreuve, classe convoquée), ANCRÉE SUR
    LE PREMIER créneau qu'elle recouvre — une ligne par créneau couvert aurait
    demandé trois pointages pour un seul examen, et gonflé ses heures d'autant.

    Renvoie le nombre de lignes projetées, 0 si la semaine n'a ni séance réelle
    ni épreuve (l'appelant retombe alors sur la grille type).
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
    convocations = _convocations_de_la_semaine(
        annee_universitaire, type_semestre, numero_semaine)

    if not seances and not convocations:
        return 0

    # Le remplacement couvre les classes concernées ET leurs sous-groupes : une
    # séance déplacée d'un groupe à l'autre laisserait sinon un résidu.
    classes = ({s.classe_id for s in seances}
               | {c.classe_id for c in convocations})
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
    lignes.extend(_lignes_epreuves(convocations, annee_universitaire, type_semestre))

    Emplois.objects.bulk_create(lignes)
    return len(lignes)


def _convocations_de_la_semaine(annee_universitaire, type_semestre, numero_semaine):
    """Les couples (épreuve, classe) qui tombent dans cette semaine."""
    from .models import EpreuveClasse, SemaineIPGEI

    codes = [code for code, (_, t) in SEMESTRES.items() if t == type_semestre]
    semaine = (SemaineIPGEI.objects
               .filter(numero=numero_semaine,
                       semestre__annee_universitaire=annee_universitaire,
                       semestre__code__in=codes)
               .first())
    if semaine is None:
        return []
    return list(
        EpreuveClasse.objects
        .filter(epreuve__date__gte=semaine.date_debut,
                epreuve__date__lte=semaine.date_fin)
        .select_related('epreuve__matiere', 'classe')
    )


def _lignes_epreuves(convocations, annee_universitaire, type_semestre):
    """
    Les épreuves, projetées dans `Emplois` pour devenir pointables.

    NI ENSEIGNANT NI SALLE : ils restent vides, comme partout ailleurs. Le
    surveillant relève de la vacation, et la salle se décide sur les fiches de
    répartition. L'écran de remplissage les affiche « — », ce qui est exact.

    L'ancrage se fait sur le PREMIER créneau recouvert. Un examen de 4h en
    couvre trois : autant de lignes aurait demandé trois pointages pour une
    seule épreuve, et compté 4h30 de surveillance au lieu de 4h.

    Une épreuve logée hors créneau — dans la pause de 13h00 à 15h10 — n'est pas
    projetée : elle n'a aucune case où se pointer. Le cas est rare et vaut mieux
    qu'une ligne accrochée à un créneau arbitraire.
    """
    from apps.parametres.models import Creneau, Jour, Seance

    if not convocations:
        return []

    creneaux = list(Creneau.objects.filter(is_actif=True).order_by('ordre', 'id'))
    # `date.weekday()` rend 0 pour lundi ; le référentiel `Jour` est libellé en
    # toutes lettres et son ordre n'est pas garanti — le nom est la seule
    # correspondance stable entre une date et une ligne de la grille.
    NOMS = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche']
    jours = {j.jour.lower(): j for j in Jour.objects.all()}
    types = {s.type_seance: s for s in Seance.objects.all()}

    lignes = []
    for convocation in convocations:
        epreuve = convocation.epreuve
        ancre = next((c for c in creneaux if epreuve.recouvre(c)), None)
        jour  = jours.get(NOMS[epreuve.date.weekday()])
        type_seance = types.get(epreuve.type_epreuve)
        if ancre is None or jour is None or type_seance is None:
            logger.warning(
                'Épreuve %s non projetée : créneau=%s jour=%s type=%s',
                epreuve.pk, bool(ancre), bool(jour), bool(type_seance))
            continue

        matiere = epreuve.matiere
        em = (matiere.em_miroir or synchroniser_matiere(matiere)) if matiere else None
        lignes.append(Emplois(
            annee_universitaire=annee_universitaire,
            type_semestre=type_semestre,
            prof=None,
            em=em,
            departement_id=convocation.classe_id,
            salle=None,
            semestre=semestre_socle(matiere.code_semestre) if matiere else None,
            creneau_fk=ancre,
            type_seance_fk=type_seance,
            jour_fk=jour,
            institution=convocation.classe.institution,
        ))
    return lignes


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


# ═════════════════════════════════════════════════════════════════════════════
# Présentation des absences IPGEI au module ABS du socle
# ═════════════════════════════════════════════════════════════════════════════
#: Traduction des statuts IPGEI vers ceux du socle (`absence.Presence`).
#: Le retard n'y a pas d'équivalent — le socle ne connaît que présent, absent,
#: sanctionné et justifiée. Il n'est donc pas présenté plutôt que rangé sous un
#: libellé qui dirait autre chose que ce qui s'est passé.
_STATUT_SOCLE = {'absent': 1, 'justifiee': 3}


def absences_pour_socle(etudiant_id: int, date_debut=None, date_fin=None) -> list:
    """
    Les absences IPGEI d'un étudiant, au format des lignes du module ABS.

    Le socle attache ses présences à une ligne de SUIVI ; IPGEI les attache à
    la séance réelle. Les deux saisies sont légitimes, mais elles ne vivent pas
    dans la même table, et les écrans « ABS par étudiant » ne lisaient que la
    première : une absence pointée à la feuille d'appel d'une épreuve
    n'apparaissait nulle part, et l'écran affichait « aucune absence » — ce qui
    est pire que de ne rien afficher du tout.

    Les lignes rendues sont en LECTURE SEULE : leur identifiant est négatif,
    de sorte qu'aucune action du module (justificatif, changement de statut) ne
    puisse viser par mégarde une ligne qui n'existe pas dans `absence_presence`.
    Elles se corrigent depuis Évaluations → Émargement des épreuves.

    Les dates portent sur la SÉANCE, seule date qui ait un sens ici : celle où
    l'étudiant n'était pas là.
    """
    from .models import AbsenceSeance

    lignes = (
        AbsenceSeance.objects
        .filter(inscription__etudiant_id=etudiant_id,
                statut__in=tuple(_STATUT_SOCLE))
        .select_related('inscription__etudiant', 'inscription__classe',
                        'seance__matiere', 'seance__prof', 'seance__salle',
                        'seance__jour', 'seance__creneau', 'seance__type_seance',
                        'seance__semaine')
    )
    if date_debut:
        lignes = lignes.filter(seance__date__gte=date_debut)
    if date_fin:
        lignes = lignes.filter(seance__date__lte=date_fin)

    # Ce qui est DÉJÀ au registre du socle n'est pas présenté une seconde fois.
    # Une absence reportée par `refleter_absences` y figure comme une présence
    # ordinaire : la rendre aussi ici la ferait compter double, et l'écran
    # annoncerait deux absences pour une seule épreuve manquée.
    from apps.absence.models import Presence

    deja = {
        (p['suivi__date_suivie'], p['suivi__em_id'])
        for p in Presence.objects
        .filter(etudiant_id=etudiant_id)
        .values('suivi__date_suivie', 'suivi__em_id')
    }

    presentees = []
    for ligne in lignes:
        seance   = ligne.seance
        etudiant = ligne.inscription.etudiant
        if (seance.date, getattr(seance.matiere, 'em_miroir_id', None)) in deja:
            continue
        presentees.append({
            # Négatif : la ligne ne vient pas de `absence_presence`, et rien
            # dans le module ne doit croire pouvoir la modifier.
            'id':                 -ligne.pk,
            'suivi':              None,
            'etudiant':           etudiant.pk,
            'etudiant_nom':       etudiant.nom,
            'etudiant_matricule': etudiant.matricule,
            'statut':             _STATUT_SOCLE[ligne.statut],
            'statut_label':       ligne.get_statut_display(),
            'commentaire':        ligne.justificatif or 'Émargement de la séance (IPGEI)',
            'justificatif':       None,
            'date_modification':  ligne.date_saisie.isoformat(),
            'suivi_jour':         seance.jour.jour if seance.jour_id else None,
            'suivi_date':         seance.date.isoformat() if seance.date else None,
            'suivi_semaine':      seance.semaine.numero if seance.semaine_id else None,
            'suivi_prof':         seance.prof.nom if seance.prof_id else None,
            'suivi_em':           seance.matiere.intitule if seance.matiere_id else None,
            'suivi_salle':        seance.salle.nom if seance.salle_id else None,
            'suivi_departement':  ligne.inscription.classe.nom,
            'suivi_creneau':      seance.creneau.creneau if seance.creneau_id else None,
            'suivi_type':         (seance.type_seance.type_seance
                                   if seance.type_seance_id else None),
        })
    return presentees


def refleter_absences(annee_universitaire: str, type_semestre: str,
                      numero_semaine=None) -> int:
    """
    Reporte l'émargement IPGEI dans le registre du socle (`absence.Presence`).

    **Le registre est celui du socle** — c'est la règle déjà posée par
    `services.bilan_absences` : « deux registres finissent par diverger ». La
    feuille d'appel d'une séance IPGEI est une SAISIE, pas un second registre :
    ce qu'elle relève doit rejoindre `absence_presence`, faute de quoi les
    écrans ABS, les rapports et les fiches ignorent les épreuves.

    Le raccord passe par la ligne de SUIVI, qui identifie la séance de la même
    façon des deux côtés : classe, matière (par son miroir), jour, créneau,
    type de séance et semaine. Une absence dont la séance n'a pas encore de
    ligne de Suivi n'est pas perdue — elle sera reportée à la génération de la
    semaine, qui appelle cette fonction.

    Idempotent : rejouer ne crée pas de doublon, `Presence` étant unique par
    (suivi, étudiant). Ne touche JAMAIS une ligne pointée à la main dans le
    module ABS avec un autre statut que celui d'origine : on ne récrit que ce
    qui vient de l'émargement.

    Renvoie le nombre de présences écrites ou mises à jour.
    """
    from apps.absence.models import Presence
    from apps.suivi.models import Suivie

    from .models import AbsenceSeance

    absences = (
        AbsenceSeance.objects
        .filter(statut__in=tuple(_STATUT_SOCLE),
                seance__semaine__semestre__annee_universitaire=annee_universitaire,
                seance__annulee=False)
        .select_related('seance__matiere', 'seance__semaine', 'inscription')
    )
    if numero_semaine is not None:
        absences = absences.filter(seance__semaine__numero=numero_semaine)

    absences = [a for a in absences
                if a.seance.semaine.semestre.type_semestre == type_semestre]
    if not absences:
        return 0

    # Les lignes de Suivi candidates, indexées par ce qui identifie une séance.
    # Un seul aller en base : la semaine d'une classe en porte des dizaines.
    lignes = Suivie.objects.filter(
        annee_universitaire=annee_universitaire, type_semestre=type_semestre,
    )
    if numero_semaine is not None:
        lignes = lignes.filter(numero_semaine=numero_semaine)

    index = {
        (s.numero_semaine, s.departement_id, s.em_id, s.jour_fk_id,
         s.creneau_fk_id, s.type_seance_fk_id): s.pk
        for s in lignes
    }

    ecrites = 0
    for absence in absences:
        seance = absence.seance
        matiere = seance.matiere
        if matiere is None or matiere.em_miroir_id is None:
            continue
        # Le sous-groupe de TP porte sa propre ligne de Suivi ; une épreuve,
        # elle, se tient en classe entière. On cherche donc l'unité telle que
        # la projection l'a écrite.
        unite = seance.sous_groupe_id or seance.classe_id
        cle = (seance.semaine.numero, unite, matiere.em_miroir_id,
               seance.jour_id, seance.creneau_id, seance.type_seance_id)
        suivi_id = index.get(cle)
        if suivi_id is None:
            continue

        Presence.objects.update_or_create(
            suivi_id=suivi_id, etudiant_id=absence.inscription.etudiant_id,
            defaults={
                'statut':      _STATUT_SOCLE[absence.statut],
                'commentaire': absence.justificatif
                               or "Émargement de l'épreuve (IPGEI)",
            },
        )
        ecrites += 1
    return ecrites
