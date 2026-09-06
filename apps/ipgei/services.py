"""
Règles métier IPGEI — calcul des notes, délibération, EDT et permutations.

Toute la logique vit ici plutôt que dans les vues : les mêmes règles servent
l'API, l'admin Django, les commandes de gestion et la génération de documents.
"""
import threading
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Prefetch
from django.utils import timezone

from .models import (
    NIVEAU_MP, NIVEAU_MPSI, SEMESTRES_PAR_NIVEAU, SEMESTRES_PAR_RANG,
    AbsenceSeance, AnonymatIPGEI, Classe, Deliberation, DetailLigneDeliberation,
    Evaluation,
    GrilleType, HistoriqueClasse, InscriptionIPGEI, LigneDeliberation, Matiere,
    MembreJuryIPGEI,
    Note, ParametresIPGEI, PermutationEtudiant, PermutationProf, SeanceReelle,
    SemaineIPGEI, SemestreIPGEI, SessionEvaluationIPGEI, SousGroupeTP,
    StatutWorkflow,
)

CENT = Decimal('100')
DEUX_DECIMALES = Decimal('0.01')


def _arrondi(valeur):
    """Arrondit une note à 2 décimales (demi vers le haut), en tolérant None."""
    if valeur is None:
        return None
    return Decimal(valeur).quantize(DEUX_DECIMALES, rounding=ROUND_HALF_UP)


def _moyenne_arithmetique(valeurs, tenues: int = 0):
    """
    Moyenne arithmétique des épreuves, l'épreuve manquée comptant ZÉRO.

    `tenues` est le nombre d'épreuves que la CLASSE a passées dans cette
    matière — le dénominateur. Sans lui, la moyenne se ferait sur les seules
    copies rendues, et manquer une épreuve deviendrait un avantage : deux
    devoirs à 13 et 12 donnaient 12,50, un seul devoir à 15 donnait 15.

    Le dénominateur ne peut pas descendre sous le nombre de notes présentes :
    un étudiant ne saurait avoir plus d'épreuves que sa classe, mais si cela
    arrivait, diviser par moins fausserait la note à la hausse.

    Renvoie None si aucune épreuve n'a été tenue — un semestre qui commence
    n'a pas de moyenne, il n'a pas zéro.
    """
    valeurs = [Decimal(v) for v in valeurs if v is not None]
    diviseur = max(tenues, len(valeurs))
    if not diviseur:
        return None
    return _arrondi(sum(valeurs) / Decimal(diviseur))


def epreuves_tenues(matiere_id, semestre_id, classe_id) -> dict:
    """
    Combien d'épreuves de chaque type la CLASSE a passées dans cette matière.

    Une épreuve est réputée tenue dès qu'un seul étudiant en porte la note :
    c'est le seul signal disponible, et il est juste — on ne saisit pas la note
    d'un devoir qui n'a pas eu lieu.

    Conséquence à connaître : saisir le deuxième devoir d'UN étudiant change le
    dénominateur de TOUTE la classe. C'est pourquoi une saisie ne recalcule
    jamais une ligne seule, mais le groupe entier (`recalculer_groupe`).
    """
    def compter(mid, types):
        lignes = (
            Evaluation.objects
            .filter(note__matiere_id=mid, note__semestre_id=semestre_id,
                    note__inscription__classe_id=classe_id,
                    type_evaluation__in=types)
            .values('type_evaluation')
            .annotate(n=Count('numero', distinct=True))
        )
        return {l['type_evaluation']: l['n'] for l in lignes}

    porteuse_id = porteuse_de(matiere_id)
    if porteuse_id == matiere_id:
        return compter(matiere_id, [Evaluation.TYPE_DS, Evaluation.TYPE_EXAM])

    # Enseignement partagé : les devoirs sont composés sous la matière porteuse.
    # Les compter ici donnerait zéro et effacerait leur part de la moyenne.
    epreuves = compter(matiere_id, [Evaluation.TYPE_EXAM])
    epreuves.update(compter(porteuse_id, [Evaluation.TYPE_DS]))
    return epreuves


def porteuse_de(matiere_id):
    """
    Identifiant de la matière où se saisissent les DS et le TP de celle-ci.

    Rend `matiere_id` lui-même quand la matière n'est rattachée à aucune autre —
    le cas courant. Voir `Matiere.partage_avec`.
    """
    from .models import Matiere

    partage = (Matiere.objects.filter(pk=matiere_id)
               .values_list('partage_avec_id', flat=True).first())
    return partage or matiere_id


def note_porteuse(note):
    """
    La ligne de note où lire les DS et le TP de celle-ci, ou None.

    None quand la matière n'est rattachée à rien — ou quand la porteuse n'a pas
    encore de ligne pour cet étudiant : DS et TP sont alors simplement absents,
    et `calculer_note` neutralise leur part. Il n'y a PAS de repli sur les
    évaluations de la rattachée : un DS saisi là avant le rattachement ne doit
    pas ressurgir, sans quoi deux étudiants n'auraient pas la même règle.
    """
    from .models import Note

    if note.matiere.partage_avec_id is None:
        return None
    return (Note.objects
            .filter(inscription_id=note.inscription_id,
                    semestre_id=note.semestre_id,
                    matiere_id=note.matiere.partage_avec_id)
            .prefetch_related('evaluations')
            .first())


# ═════════════════════════════════════════════════════════════════════════════
# Fenêtres de saisie (sessions normale / rattrapage)
# ═════════════════════════════════════════════════════════════════════════════
def session_saisie(semestre: SemestreIPGEI, type_session: str) -> SessionEvaluationIPGEI:
    """
    Session d'un semestre, créée au premier accès si elle manque.

    Créer à la demande plutôt que d'exiger un peuplement préalable évite qu'un
    semestre importé ou créé hors de l'API se retrouve sans fenêtre de saisie —
    ce qui bloquerait toute notation sans que rien ne l'explique. Les états par
    défaut reconduisent le comportement d'avant les sessions : la saisie normale
    est ouverte tant que le semestre l'est, le rattrapage attend une décision
    explicite de la scolarité.
    """
    normale = type_session == SessionEvaluationIPGEI.TYPE_NORMALE
    session, cree = SessionEvaluationIPGEI.objects.get_or_create(
        semestre=semestre, type_session=type_session,
        defaults={
            'est_ouverte': normale and not semestre.est_cloture,
            'est_close':   semestre.est_cloture,
            'ouverte_le':  timezone.now() if normale and not semestre.est_cloture else None,
            # Le plafond n'a de sens que sur le rattrapage ; il est figé ici,
            # une fois pour toutes, depuis le paramétrage du jour.
            'plafond_rattrapage': (
                None if normale else ParametresIPGEI.pour(semestre.institution).plafond_rattrapage
            ),
        },
    )
    return session


def sessions_du_semestre(semestre: SemestreIPGEI) -> dict:
    """Les deux sessions d'un semestre, indexées par leur type."""
    return {
        type_session: session_saisie(semestre, type_session)
        for type_session, _ in SessionEvaluationIPGEI.TYPE_CHOICES
    }


def annee_cloturee(annee_universitaire: str) -> bool:
    """
    L'année universitaire est-elle close au registre du socle ?

    La clôture d'une année est une décision administrative, prise dans
    « Paramètres → Années » ; elle ne se déduit pas de l'état des semestres, et
    l'ignorer laissait saisir des notes sur une année officiellement fermée.
    """
    from apps.parametres.models import Year

    return Year.objects.filter(
        annee=annee_universitaire, est_cloturee=True,
    ).exists()


def controler_saisie(semestre: SemestreIPGEI, *, rattrapage: bool = False) -> SessionEvaluationIPGEI:
    """
    Vérifie qu'une note peut être saisie ; lève `ValidationError` sinon.

    Deux verrous indépendants, et il faut les deux : l'année close au registre
    du socle, et le semestre clôturé au calendrier IPGEI. Le premier est une
    décision d'établissement, le second l'état du calendrier.

    Renvoie la session concernée, pour que l'appelant en tire le plafond sans
    la rechercher une seconde fois.
    """
    if annee_cloturee(semestre.annee_universitaire):
        raise ValidationError(
            f'Année universitaire {semestre.annee_universitaire} clôturée — '
            'saisie impossible.'
        )
    if semestre.est_cloture:
        raise ValidationError('Semestre clôturé — saisie impossible.')

    type_session = (SessionEvaluationIPGEI.TYPE_RATTRAPAGE if rattrapage
                    else SessionEvaluationIPGEI.TYPE_NORMALE)
    session = session_saisie(semestre, type_session)
    if session.est_close:
        raise ValidationError(
            f'{session.get_type_session_display()} clôturée sur {semestre.code} — '
            'saisie impossible.'
        )
    if not session.est_ouverte:
        raise ValidationError(
            f'{session.get_type_session_display()} pas encore ouverte sur '
            f'{semestre.code} — ouvrez-la avant de saisir.'
        )
    return session


def controler_recalcul(semestre: SemestreIPGEI) -> None:
    """
    Vérifie qu'un recalcul de notes est encore permis ; lève sinon.

    `controler_saisie` gardait la saisie, mais deux chemins recalculaient sans
    lui : la correction d'une évaluation isolée et le recalcul de masse d'un
    semestre. Or un recalcul réécrit les moyennes de toute la classe — et les
    pondérations « figées » sur chaque note — donc, sur une année close, ce que
    le jury a arrêté. Mêmes verrous que la saisie, hors état de campagne : un
    recalcul n'est pas une campagne.
    """
    if annee_cloturee(semestre.annee_universitaire):
        raise ValidationError(
            f'Année universitaire {semestre.annee_universitaire} clôturée — '
            'recalcul impossible.'
        )
    if semestre.est_cloture:
        raise ValidationError('Semestre clôturé — recalcul impossible.')


def plafond_effectif(semestre: SemestreIPGEI):
    """
    Plafond de rattrapage à appliquer aux notes de ce semestre.

    Celui figé sur la session de rattrapage prime sur le paramétrage courant :
    une campagne close doit se recalculer à l'identique, même si l'institution
    a changé sa règle depuis.
    """
    return session_saisie(semestre, SessionEvaluationIPGEI.TYPE_RATTRAPAGE).plafond_rattrapage


@transaction.atomic
def ouvrir_session(session: SessionEvaluationIPGEI, utilisateur=None) -> SessionEvaluationIPGEI:
    """
    Ouvre une campagne de saisie.

    Le rattrapage suppose des moyennes arrêtées : l'ouvrir alors que la session
    normale accepte encore des DS produirait des rattrapages calculés sur une
    moyenne qui bouge encore.
    """
    if session.semestre.est_cloture:
        raise ValidationError('Semestre clôturé — réouvrez le semestre d\'abord.')
    if session.est_close:
        raise ValidationError('Session clôturée — sa réouverture est réservée à un administrateur.')
    if session.type_session == SessionEvaluationIPGEI.TYPE_RATTRAPAGE:
        normale = session_saisie(session.semestre, SessionEvaluationIPGEI.TYPE_NORMALE)
        if not normale.est_close:
            raise ValidationError(
                'La session normale doit être clôturée avant d\'ouvrir le rattrapage : '
                'sans moyennes arrêtées, un rattrapage ne veut rien dire.'
            )
    session.est_ouverte = True
    session.ouverte_le  = timezone.now()
    session.save(update_fields=['est_ouverte', 'ouverte_le'])
    _tracer_session(session, 'Ouverture', utilisateur)
    return session


@transaction.atomic
def cloturer_session(session: SessionEvaluationIPGEI, utilisateur=None) -> SessionEvaluationIPGEI:
    """Ferme la campagne : plus aucune saisie tant qu'un admin ne réouvre pas."""
    if session.est_close:
        raise ValidationError('Session déjà clôturée.')
    session.est_close    = True
    session.est_ouverte  = False
    session.cloturee_le  = timezone.now()
    session.cloturee_par = utilisateur
    session.save(update_fields=['est_close', 'est_ouverte', 'cloturee_le', 'cloturee_par'])
    _tracer_session(session, 'Clôture', utilisateur)
    return session


@transaction.atomic
def rouvrir_session(session: SessionEvaluationIPGEI, utilisateur=None) -> SessionEvaluationIPGEI:
    """
    Réouvre une campagne close. Le contrôle du profil appartient à la vue —
    ici on garantit seulement que l'acte reste tracé.
    """
    if not session.est_close:
        raise ValidationError('Session déjà ouverte.')
    session.est_close    = False
    session.est_ouverte  = True
    session.ouverte_le   = timezone.now()
    session.cloturee_le  = None
    session.cloturee_par = None
    session.save(update_fields=[
        'est_close', 'est_ouverte', 'ouverte_le', 'cloturee_le', 'cloturee_par',
    ])
    _tracer_session(session, 'Réouverture', utilisateur)
    return session


def _tracer_session(session: SessionEvaluationIPGEI, acte: str, utilisateur) -> None:
    """
    Journalise l'ouverture ou la fermeture d'une campagne de notes.

    `SessionEvaluationIPGEI` n'est pas dans TRACKED_MODELS : sans cet appel
    explicite, savoir qui a rouvert une session close serait impossible.
    """
    try:
        from core.audit_helpers import write_audit
        write_audit(
            action='UPDATE', model_name='SessionEvaluationIPGEI', object_id=str(session.pk),
            changes={'etat': session.etat, 'par': getattr(utilisateur, 'username', None)},
            label=f'{acte} — {session}', keep_forever=True,
        )
    except Exception:                                       # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            'Audit %s session %s échoué', acte.lower(), session.pk, exc_info=True,
        )


# ═════════════════════════════════════════════════════════════════════════════
# Calcul d'une note de matière
# ═════════════════════════════════════════════════════════════════════════════
def calculer_note(note: Note, *, plafond_rattrapage=None, sauvegarder=True,
                  epreuves: dict = None, rattrapage_seulement: bool = False,
                  partagee: Note = None) -> Note:
    """
    Recalcule une ligne de note et **fige la pondération** de la matière dedans.

    moy_DS   = moyenne arithmétique des DS, l'épreuve manquée comptant ZÉRO
    moy_exam = idem pour les examens
    moyenne  = pct_ds·moy_DS + pct_tp·TP + pct_exam·moy_exam

    Le diviseur est le nombre d'épreuves que la CLASSE a passées, non celui des
    copies rendues par l'étudiant : sans quoi manquer un devoir serait un
    avantage — 13 et 12 donnaient 12,50, un seul 15 donnait 15.

    Une composante sans note (aucun DS saisi, pas de TP…) est neutralisée : sa
    part est redistribuée sur les composantes présentes, faute de quoi un
    semestre en cours afficherait des moyennes artificiellement basses.

    La note retenue applique ensuite la règle SIGA `max(moyenne, rattrapage)`,
    bornée par le plafond éventuel.
    """
    if note.verrouillee and not rattrapage_seulement:
        raise ValidationError('Note verrouillée par une délibération validée — recalcul refusé.')

    if rattrapage_seulement:
        # Le verrou protège CE QUE LE JURY A VU : les notes de la session
        # normale, arrêtées le jour où il a siégé. Le rattrapage, lui, se
        # compose APRÈS — l'interdire revenait à rendre la seconde chance
        # inapplicable, puisque valider le jury annuel figeait tout.
        #
        # On ne recalcule donc que la note RETENUE, à partir de la moyenne déjà
        # figée : les composantes du jury ne bougent pas d'un centième.
        note.plafond_rattrapage_applique = plafond_rattrapage
        note.note_retenue = _appliquer_rattrapage(
            note.moyenne, note.note_rattrapage, plafond_rattrapage)
        note.date_calcul = timezone.now()
        if sauvegarder:
            note.save(update_fields=['note_retenue', 'plafond_rattrapage_applique',
                                     'date_calcul', 'date_modification'])
        return note

    evaluations = list(note.evaluations.all())
    # Le dénominateur est celui de la CLASSE : une épreuve manquée compte zéro.
    # Fourni par l'appelant quand il recalcule tout un groupe — sinon une
    # requête par ligne, ce qui se paie sur une classe de quarante.
    if epreuves is None:
        epreuves = epreuves_tenues(note.matiere_id, note.semestre_id,
                                   note.inscription.classe_id)

    matiere = note.matiere
    # Enseignement partagé : DS et TP viennent de la matière porteuse — les
    # devoirs sont composés une fois et valent pour les deux, le TP aussi ;
    # seul l'examen est propre à cette ligne. `partagee` est fourni par
    # `recalculer_notes`, qui charge les lignes porteuses en une requête.
    if matiere.partage_avec_id is not None:
        if partagee is None:
            partagee = note_porteuse(note)
        evaluations_ds = list(partagee.evaluations.all()) if partagee is not None else []
        # La valeur partagée devient CELLE DE LA LIGNE : le relevé, le PV et
        # l'écran de saisie disent alors tous la même chose, sans avoir à
        # remonter jusqu'à la porteuse pour la lire.
        note.note_tp = partagee.note_tp if partagee is not None else None
    else:
        evaluations_ds = evaluations

    moy_ds   = _moyenne_arithmetique(
        [e.valeur for e in evaluations_ds if e.type_evaluation == Evaluation.TYPE_DS],
        epreuves.get(Evaluation.TYPE_DS, 0),
    )
    moy_exam = _moyenne_arithmetique(
        [e.valeur for e in evaluations if e.type_evaluation == Evaluation.TYPE_EXAM],
        epreuves.get(Evaluation.TYPE_EXAM, 0),
    )

    # Snapshot : à partir d'ici, on ne lit plus la matière — la pondération de
    # cette ligne est celle du jour du calcul, définitivement.
    note.pct_ds      = matiere.pct_ds
    note.pct_tp      = matiere.pct_tp if matiere.has_tp else Decimal('0')
    note.pct_exam    = matiere.pct_exam
    note.coefficient = matiere.coefficient
    note.moy_ds      = moy_ds
    note.moy_exam    = moy_exam

    note_tp = note.note_tp if matiere.has_tp else None

    composantes = [
        (moy_ds,   Decimal(note.pct_ds or 0)),
        (note_tp,  Decimal(note.pct_tp or 0)),
        (moy_exam, Decimal(note.pct_exam or 0)),
    ]
    presentes = [(v, p) for v, p in composantes if v is not None and p > 0]
    total_poids = sum(p for _, p in presentes)

    if not presentes or total_poids == 0:
        note.moyenne = None
    else:
        cumul = sum(Decimal(v) * p for v, p in presentes)
        note.moyenne = _arrondi(cumul / total_poids)

    note.plafond_rattrapage_applique = plafond_rattrapage
    note.note_retenue = _appliquer_rattrapage(
        note.moyenne, note.note_rattrapage, plafond_rattrapage,
    )
    note.date_calcul = timezone.now()

    if sauvegarder:
        note.save(update_fields=[
            'moy_ds', 'moy_exam', 'moyenne', 'note_retenue',
            'pct_ds', 'pct_tp', 'pct_exam', 'coefficient', 'note_tp',
            'plafond_rattrapage_applique', 'date_calcul', 'date_modification',
        ])
    return note


def _appliquer_rattrapage(moyenne, rattrapage, plafond):
    """
    Règle SIGA reprise telle quelle : on retient la meilleure des deux notes.
    Le plafond ne s'applique qu'à la note issue du rattrapage — il ne peut
    jamais faire baisser une moyenne obtenue en session normale.
    """
    if rattrapage is None:
        return _arrondi(moyenne)
    valeur_rattrapage = Decimal(rattrapage)
    if plafond is not None:
        valeur_rattrapage = min(valeur_rattrapage, Decimal(plafond))
    if moyenne is None:
        return _arrondi(valeur_rattrapage)
    return _arrondi(max(Decimal(moyenne), valeur_rattrapage))


def recalculer_notes(queryset, *, plafond_rattrapage=None,
                     rattrapage_seulement: bool = False) -> int:
    """
    Recalcule un lot de notes ; renvoie le nombre de lignes traitées.

    Le nombre d'épreuves tenues est calculé UNE fois par groupe
    (matière × semestre × classe) et non par ligne : sur une classe de
    quarante, ce serait quarante requêtes identiques.
    """
    total = 0
    qs = queryset if rattrapage_seulement else queryset.filter(verrouillee=False)
    notes = list(qs.select_related('matiere', 'inscription')
                   .prefetch_related('evaluations'))

    # Enseignement partagé : les lignes porteuses en UNE requête pour tout le
    # lot, plutôt qu'une par élève — même motif que le cache des épreuves.
    partagees = {}
    besoins = [(n.inscription_id, n.semestre_id, n.matiere.partage_avec_id)
               for n in notes if n.matiere.partage_avec_id]
    if besoins:
        partagees = {
            (p.inscription_id, p.semestre_id, p.matiere_id): p
            for p in Note.objects
            .filter(inscription_id__in={b[0] for b in besoins},
                    semestre_id__in={b[1] for b in besoins},
                    matiere_id__in={b[2] for b in besoins})
            .prefetch_related('evaluations')
        }

    cache = {}
    for note in notes:
        cle = (note.matiere_id, note.semestre_id, note.inscription.classe_id)
        if cle not in cache:
            cache[cle] = epreuves_tenues(*cle)
        calculer_note(note, plafond_rattrapage=plafond_rattrapage,
                      epreuves=cache[cle],
                      rattrapage_seulement=rattrapage_seulement,
                      partagee=partagees.get((note.inscription_id, note.semestre_id,
                                              note.matiere.partage_avec_id)))
        total += 1
    return total


def recalculer_groupe(matiere_id, semestre_id, classe_id, *, plafond_rattrapage=None) -> int:
    """
    Recalcule TOUTES les notes d'une matière pour une classe.

    Indispensable depuis que l'épreuve manquée compte zéro : saisir le deuxième
    devoir d'un seul étudiant fait passer la classe de un à deux devoirs tenus,
    et change donc le diviseur de tout le monde. Ne recalculer que la ligne
    saisie laisserait les trente-neuf autres sur l'ancien diviseur, sans que
    rien ne le signale.

    Les matières RATTACHÉES à celle-ci sont recalculées dans la foulée : elles
    lisent ses DS et son TP, et personne n'irait rouvrir leur grille après avoir
    saisi un devoir — elles resteraient sur d'anciennes valeurs.
    """
    from .models import Matiere

    ids = [matiere_id] + list(Matiere.objects
                              .filter(partage_avec_id=matiere_id)
                              .values_list('id', flat=True))
    return recalculer_notes(
        Note.objects.filter(matiere_id__in=ids, semestre_id=semestre_id,
                            inscription__classe_id=classe_id),
        plafond_rattrapage=plafond_rattrapage,
    )


@transaction.atomic
def initialiser_notes(inscription: InscriptionIPGEI, semestre: SemestreIPGEI) -> int:
    """
    Crée les lignes de note manquantes d'un étudiant pour toutes les matières
    actives du semestre. Idempotent : relancer n'écrase aucune saisie.

    La maquette retenue est celle du NIVEAU, pas seulement celle du semestre :
    deux niveaux d'un même rang — MP et MPI — partagent S3 et S4 en suivant des
    programmes différents. Les matières antérieures au référentiel n'ont pas de
    niveau ; elles appartiennent au cursus d'origine, donc on les conserve.
    """
    from django.db.models import Q

    fiche = niveau_ref(inscription.classe.niveau_code, semestre.institution)
    matieres = Matiere.objects.filter(
        institution=semestre.institution, code_semestre=semestre.code, actif=True,
    ).filter(
        Q(niveau_ref=fiche) | Q(niveau_ref__isnull=True) if fiche is not None
        else Q(niveau_ref__isnull=True)
    )
    existantes = set(
        Note.objects.filter(inscription=inscription, semestre=semestre)
        .values_list('matiere_id', flat=True)
    )
    nouvelles = [
        Note(inscription=inscription, matiere=m, semestre=semestre)
        for m in matieres if m.id not in existantes
    ]
    Note.objects.bulk_create(nouvelles)
    return len(nouvelles)


def moyenne_semestrielle(inscription: InscriptionIPGEI, semestre: SemestreIPGEI,
                         *, absents_a_zero: bool = False):
    """
    Moyenne pondérée par les coefficients sur un semestre.
    Renvoie (moyenne, total_coefficients, [notes]).

    `absents_a_zero` : règle du jury — une matière sans note vaut zéro, avec
    son coefficient. Réservé au calcul de la délibération ; les écrans de
    saisie et les relevés gardent la moyenne des seules matières notées, sans
    quoi toute promotion afficherait des moyennes catastrophiques tant que la
    saisie n'est pas terminée.
    """
    notes = list(
        Note.objects.filter(inscription=inscription, semestre=semestre)
        .select_related('matiere')
    )
    return _agreger(
        notes, absents_a_zero=absents_a_zero,
        maquette=maquette_du_semestre(inscription, semestre) if absents_a_zero else None,
    )


def _agreger(notes, *, absents_a_zero: bool = False, maquette=None):
    """
    Agrège des lignes de note en moyenne pondérée par coefficient.

    `absents_a_zero` compte ZÉRO toute matière sans note retenue, avec son
    coefficient — la règle du jury : une épreuve non rendue vaut zéro, elle ne
    s'efface pas du calcul. Sans cela, un étudiant n'ayant composé que dans
    trois matières obtenait la moyenne de ces trois-là, et passait devant ceux
    qui avaient tout rendu.

    `maquette` fournit les matières ATTENDUES. Elle est indispensable : une
    matière jamais saisie n'a pas de ligne de note du tout, et compter zéro
    seulement les lignes présentes laisserait un semestre entier hors du calcul.
    """
    cumul  = Decimal('0')
    poids  = Decimal('0')
    notees = set()
    for n in notes:
        coef = Decimal(n.coefficient if n.coefficient is not None else n.matiere.coefficient)
        if n.note_retenue is None:
            if not absents_a_zero:
                continue
            # Zéro : on ajoute le poids sans rien au cumul.
            poids += coef
            notees.add(n.matiere_id)
            continue
        # Coefficient snapshoté s'il existe, sinon celui de la matière : une
        # note jamais calculée ne doit pas faire disparaître la matière du total.
        cumul += Decimal(n.note_retenue) * coef
        poids += coef
        notees.add(n.matiere_id)

    if absents_a_zero and maquette is not None:
        for matiere in maquette:
            if matiere.pk not in notees:
                poids += Decimal(matiere.coefficient)

    if poids == 0:
        return None, Decimal('0'), notes
    return _arrondi(cumul / poids), poids, notes


def maquette_du_semestre(inscription: InscriptionIPGEI, semestre):
    """Matières attendues d'un étudiant sur un semestre — sa maquette."""
    from .models import Matiere

    return list(Matiere.objects.filter(
        institution=inscription.classe.institution,
        niveau_ref__code=inscription.niveau,
        code_semestre=semestre.code,
        actif=True,
    ))


def maquette_de_lannee(inscription: InscriptionIPGEI, annee_universitaire: str):
    """Matières attendues sur toute l'année — les deux semestres du niveau."""
    from .models import Matiere

    return list(Matiere.objects.filter(
        institution=inscription.classe.institution,
        niveau_ref__code=inscription.niveau,
        code_semestre__in=codes_semestres_du_niveau(
            inscription.niveau, inscription.classe.institution),
        actif=True,
    ))


#: Poids de chaque SEMESTRE dans la moyenne annuelle, par rang d'année.
#:
#: La moyenne annuelle n'est PAS une moyenne pondérée sur toutes les matières
#: des deux semestres : c'est la moyenne des deux MOYENNES SEMESTRIELLES, et les
#: deux années n'y appliquent pas la même règle.
#:
#:   première année : (S1 × 2 + S2 × 3) / 5   — le second semestre pèse plus
#:   deuxième année : (S3 + S4) / 2           — les deux à égalité
#:
#: La nuance n'est pas cosmétique : agréger toutes les matières d'un coup donne
#: un autre nombre, car les deux semestres n'ont ni le même nombre de matières
#: ni le même total de coefficients. C'est la règle de l'établissement qui
#: tranche, pas l'arithmétique la plus commode.
PONDERATION_ANNUELLE = {
    1: {'S1': Decimal('2'), 'S2': Decimal('3')},
    2: {'S3': Decimal('1'), 'S4': Decimal('1')},
}


def moyenne_annuelle(inscription: InscriptionIPGEI, annee_universitaire: str,
                     *, absents_a_zero: bool = False):
    """
    Moyenne annuelle : la moyenne PONDÉRÉE DES MOYENNES SEMESTRIELLES.

    Voir `PONDERATION_ANNUELLE` pour les poids. Chaque moyenne de semestre reste,
    elle, pondérée par les coefficients de ses matières.

    `absents_a_zero` : règle du jury. Une matière sans note vaut zéro dans son
    semestre ; un semestre entièrement vide vaut zéro dans l'année, avec son
    poids. C'est ce qui permet de statuer sur un dossier incomplet plutôt que de
    le laisser en blanc. Sans ce drapeau — relevés, écrans de consultation — un
    semestre non saisi est simplement écarté et les poids se renormalisent : une
    promotion en cours de saisie n'a pas à afficher des moyennes catastrophiques.

    Rend `(moyenne, total_coefficients, notes)`. Le total des coefficients reste
    celui des MATIÈRES de l'année — c'est lui que le PV affiche, et il ne se
    confond pas avec la somme des poids de semestre.
    """
    codes = codes_semestres_du_niveau(inscription.niveau,
                                      inscription.classe.institution)
    notes = list(
        Note.objects.filter(
            inscription=inscription,
            semestre__annee_universitaire=annee_universitaire,
            semestre__code__in=codes,
        ).select_related('matiere', 'semestre')
    )

    maquette = (maquette_de_lannee(inscription, annee_universitaire)
                if absents_a_zero else None)
    # Le total des coefficients, inchangé : c'est la colonne « Total coef. » du
    # PV, et elle doit continuer de dire le poids de l'année en matières.
    _, poids_matieres, _ = _agreger(notes, absents_a_zero=absents_a_zero,
                                    maquette=maquette)

    rang = rang_du_niveau(inscription.niveau, inscription.classe.institution)
    poids_semestres = PONDERATION_ANNUELLE.get(rang) or {}

    cumul = Decimal('0')
    poids_retenu = Decimal('0')
    for code in codes:
        poids = poids_semestres.get(code)
        if poids is None:
            continue
        lot = [n for n in notes if n.semestre.code == code]
        attendues = ([m for m in maquette if m.code_semestre == code]
                     if maquette is not None else None)
        moyenne_semestre, _, _ = _agreger(lot, absents_a_zero=absents_a_zero,
                                          maquette=attendues)
        if moyenne_semestre is None:
            # Semestre muet : il compte zéro pour le jury — une année à moitié
            # faite ne vaut pas une année réussie — et s'efface ailleurs.
            if not absents_a_zero:
                continue
            moyenne_semestre = Decimal('0')
        cumul += Decimal(moyenne_semestre) * poids
        poids_retenu += poids

    moyenne = _arrondi(cumul / poids_retenu) if poids_retenu else None
    return moyenne, poids_matieres, notes


# ═════════════════════════════════════════════════════════════════════════════
# Délibération
# ═════════════════════════════════════════════════════════════════════════════
_referentiel_local = threading.local()


@contextmanager
def cache_referentiel():
    """
    Mémorise les fiches `NiveauIPGEI` le temps d'un bloc.

    Le référentiel fait deux lignes, mais chaque moyenne le relit — par
    inscription, par semestre, par candidate de première année : 269 lectures
    pour un classement de 23 lignes. Le cache ne vit que dans le bloc : rien
    ne survit à la requête HTTP, rien ne peut devenir périmé.
    """
    if getattr(_referentiel_local, 'niveaux', None) is not None:
        yield                                   # bloc imbriqué : celui du dessus tient
        return
    _referentiel_local.niveaux = {}
    try:
        yield
    finally:
        _referentiel_local.niveaux = None


def niveau_ref(code: str, institution=None):
    """
    Fiche du référentiel correspondant à un code de niveau, ou None.

    Le code reste la clé d'usage : classes, semestres et délibérations le
    portent en texte. Cette recherche est le pont vers les règles associées.
    """
    from .models import NiveauIPGEI

    memo = getattr(_referentiel_local, 'niveaux', None)
    cle  = (code, getattr(institution, 'pk', institution))
    if memo is not None and cle in memo:
        return memo[cle]

    requete = NiveauIPGEI.objects.filter(code=code)
    if institution is not None:
        requete = requete.filter(institution=institution)
    fiche = requete.first()
    if memo is not None:
        memo[cle] = fiche
    return fiche


def rang_du_niveau(code: str, institution=None) -> int:
    """
    Rang d'année d'un niveau : 1 pour une première année, 2 pour une deuxième.

    Repli sur le cursus d'origine quand le référentiel ne connaît pas le code —
    une base d'avant la reprise, ou un code saisi à la main.
    """
    fiche = niveau_ref(code, institution)
    if fiche is not None:
        return fiche.rang
    return 1 if code == NIVEAU_MPSI else 2


def rang_du_semestre(code_semestre: str) -> int:
    """Rang d'année auquel appartient un semestre : S1/S2 en 1re, S3/S4 en 2e."""
    for rang, codes in SEMESTRES_PAR_RANG.items():
        if code_semestre in codes:
            return rang
    return 1


def codes_semestres_du_niveau(code: str, institution=None) -> list:
    """Codes de semestres suivis à ce niveau — ceux de son rang."""
    return list(SEMESTRES_PAR_RANG.get(rang_du_niveau(code, institution), []))


def semestres_incomplets(inscription: InscriptionIPGEI, annee_universitaire: str) -> list:
    """
    Codes des semestres de l'année où il manque au moins une note.

    Une décision de passage porte sur l'ANNÉE entière. Tant qu'un semestre n'est
    pas noté, la moyenne ne repose que sur l'autre : `_agreger` ignore les notes
    vides, si bien qu'un étudiant n'ayant fait que S1 obtenait une moyenne et,
    avec elle, un « admis en 2e année » avant même d'avoir commencé S2.
    """
    codes = codes_semestres_du_niveau(inscription.niveau,
                                      inscription.classe.institution)
    notes = Note.objects.filter(
        inscription=inscription,
        semestre__annee_universitaire=annee_universitaire,
        semestre__code__in=codes,
    ).select_related('semestre')

    manquants, vus = [], set()
    for code in codes:
        lot = [n for n in notes if n.semestre.code == code]
        vus.add(code)
        # Aucune ligne du tout, ou une ligne sans note retenue : dans les deux
        # cas le semestre n'a pas été jugé.
        if not lot or any(n.note_retenue is None for n in lot):
            manquants.append(code)
    return manquants


def decision_automatique(niveau: str, moyenne, seuil, nb_redoublements: int,
                         droit_redoublement_max: int, institution=None) -> str:
    """
    Décision proposée par le calcul (plan §4.4).

    Première année : admis si la moyenne atteint le seuil, sinon réorienté — ni
    rattrapage, ni redoublement.

    Deuxième année, JURY ANNUEL : deux issues et deux seulement. Le seuil est
    atteint et l'étudiant est autorisé à concourir ; il ne l'est pas et il passe
    par le RATTRAPAGE. On ne va jamais au concours en sautant le rattrapage, et
    le redoublement ne se prononce pas ici — il ne vient qu'après la session de
    rattrapage, ou après un échec au concours, selon que le droit reste ouvert.

    C'est ce que faisait le calcul auparavant : sous le seuil, il proposait
    d'emblée « redoublant », ou « exclu » une fois le droit épuisé. Un étudiant
    était donc renvoyé de l'établissement sans avoir composé sa seconde chance.

    Le rang et le droit au redoublement viennent du référentiel : un niveau
    ajouté — MPI par exemple — suit la règle de son rang sans que ce code ait à
    le connaître. Une décision retirée des `decisions_possibles` du niveau n'est
    jamais proposée, quitte à ne rien proposer du tout : mieux vaut laisser le
    jury trancher que suggérer ce qu'il s'est interdit.
    """
    if moyenne is None:
        return ''

    fiche    = niveau_ref(niveau, institution)
    rang     = fiche.rang if fiche is not None else rang_du_niveau(niveau, institution)
    permises = set(fiche.decisions_possibles or []) if fiche is not None else set()
    atteint  = Decimal(moyenne) >= Decimal(seuil)

    def retenue(decision: str) -> str:
        return decision if (not permises or decision in permises) else ''

    if rang == 1:
        return retenue(LigneDeliberation.DECISION_ADMIS if atteint
                       else LigneDeliberation.DECISION_REORIENTE)
    if atteint:
        return retenue(LigneDeliberation.DECISION_CNIM)
    return retenue(LigneDeliberation.DECISION_RATTRAPAGE)


def decision_apres_seconde_chance(inscription, droit_redoublement_max: int,
                                  institution=None) -> str:
    """
    Ce qu'il advient d'un étudiant qui n'ira pas au concours.

    Deux cas de figure, un seul embranchement : le jury de rattrapage ne l'a pas
    autorisé à concourir, ou il a concouru sans succès. Dans les deux, la suite
    ne dépend que de son droit de redoublement — il redouble s'il lui en reste,
    il est réorienté sinon.

    « Réorienté » et non « exclu » : l'établissement ne renvoie pas, il oriente
    ailleurs. Le mot figure sur une pièce remise à l'étudiant.
    """
    fiche = niveau_ref(inscription.niveau, institution
                       or inscription.classe.institution)
    ouvert = (fiche.redoublement_autorise if fiche is not None
              else rang_du_niveau(inscription.niveau) > 1)
    if ouvert and inscription.nb_redoublements < droit_redoublement_max:
        return LigneDeliberation.DECISION_REDOUBLANT
    return LigneDeliberation.DECISION_REORIENTE


def grille_rattrapage(deliberation: Deliberation) -> dict:
    """
    La grille de saisie du rattrapage : les étudiants du jury × TOUTES les
    matières de l'année.

    Toutes, et non celles où l'étudiant a échoué : c'est le jury qui décide où
    l'on repasse, et la règle retenue est `max(session normale, rattrapage)`.
    Une matière qu'on repasse sans l'améliorer ne fait donc aucun mal — mais une
    matière absente de la grille aurait interdit de la repasser.

    Chaque case rappelle la moyenne de la session normale : on ne saisit pas un
    rattrapage sans savoir ce qu'il doit dépasser.
    """
    codes = codes_semestres_du_niveau(deliberation.niveau, deliberation.institution)
    matieres = list(
        Matiere.objects
        .filter(niveau_ref__code=deliberation.niveau, code_semestre__in=codes,
                actif=True)
        .order_by('code_semestre', 'ordre', 'code'))

    lignes = list(deliberation.lignes
                  .select_related('inscription__etudiant', 'inscription__classe')
                  .order_by('rang', 'inscription__etudiant__nom'))
    inscriptions = [ligne.inscription for ligne in lignes]

    notes = {
        (note.inscription_id, note.matiere_id): note
        for note in Note.objects.filter(
            inscription__in=inscriptions,
            semestre__annee_universitaire=deliberation.annee_universitaire,
            matiere__in=matieres)
    }

    return {
        'matieres': [
            {'id': m.pk, 'code': m.code, 'intitule': m.intitule,
             'semestre': m.code_semestre, 'coefficient': m.coefficient}
            for m in matieres
        ],
        'lignes': [
            {
                'inscription':        ligne.inscription_id,
                'etudiant_nom':       ligne.inscription.etudiant.nom_display,
                'etudiant_matricule': ligne.inscription.etudiant.matricule,
                'classe_nom':         ligne.inscription.classe.nom,
                'moyenne_generale':   ligne.moyenne_generale,
                'decision_auto':      ligne.decision_auto,
                'notes': {
                    str(m.pk): {
                        'moyenne':       getattr(notes.get((ligne.inscription_id, m.pk)),
                                                 'moyenne', None),
                        'rattrapage':    getattr(notes.get((ligne.inscription_id, m.pk)),
                                                 'note_rattrapage', None),
                        'note_retenue':  getattr(notes.get((ligne.inscription_id, m.pk)),
                                                 'note_retenue', None),
                    }
                    for m in matieres
                },
            }
            for ligne in lignes
        ],
    }


@transaction.atomic
def saisir_rattrapage(deliberation: Deliberation, saisies: list) -> dict:
    """
    Enregistre les notes de rattrapage du jury, puis le recalcule.

    `saisies` : `[{'inscription': id, 'matiere': id, 'note_rattrapage': 12.5}]`.
    Une valeur nulle EFFACE le rattrapage — l'étudiant retombe alors sur sa
    moyenne de session normale, ce qui est le sens de « il ne s'est pas
    présenté ».

    Les notes sont verrouillées par le jury annuel : c'est normal, et seule la
    note de rattrapage peut encore les faire bouger. On ne recalcule donc que
    la note RETENUE — les composantes que le jury a arrêtées ne changent pas.

    Le jury est recalculé dans la foulée : sans cela, l'écran afficherait des
    décisions établies sur des moyennes périmées.
    """
    if deliberation.est_verrouillee:
        raise ValidationError(
            'Jury de rattrapage validé — la saisie est close. Dévalidez-le pour '
            'y revenir.')
    if deliberation.portee != Deliberation.PORTEE_RATTRAPAGE:
        raise ValidationError(
            "La saisie du rattrapage appartient au jury de RATTRAPAGE : c'est "
            'lui qui désigne les étudiants concernés.')

    permises = set(deliberation.lignes.values_list('inscription_id', flat=True))
    ecrites = 0
    for saisie in saisies:
        inscription_id = int(saisie['inscription'])
        if inscription_id not in permises:
            raise ValidationError(
                f"L'inscription {inscription_id} ne relève pas de ce jury de "
                'rattrapage.')
        matiere = Matiere.objects.filter(pk=saisie['matiere']).first()
        if matiere is None:
            raise ValidationError({'matiere': 'Matière introuvable.'})

        semestre = SemestreIPGEI.objects.filter(
            code=matiere.code_semestre,
            annee_universitaire=deliberation.annee_universitaire,
            institution=deliberation.institution).first()
        if semestre is None:
            raise ValidationError(
                f"{matiere.code_semestre} n'existe pas en "
                f'{deliberation.annee_universitaire} : la note ne peut se '
                'rattacher à aucun semestre.')

        note, _ = Note.objects.get_or_create(
            inscription_id=inscription_id, matiere=matiere, semestre=semestre)
        note.note_rattrapage = saisie.get('note_rattrapage')
        note.save(update_fields=['note_rattrapage', 'date_modification'])
        calculer_note(note, plafond_rattrapage=plafond_effectif(semestre),
                      rattrapage_seulement=True)
        ecrites += 1

    calculer_deliberation(deliberation)
    return {'saisies': ecrites}


@transaction.atomic
def ouvrir_jury_rattrapage(annuelle: Deliberation, utilisateur=None) -> Deliberation:
    """
    Ouvre le second jury de deuxième année, celui qui suit la session de
    rattrapage.

    Il reprend EXACTEMENT les étudiants que le jury annuel y a renvoyés — ni
    plus, ni moins. Une délibération à part, et non une reprise de la première :
    deux jurys siègent à deux dates et signent chacun son procès-verbal. Le PV
    annuel dit « X est autorisé au rattrapage », celui-ci dit ce qu'il en est
    advenu ; rouvrir le premier effacerait la première décision.

    Trois refus, et chacun dit ce qu'il faut faire :
      * le jury annuel doit être VALIDÉ — tant que ses décisions peuvent
        changer, la liste des candidats au rattrapage aussi ;
      * il doit avoir renvoyé quelqu'un au rattrapage, sinon il n'y a pas de
        session à délibérer ;
      * il ne s'ouvre qu'une fois : le second jury existant est rendu tel quel
        plutôt que dupliqué.

    Le seuil est celui du jury annuel, recopié : on ne juge pas la seconde
    chance à une exigence différente de la première.
    """
    if annuelle.portee != Deliberation.PORTEE_ANNUELLE:
        raise ValidationError(
            "Le jury de rattrapage suit un jury ANNUEL. Un jury de semestre est "
            "un bilan intermédiaire : il ne prononce rien qui l'appelle."
        )
    if not annuelle.est_verrouillee:
        raise ValidationError(
            'Validez le jury annuel avant : tant que ses décisions peuvent '
            'changer, la liste des étudiants envoyés au rattrapage aussi.'
        )

    deja = annuelle.jurys_rattrapage.first()
    if deja is not None:
        return deja

    attendus = annuelle.lignes.filter(
        decision=LigneDeliberation.DECISION_RATTRAPAGE).count()
    if not attendus:
        raise ValidationError(
            "Aucun étudiant n'a été autorisé au rattrapage par ce jury : il n'y "
            'a pas de session à délibérer.'
        )

    rattrapage = Deliberation.objects.create(
        institution=annuelle.institution,
        libelle='',
        niveau=annuelle.niveau,
        classe=annuelle.classe,
        annee_universitaire=annuelle.annee_universitaire,
        portee=Deliberation.PORTEE_RATTRAPAGE,
        jury_source=annuelle,
        seuil_validation=annuelle.seuil_validation,
        plafond_rattrapage=annuelle.plafond_rattrapage,
    )
    # Calculé d'emblée : la cohorte est connue, et un jury qu'il faut d'abord
    # « calculer » avant de voir qui il concerne se lit comme une coquille.
    calculer_deliberation(rattrapage)
    import logging
    logging.getLogger(__name__).info(
        'Jury de rattrapage %s ouvert depuis le jury annuel %s (%s étudiant(s))',
        rattrapage.pk, annuelle.pk, attendus)
    return rattrapage


def lacunes_deliberation(deliberation: Deliberation) -> list:
    """
    Les matières que le jury s'apprête à compter ZÉRO.

    La règle de l'épreuve non rendue est juste, mais elle est SILENCIEUSE : une
    matière jamais saisie n'a pas de ligne de note, elle entre au calcul par sa
    seule maquette, avec son coefficient et un cumul nul. Un semestre oublié
    fait alors passer une promotion entière sous le seuil sans qu'aucun écran
    ne l'annonce — c'est arrivé, et le jury ne l'a vu qu'aux moyennes.

    On rend donc ce que la règle coûte, matière par matière : combien
    d'étudiants du jury n'y ont pas de note retenue, et quel coefficient pèse
    dans le vide. Rien n'est bloqué pour autant — un jury peut parfaitement
    délibérer sur une matière non rendue. Il doit seulement le savoir.
    """
    from .models import Matiere, SemestreIPGEI

    lignes = list(deliberation.lignes.select_related(
        'inscription__classe__institution'))
    if not lignes:
        return []

    inscriptions = [ligne.inscription for ligne in lignes]
    reference    = inscriptions[0]
    institution  = reference.classe.institution

    # Un jury de semestre ne regarde que le sien ; l'annuel et le rattrapage
    # portent l'année entière — c'est sur elle qu'ils prononcent.
    if deliberation.semestre_id:
        codes = [deliberation.semestre.code]
    else:
        codes = codes_semestres_du_niveau(deliberation.niveau, institution)

    semestres = {
        semestre.pk: semestre
        for semestre in SemestreIPGEI.objects.filter(
            institution=institution, code__in=codes,
            annee_universitaire=deliberation.annee_universitaire)
    }
    if not semestres:
        return []

    maquette = Matiere.objects.filter(
        institution=institution, niveau_ref__code=deliberation.niveau,
        code_semestre__in=codes, actif=True,
    ).order_by('code_semestre', 'ordre', 'code')

    # Une seule requête pour toutes les notes du jury : le comptage se fait en
    # mémoire, sur quelques centaines de lignes au plus.
    notees = set(
        Note.objects.filter(
            inscription__in=inscriptions, semestre_id__in=semestres,
            note_retenue__isnull=False,
        ).values_list('semestre__code', 'matiere_id', 'inscription_id')
    )

    effectif = len(inscriptions)
    manquantes = []
    for matiere in maquette:
        sans = sum(
            1 for inscription in inscriptions
            if (matiere.code_semestre, matiere.pk, inscription.pk) not in notees
        )
        if not sans:
            continue
        manquantes.append({
            'semestre':    matiere.code_semestre,
            'matiere':     matiere.pk,
            'code':        matiere.code,
            'intitule':    matiere.intitule,
            # En chaîne comme partout ailleurs : les sérialiseurs rendent les
            # décimaux ainsi, et un dictionnaire brut sortirait un flottant —
            # deux formats pour un même coefficient selon l'endpoint.
            'coefficient': str(matiere.coefficient),
            'manquants':   sans,
            'effectif':    effectif,
            # Une matière que PERSONNE n'a passée se lit autrement qu'un absent
            # isolé : la première est un oubli de saisie, la seconde un cas.
            'totale':      sans == effectif,
        })
    manquantes.sort(key=lambda m: (-m['manquants'], m['semestre'], m['code']))
    return manquantes


@transaction.atomic
def calculer_deliberation(deliberation: Deliberation) -> int:
    """
    (Re)calcule toutes les lignes d'une délibération : moyennes, rangs,
    décisions proposées, et photo des notes par matière.

    Un ajustement déjà décidé par le jury est **conservé** — recalculer ne doit
    jamais effacer une décision humaine ; seule la proposition automatique est
    rafraîchie.
    """
    if deliberation.est_verrouillee:
        raise ValidationError('Délibération validée — recalcul interdit.')

    params = ParametresIPGEI.pour(deliberation.institution)
    droit_max = params.droit_redoublement_max

    # Le jury siège classe par classe : le rang qu'il arrête est le rang DANS
    # LA CLASSE. Mêler deux classes le ferait dépendre d'élèves que celui-ci ne
    # côtoie pas, et le PV d'une classe porterait le classement de l'autre.
    # Les jurys tenus avant la règle n'ont pas de classe : ils gardent leur
    # portée de niveau, sans quoi leurs lignes disparaîtraient au recalcul.
    inscriptions = (
        InscriptionIPGEI.objects
        .filter(
            annee_universitaire=deliberation.annee_universitaire,
            classe__institution=deliberation.institution,
            actif=True,
        )
        .select_related('etudiant', 'classe')
    )
    if deliberation.portee == Deliberation.PORTEE_RATTRAPAGE:
        # Le périmètre d'un jury de rattrapage n'est pas une classe : c'est la
        # liste exacte de ceux que le jury annuel y a renvoyés. Reprendre toute
        # la classe y ferait entrer des étudiants déjà autorisés à concourir, à
        # qui l'on redemanderait une décision.
        inscriptions = inscriptions.filter(
            pk__in=deliberation.jury_source.lignes
            .filter(decision=LigneDeliberation.DECISION_RATTRAPAGE)
            .values_list('inscription_id', flat=True))
    elif deliberation.classe_id:
        inscriptions = inscriptions.filter(classe_id=deliberation.classe_id)
    else:
        inscriptions = inscriptions.filter(
            classe__niveau__niveau=deliberation.niveau)

    # Devant le jury, une matière sans note vaut ZÉRO, avec son coefficient :
    # une épreuve non rendue ne s'efface pas du calcul. Sans cette règle, un
    # étudiant n'ayant composé que dans trois matières obtenait la moyenne de
    # ces trois-là et passait devant ceux qui avaient tout rendu.
    resultats = []
    for inscription in inscriptions:
        if deliberation.portee == Deliberation.PORTEE_SEMESTRE:
            moyenne, poids, notes = moyenne_semestrielle(
                inscription, deliberation.semestre, absents_a_zero=True)
        else:
            moyenne, poids, notes = moyenne_annuelle(
                inscription, deliberation.annee_universitaire,
                absents_a_zero=True,
            )
        resultats.append((inscription, moyenne, poids, notes))

    # Rang : les étudiants sans moyenne restent non classés plutôt que derniers.
    classables = sorted(
        [r for r in resultats if r[1] is not None], key=lambda r: r[1], reverse=True,
    )
    rangs = {r[0].id: i + 1 for i, r in enumerate(classables)}

    # Une décision de passage engage l'ANNÉE : un jury de semestre, simple bilan
    # intermédiaire, n'en propose pas.
    #
    # Un semestre non noté ne suspend PLUS la proposition. Il la suspendait
    # parce que la moyenne ne reposait alors que sur l'autre semestre, et qu'un
    # « admis » sortait avant même que le second ait commencé. Les matières
    # manquantes comptant désormais zéro, la moyenne dit le vrai : celui à qui
    # il manque un semestre est proposé pour ce qu'il a rendu, et le jury reste
    # libre de trancher autrement — c'est la *proposition* qu'on retient, pas
    # la décision.
    semestrielle = deliberation.portee == Deliberation.PORTEE_SEMESTRE
    rattrapage   = deliberation.portee == Deliberation.PORTEE_RATTRAPAGE

    for inscription, moyenne, poids, notes in resultats:
        if semestrielle:
            auto = ''
        elif rattrapage:
            # Après la seconde chance, deux issues et deux seulement : le seuil
            # est atteint et l'étudiant concourt ; il ne l'est pas et tout
            # dépend de son droit de redoublement. La moyenne intègre déjà les
            # notes de rattrapage — `note_retenue` vaut max(moyenne, rattrapage),
            # plafond compris.
            auto = (LigneDeliberation.DECISION_CNIM
                    if moyenne is not None
                    and Decimal(moyenne) >= Decimal(deliberation.seuil_validation)
                    else decision_apres_seconde_chance(
                        inscription, droit_max, deliberation.institution))
        else:
            auto = decision_automatique(
                inscription.niveau, moyenne, deliberation.seuil_validation,
                inscription.nb_redoublements, droit_max,
            )
        # Proposition d'avant recalcul : elle dit si la décision affichée vient
        # du jury ou de la machine.
        existante = LigneDeliberation.objects.filter(
            deliberation=deliberation, inscription=inscription,
        ).values('decision', 'decision_auto', 'motif_ajustement').first()

        ligne, cree = LigneDeliberation.objects.update_or_create(
            deliberation=deliberation, inscription=inscription,
            defaults={
                'moyenne_generale':   moyenne,
                'total_coefficients': poids,
                'rang':               rangs.get(inscription.id),
                'decision_auto':      auto,
            },
        )
        # La décision du jury prime : on ne (re)pose l'auto que si rien n'a été
        # arbitré. Une décision qui n'était que la proposition précédente n'a
        # rien d'arbitré — sans quoi une proposition retirée parce qu'elle était
        # prématurée resterait affichée pour toujours.
        jamais_arbitree = (
            cree
            or not ligne.decision
            or (existante is not None
                and not existante['motif_ajustement']
                and existante['decision'] == existante['decision_auto'])
        )
        if jamais_arbitree and ligne.decision != auto:
            ligne.decision = auto
            ligne.save(update_fields=['decision'])

        ligne.details.all().delete()
        DetailLigneDeliberation.objects.bulk_create([
            DetailLigneDeliberation(
                ligne=ligne, matiere=n.matiere, semestre=n.semestre,
                note_retenue=n.note_retenue,
                coefficient=(n.coefficient if n.coefficient is not None else n.matiere.coefficient),
            )
            for n in notes
        ])

    deliberation.statut      = Deliberation.STATUT_CALCULEE
    deliberation.date_calcul = timezone.now()
    deliberation.save(update_fields=['statut', 'date_calcul'])
    return len(resultats)


def notes_couvertes(deliberation: Deliberation):
    """
    Notes que le périmètre d'une délibération englobe.

    Une délibération de semestre ne couvre que le sien ; une annuelle couvre les
    deux semestres du niveau. Ce découpage sert à verrouiller à la validation,
    et à savoir quoi déverrouiller — ou pas — à la dévalidation.
    """
    notes = Note.objects.filter(
        inscription__in=deliberation.lignes.values_list('inscription_id', flat=True),
    )
    if deliberation.portee == Deliberation.PORTEE_SEMESTRE:
        return notes.filter(semestre=deliberation.semestre)
    # Le rattrapage couvre l'année, comme le jury annuel : c'est la moyenne
    # annuelle qu'il arrête, notes de rattrapage comprises.
    return notes.filter(
        semestre__annee_universitaire=deliberation.annee_universitaire,
        semestre__code__in=codes_semestres_du_niveau(deliberation.niveau,
                                                     deliberation.institution),
    )


@transaction.atomic
def valider_deliberation(deliberation: Deliberation, utilisateur) -> Deliberation:
    """
    Fige la délibération : verrouille les notes concernées et reporte chaque
    décision sur le statut de l'inscription. Le compteur de redoublement n'est
    incrémenté qu'ici, une seule fois, à la validation.

    **Un jury de SEMESTRE ne décide de rien.** Il arrête un bilan intermédiaire
    et verrouille les notes du semestre, rien de plus : ni statut d'inscription
    modifié, ni redoublement compté. C'est la règle déjà posée au calcul, qui
    ne lui propose aucune décision, et à `preparer_passage`, qui refuse le
    passage hors jury annuel.

    Elle ne l'était pas ici : la validation exigeait une décision par étudiant,
    et les reportait comme celles d'un jury de fin d'année. Un bilan de mi-année
    pouvait donc rendre vingt-cinq étudiants « redoublants » et incrémenter leur
    compteur — la seule façon d'y échapper étant de ne jamais valider.
    """
    if deliberation.est_verrouillee:
        raise ValidationError('Délibération déjà validée.')
    if deliberation.statut != Deliberation.STATUT_CALCULEE:
        raise ValidationError('Calculer la délibération avant de la valider.')

    lignes = deliberation.lignes.select_related('inscription').all()

    if deliberation.portee == Deliberation.PORTEE_SEMESTRE:
        # Seul effet : figer les notes du semestre. Le bilan devient consultable
        # sans risque qu'une note bouge sous lui.
        notes_couvertes(deliberation).update(verrouillee=True)
        deliberation.statut          = Deliberation.STATUT_VALIDEE
        deliberation.validee_par     = (utilisateur if getattr(
            utilisateur, 'is_authenticated', False) else None)
        deliberation.date_validation = timezone.now()
        deliberation.save(update_fields=['statut', 'validee_par', 'date_validation'])
        return deliberation

    sans_decision = [l for l in lignes if not l.decision]
    if sans_decision:
        raise ValidationError(
            f'{len(sans_decision)} étudiant(s) sans décision — compléter avant validation.'
        )
    ajustees_sans_motif = [
        l for l in lignes if l.est_ajustee and not l.motif_ajustement.strip()
    ]
    if ajustees_sans_motif:
        raise ValidationError(
            f'{len(ajustees_sans_motif)} décision(s) modifiée(s) sans motif — motif obligatoire.'
        )

    statut_par_decision = {
        LigneDeliberation.DECISION_ADMIS:      InscriptionIPGEI.STATUT_ADMIS,
        LigneDeliberation.DECISION_REORIENTE:  InscriptionIPGEI.STATUT_REORIENTE,
        LigneDeliberation.DECISION_CNIM:       InscriptionIPGEI.STATUT_CNIM,
        LigneDeliberation.DECISION_REDOUBLANT: InscriptionIPGEI.STATUT_REDOUBLANT,
        LigneDeliberation.DECISION_RATTRAPAGE: InscriptionIPGEI.STATUT_RATTRAPAGE,
    }

    for ligne in lignes:
        inscription = ligne.inscription
        # Photo de l'état d'avant, prise ici et nulle part ailleurs : c'est le
        # seul instant où il est encore connu, et ce que la dévalidation devra
        # restaurer.
        ligne.statut_avant           = inscription.statut
        ligne.nb_redoublements_avant = inscription.nb_redoublements
        ligne.save(update_fields=['statut_avant', 'nb_redoublements_avant'])

        inscription.statut = statut_par_decision.get(ligne.decision, inscription.statut)
        champs = ['statut']
        if ligne.decision == LigneDeliberation.DECISION_REDOUBLANT:
            inscription.nb_redoublements += 1
            champs.append('nb_redoublements')
        inscription.save(update_fields=champs)

    notes_couvertes(deliberation).update(verrouillee=True)

    deliberation.statut          = Deliberation.STATUT_VALIDEE
    deliberation.validee_par     = utilisateur if getattr(utilisateur, 'is_authenticated', False) else None
    deliberation.date_validation = timezone.now()
    deliberation.save(update_fields=['statut', 'validee_par', 'date_validation'])
    return deliberation


def decisions_emises(deliberation: Deliberation) -> int:
    """
    Nombre de décisions de jury déjà éditées pour cette délibération.

    Sert d'avertissement avant dévalidation : ces documents portent un numéro de
    série et sont vérifiables en ligne — ils sont dans la nature. Le compte est
    approché (le registre ne pointe pas la délibération, seulement l'étudiant et
    l'année), et c'est assumé : mieux vaut un ordre de grandeur exact au dossier
    près qu'aucun signal.
    """
    from apps.documents.models import DocumentOfficiel

    from .documents import TYPE_DECISION

    return DocumentOfficiel.objects.filter(
        type_document=TYPE_DECISION,
        annee_universitaire=deliberation.annee_universitaire,
        etudiant__in=deliberation.lignes.values_list('inscription__etudiant_id', flat=True),
    ).count()


@transaction.atomic
def devalider_deliberation(deliberation: Deliberation, utilisateur=None) -> Deliberation:
    """
    Défait une validation : restaure les inscriptions, déverrouille les notes.

    Trois effets à annuler, et un piège dans chacun.

    1. Le statut de l'inscription revient à ce que la ligne a mémorisé. Une
       délibération validée avant l'existence de cette mémoire ne peut pas être
       défaite : deviner l'état d'avant, ce serait effacer un redoublement
       antérieur ou en inventer un.
    2. Le compteur de redoublement reprend sa valeur d'avant, jamais une simple
       décrémentation — deux dévalidations successives le feraient dériver.
    3. Les notes ne se déverrouillent que si aucune AUTRE délibération validée ne
       les couvre : dévalider le jury annuel ne doit pas rouvrir des notes que le
       jury du semestre a déjà arrêtées.

    La délibération repasse en « calculée » : ses lignes et ses moyennes restent,
    seule la portée juridique est retirée.
    """
    if deliberation.statut != Deliberation.STATUT_VALIDEE:
        raise ValidationError('Cette délibération n\'est pas validée.')

    lignes = list(deliberation.lignes.select_related('inscription').all())
    inscriptions_visees = {l.inscription_id for l in lignes}

    # Chaque validation photographie l'état du moment. Deux jurys qui se suivent
    # sur les mêmes inscriptions empilent donc deux photos ; les défaire dans le
    # désordre restaurerait un état antérieur au jury encore validé, et le
    # compteur de redoublement s'en trouverait faussé. On défait dans l'ordre
    # inverse de ce qui a été fait, ou pas du tout.
    posterieures = [
        autre for autre in Deliberation.objects
        .filter(statut=Deliberation.STATUT_VALIDEE,
                date_validation__gt=deliberation.date_validation)
        .exclude(pk=deliberation.pk)
        if inscriptions_visees & set(
            autre.lignes.values_list('inscription_id', flat=True))
    ]
    if posterieures:
        noms = ', '.join(str(autre) for autre in posterieures)
        raise ValidationError(
            f'Ces délibérations ont été validées après celle-ci et portent sur '
            f'les mêmes étudiants : {noms}. Dévalidez-les d\'abord — les défaire '
            'dans le désordre fausserait les compteurs de redoublement.'
        )

    sans_memoire = [l for l in lignes if not l.statut_avant]
    if sans_memoire:
        raise ValidationError(
            f"{len(sans_memoire)} ligne(s) ne portent pas l'état antérieur de "
            "l'inscription : cette délibération a été validée avant que cet état "
            'soit mémorisé, sa dévalidation automatique ferait plus de dégâts '
            "qu'elle n'en répare."
        )

    for ligne in lignes:
        inscription = ligne.inscription
        inscription.statut = ligne.statut_avant
        if ligne.nb_redoublements_avant is not None:
            inscription.nb_redoublements = ligne.nb_redoublements_avant
        inscription.save(update_fields=['statut', 'nb_redoublements'])

    autres = (Deliberation.objects
              .filter(statut=Deliberation.STATUT_VALIDEE)
              .exclude(pk=deliberation.pk))
    encore_verrouillees = set()
    for autre in autres:
        encore_verrouillees.update(
            notes_couvertes(autre).values_list('id', flat=True)
        )
    (notes_couvertes(deliberation)
     .exclude(id__in=encore_verrouillees)
     .update(verrouillee=False))

    deliberation.statut          = Deliberation.STATUT_CALCULEE
    deliberation.validee_par     = None
    deliberation.date_validation = None
    deliberation.save(update_fields=['statut', 'validee_par', 'date_validation'])

    try:
        from core.audit_helpers import write_audit
        write_audit(
            action='UPDATE', model_name='Deliberation', object_id=str(deliberation.pk),
            changes={'statut': {'old': Deliberation.STATUT_VALIDEE,
                                'new': Deliberation.STATUT_CALCULEE},
                     'lignes_restaurees': len(lignes),
                     'par': getattr(utilisateur, 'username', None)},
            label=f'Dévalidation — {deliberation}', keep_forever=True,
        )
    except Exception:                                       # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            'Audit dévalidation délibération %s échoué', deliberation.pk, exc_info=True,
        )
    return deliberation


# ═════════════════════════════════════════════════════════════════════════════
# Périmètre d'un enseignant : ce qu'il enseigne, donc ce qu'il note
# ═════════════════════════════════════════════════════════════════════════════
def prof_de(utilisateur):
    """Fiche enseignant rattachée à un compte, ou None."""
    return getattr(utilisateur, 'prof_profile', None)


def feuilles_enseignant(prof, *, annee_universitaire: str = '', semestre: SemestreIPGEI = None):
    """
    Couples (classe, matière) qu'un enseignant a effectivement en charge.

    Le périmètre se lit dans l'emploi du temps et non dans une table
    d'affectation : IPGEI n'en a pas, et la planification est de toute façon
    l'endroit où l'information est tenue à jour. Les séances annulées comptent
    — elles disent qui a la matière, pas qui a fait cours ce jour-là.

    Renvoie une liste de dictionnaires triés, prête pour l'écran « mes feuilles ».
    """
    seances = (
        SeanceReelle.objects
        .filter(prof=prof)
        .select_related('classe', 'matiere', 'semaine__semestre')
    )
    if semestre is not None:
        seances = seances.filter(semaine__semestre=semestre)
    elif annee_universitaire:
        seances = seances.filter(semaine__semestre__annee_universitaire=annee_universitaire)

    feuilles = {}
    for seance in seances:
        semestre_seance = seance.semaine.semestre
        cle = (seance.classe_id, seance.matiere_id, semestre_seance.id)
        if cle in feuilles:
            feuilles[cle]['seances'] += 1
            continue
        feuilles[cle] = {
            'classe':           seance.classe_id,
            'classe_nom':       seance.classe.nom,
            'matiere':          seance.matiere_id,
            'matiere_code':     seance.matiere.code,
            'matiere_intitule': seance.matiere.intitule,
            'semestre':         semestre_seance.id,
            'semestre_code':    semestre_seance.code,
            'annee_universitaire': semestre_seance.annee_universitaire,
            'seances':          1,
        }
    return sorted(
        feuilles.values(),
        key=lambda f: (f['annee_universitaire'], f['semestre_code'],
                       f['classe_nom'], f['matiere_code']),
    )


def enseigne(prof, *, classe_id, matiere_id, semestre: SemestreIPGEI) -> bool:
    """Vrai si cet enseignant a la matière dans cette classe, ce semestre."""
    if prof is None:
        return False
    return SeanceReelle.objects.filter(
        prof=prof, classe_id=classe_id, matiere_id=matiere_id,
        semaine__semestre=semestre,
    ).exists()


def enseigne_matiere(prof, *, matiere_id, semestre: SemestreIPGEI) -> bool:
    """
    Vrai si l'enseignant a cette matière ce semestre, quelle que soit la classe.

    C'est le contrôle qui convient à la correction anonyme : le numéro de copie
    ne dit pas de quelle classe elle vient, et c'est précisément le but.
    """
    if prof is None:
        return False
    return SeanceReelle.objects.filter(
        prof=prof, matiere_id=matiere_id, semaine__semestre=semestre,
    ).exists()


# ═════════════════════════════════════════════════════════════════════════════
# Anonymat des copies
# ═════════════════════════════════════════════════════════════════════════════
def inscrits_du_semestre(semestre: SemestreIPGEI):
    """
    Inscriptions actives couvertes par un semestre — celles de son année d'étude.

    Le rattachement se fait sur le RANG. Comparer au niveau déduit du code
    laissait dehors les inscrits d'un second niveau du même rang — un MPI de
    première année n'aurait reçu aucun numéro d'anonymat, et rien ne l'aurait
    signalé avant l'ouverture des copies.
    """
    rang = rang_du_semestre(semestre.code)
    return [
        inscription for inscription in (
            InscriptionIPGEI.objects
            .filter(actif=True,
                    annee_universitaire=semestre.annee_universitaire,
                    classe__annee_universitaire=semestre.annee_universitaire)
            .select_related('classe', 'etudiant')
        )
        if rang_du_niveau(inscription.classe.niveau_code, semestre.institution) == rang
    ]


@transaction.atomic
def generer_anonymats(session: SessionEvaluationIPGEI, *, regenerer: bool = False,
                      force: bool = False, utilisateur=None) -> int:
    """
    Attribue à chaque inscrit un numéro tiré au sort dans 1..N.

    Les numéros sont mélangés : distribués dans l'ordre alphabétique, ils
    laisseraient deviner l'identité d'une copie à partir de son rang.

    Régénérer après le début de la saisie réassocie numéros et étudiants, donc
    attribue les notes déjà saisies à d'autres personnes. On le refuse dès
    qu'une note existe sur le semestre, sauf décision explicite (`force`), que
    la vue réserve à un administrateur.
    """
    import random

    # L'année et le semestre priment sur la campagne : ce qui est clos ne se
    # corrige plus, et tirer des numéros y laisserait croire le contraire.
    if annee_cloturee(session.semestre.annee_universitaire):
        raise ValidationError(
            f'Année {session.semestre.annee_universitaire} clôturée — '
            'aucun tirage possible.')
    if session.semestre.est_cloture:
        raise ValidationError(
            f'Semestre {session.semestre.code} clôturé — aucun tirage possible.')
    if session.est_close:
        raise ValidationError('Campagne clôturée — les anonymats ne se régénèrent plus.')

    existants = AnonymatIPGEI.objects.filter(session=session)
    if existants.exists():
        if not regenerer:
            raise ValidationError(
                'Des numéros existent déjà pour cette campagne. Demandez une '
                'régénération pour les remplacer.'
            )
        deja_note = Note.objects.filter(
            semestre=session.semestre, evaluations__isnull=False,
        ).exists()
        if deja_note and not force:
            raise ValidationError(
                'Des notes sont déjà saisies sur ce semestre : régénérer les '
                'numéros les attribuerait à d\'autres étudiants. Un '
                'administrateur peut passer outre en connaissance de cause.'
            )
        existants.delete()

    inscriptions = inscrits_du_semestre(session.semestre)
    if not inscriptions:
        # Un « succès » qui ne produit rien se lit comme une panne : l'agent
        # clique, l'écran annonce 0 numéro, et rien ne dit que la faute est
        # à l'absence d'inscrits. On le REFUSE en le disant, et l'on nomme
        # l'écran où inscrire.
        raise ValidationError(
            f'Aucun inscrit actif en {session.semestre.annee_universitaire} '
            f'pour les classes de {session.semestre.code} : aucune copie '
            'à numéroter. Inscrivez les étudiants depuis '
            'Inscriptions → Nouvelle inscription, puis relancez le tirage.'
        )

    numeros = list(range(1, len(inscriptions) + 1))
    random.shuffle(numeros)
    AnonymatIPGEI.objects.bulk_create([
        AnonymatIPGEI(session=session, inscription=inscription, numero=numero,
                      genere_par=utilisateur if getattr(utilisateur, 'is_authenticated', False) else None)
        for inscription, numero in zip(inscriptions, numeros)
    ])
    return len(inscriptions)


def resoudre_anonymat(session: SessionEvaluationIPGEI, numero) -> InscriptionIPGEI:
    """Retrouve l'étudiant derrière un numéro, pour reporter une note saisie."""
    try:
        numero = int(numero)
    except (TypeError, ValueError):
        raise ValidationError({'numero_anonymat': "Numéro d'anonymat invalide."})

    anonymat = (AnonymatIPGEI.objects
                .select_related('inscription__etudiant')
                .filter(session=session, numero=numero).first())
    if anonymat is None:
        raise ValidationError(
            {'numero_anonymat': f'Aucun étudiant sous le numéro {numero} '
                                f'pour {session.semestre.code}.'}
        )
    return anonymat.inscription


# ═════════════════════════════════════════════════════════════════════════════
# Jury : composition et signatures du PV
# ═════════════════════════════════════════════════════════════════════════════
def ajouter_membre_jury(deliberation: Deliberation, utilisateur, role: str) -> MembreJuryIPGEI:
    """
    Inscrit une personne au jury d'une délibération.

    Un seul président : c'est lui qui arrête la séance, deux rendraient le PV
    incohérent. Les autres rôles sont ouverts, un jury comptant plusieurs
    enseignants.
    """
    if role not in dict(MembreJuryIPGEI.ROLE_CHOICES):
        raise ValidationError({'role': 'Rôle inconnu.'})
    if MembreJuryIPGEI.objects.filter(
        deliberation=deliberation, utilisateur=utilisateur,
    ).exists():
        raise ValidationError('Cette personne siège déjà dans ce jury.')
    if role == MembreJuryIPGEI.ROLE_PRESIDENT and MembreJuryIPGEI.objects.filter(
        deliberation=deliberation, role=MembreJuryIPGEI.ROLE_PRESIDENT,
    ).exists():
        raise ValidationError({'role': 'Le jury a déjà un président.'})

    return MembreJuryIPGEI.objects.create(
        deliberation=deliberation, utilisateur=utilisateur, role=role,
    )


def retirer_membre_jury(membre: MembreJuryIPGEI) -> None:
    """Retire un membre — sauf s'il a signé : une signature ne s'efface pas."""
    if membre.a_signe:
        raise ValidationError(
            'Ce membre a signé le procès-verbal ; son retrait effacerait sa signature.'
        )
    membre.delete()


def signer_pv(deliberation: Deliberation, utilisateur) -> MembreJuryIPGEI:
    """
    Enregistre la signature du membre correspondant à l'utilisateur courant.

    On ne signe ni pour autrui, ni avant que les décisions soient arrêtées :
    signer un projet reviendrait à approuver ce qui peut encore changer.
    """
    if deliberation.statut != Deliberation.STATUT_VALIDEE:
        raise ValidationError(
            'Le procès-verbal ne se signe qu\'une fois la délibération validée.'
        )
    membre = MembreJuryIPGEI.objects.filter(
        deliberation=deliberation, utilisateur=utilisateur,
    ).first()
    if membre is None:
        raise ValidationError('Vous ne siégez pas dans ce jury.')
    if membre.a_signe:
        raise ValidationError('Vous avez déjà signé ce procès-verbal.')

    membre.signature_le = timezone.now()
    membre.save(update_fields=['signature_le'])
    return membre


def membres_jury_ordonnes(deliberation: Deliberation):
    """Membres du jury dans l'ordre protocolaire, pour l'impression du PV."""
    membres = deliberation.membres_jury.select_related('utilisateur').all()
    return sorted(
        membres,
        key=lambda m: (MembreJuryIPGEI.ORDRE_ROLES.get(m.role, 9),
                       (m.utilisateur.name or m.utilisateur.username or '').lower()),
    )


# ═════════════════════════════════════════════════════════════════════════════
# Calendrier : génération des semaines
# ═════════════════════════════════════════════════════════════════════════════
# Qualifications du calendrier du socle → celles d'IPGEI. « Férié » n'a pas
# d'équivalent : une semaine chômée est une semaine sans cours, donc des
# vacances au sens du calendrier pédagogique.
_TYPE_SEMAINE_SOCLE = {
    'cours':    SemaineIPGEI.TYPE_COURS,
    'examen':   SemaineIPGEI.TYPE_EXAMEN,
    'vacances': SemaineIPGEI.TYPE_VACANCES,
    'ferie':    SemaineIPGEI.TYPE_VACANCES,
}


@transaction.atomic
def synchroniser_calendrier(semestre: SemestreIPGEI, *, remplacer=False) -> dict:
    """
    Aligne le calendrier du semestre sur celui du socle : crée, corrige, retire.

    Trois mouvements, parce que le calendrier vit : on le génère d'un coup, on
    y ajoute une semaine en cours d'année, et l'on en retire une posée par
    erreur. Ne traiter que le premier laissait le reflet dériver dès la
    première correction.

    **Il n'y a qu'un seul calendrier**, celui de « Paramètres → Semaines »
    (`parametres.Semaine`), et c'est lui qui fait foi. IPGEI n'en tient pas un
    second : il en reflète les semaines pour pouvoir y rattacher ses séances.

    Auparavant, chaque côté générait les siennes depuis les dates du semestre,
    et les deux divergeaient sans que rien ne le signale — des semaines
    présentes à l'écran du socle restaient introuvables à la duplication.

    Le socle stocke une ligne par (semaine × jour) ; on en tire ici une ligne
    par semaine, bornée par ses dates extrêmes. La numérotation, le type
    (cours, vacances, examens) et les dates viennent de lui : ajouter des
    semaines là-bas suffit, il n'y a qu'à relancer.

    `remplacer=True` repart de zéro, en ne supprimant que les semaines sans
    séance — un emploi du temps déjà posé n'est jamais détruit.

    On reprend TOUT ce que le socle porte, et non un nombre choisi : l'écran
    offrait un compteur « semaines à reprendre au maximum » que ce service
    n'a jamais lu. Reprendre la moitié d'un calendrier qui fait foi ne veut
    rien dire — le compteur a été retiré plutôt qu'implémenté.
    """
    from django.db.models import Max, Min

    from apps.parametres.models import Semaine as SemaineSocle

    if remplacer:
        semestre.semaines.filter(seances__isnull=True).delete()

    groupes = (
        SemaineSocle.objects
        .filter(annee_universitaire=semestre.annee_universitaire,
                type_semestre=semestre.type_semestre,
                numero_semaine__isnull=False)
        .values('numero_semaine', 'type_semaine')
        .annotate(debut=Min('date'), fin=Max('date'))
        .order_by('numero_semaine')
    )

    au_socle = {g['numero_semaine']: g for g in groupes}
    existantes = {s.numero: s for s in semestre.semaines.all()}

    # ── Ce que le socle porte et que le semestre n'a pas encore ─────────────
    creees = [
        SemaineIPGEI(
            semestre=semestre,
            numero=numero,
            date_debut=g['debut'],
            date_fin=g['fin'],
            # Le socle connaît les mêmes qualifications, à « férié » près, qui
            # n'existe pas côté IPGEI : on le range en vacances, faute de quoi
            # un jour chômé se retrouverait compté comme semaine de cours.
            type_semaine=_TYPE_SEMAINE_SOCLE.get(
                g['type_semaine'], SemaineIPGEI.TYPE_COURS),
        )
        for numero, g in au_socle.items() if numero not in existantes
    ]
    SemaineIPGEI.objects.bulk_create(creees)

    # ── Ce qui a changé de dates ou de qualification ────────────────────────
    # Une semaine décalée d'un jour au socle doit l'être ici aussi : sans cela
    # les deux calendriers repartent en silence, ce que ce reflet existe
    # précisément pour empêcher.
    modifiees = []
    for numero, semaine in existantes.items():
        g = au_socle.get(numero)
        if g is None:
            continue
        type_attendu = _TYPE_SEMAINE_SOCLE.get(
            g['type_semaine'], SemaineIPGEI.TYPE_COURS)
        if (semaine.date_debut, semaine.date_fin, semaine.type_semaine) ==            (g['debut'], g['fin'], type_attendu):
            continue
        semaine.date_debut, semaine.date_fin = g['debut'], g['fin']
        semaine.type_semaine = type_attendu
        modifiees.append(semaine)
    if modifiees:
        SemaineIPGEI.objects.bulk_update(
            modifiees, ['date_debut', 'date_fin', 'type_semaine'])

    # ── Ce que le socle ne porte plus ───────────────────────────────────────
    # Une semaine ajoutée par erreur puis retirée du calendrier doit
    # disparaître ici aussi, SAUF si des séances s'y sont accrochées : on ne
    # fait pas disparaître un emploi du temps par ricochet. La semaine reste
    # alors, et le bilan la signale — à l'agent de vider la semaine d'abord.
    orphelines = [s for numero, s in existantes.items() if numero not in au_socle]
    a_retirer  = [s.pk for s in orphelines
                  if not s.seances.exists() and not s.archives.exists()]
    retenues   = [s.numero for s in orphelines if s.pk not in a_retirer]
    supprimees = 0
    if a_retirer:
        supprimees = SemaineIPGEI.objects.filter(pk__in=a_retirer).delete()[0]

    aligner_debut(semestre)
    return {
        'creees':     len(creees),
        'modifiees':  len(modifiees),
        'supprimees': supprimees,
        'retenues':   retenues,
        'total':      semestre.semaines.count(),
    }


def generer_semaines(semestre: SemestreIPGEI, *, remplacer=False) -> int:
    """
    Nombre de semaines CRÉÉES par la synchronisation — vue simplifiée.

    Conservée pour les appelants qui ne veulent qu'un compte : la commande
    d'initialisation et la synchronisation du miroir.
    """
    return synchroniser_calendrier(semestre, remplacer=remplacer)['creees']


def aligner_debut(semestre: SemestreIPGEI) -> bool:
    """
    Recale la date de debut du semestre sur sa premiere semaine.

    Elle se DEDUIT du calendrier, elle ne le decide pas : le champ s'annonce
    lui-meme comme « la date de la premiere semaine », mais restait saisi a la
    main et pouvait donc dementir ce qu'il decrivait. S3 2026-2027 affichait le
    15/09 quand ses semaines commencaient le 05/10 — trois semaines d'ecart
    entre la date lue a l'ecran et le jour ou les cours commencent.

    Consequence a connaitre : S1 et S3 porteront la MEME date, puisque la
    reprise puise dans les memes semaines du socle — un seul calendrier pour
    les deux promotions. C'est la realite du calendrier, rendue visible.

    La date de FIN n'est jamais ecrite. On ne sait pas quand un semestre
    s'acheve : apres seize semaines on peut en ajouter d'autres, et une fin
    inscrite d'avance serait dementie par la premiere semaine de rattrapage.
    L'ecran constate la derniere semaine existante ; il n'annonce pas de terme.

    Sans semaine reprise, rien ne bouge : la date de convention tient lieu de
    repere, un semestre sans calendrier devant tout de meme se situer.
    """
    premiere = semestre.semaines.order_by('date_debut').values_list(
        'date_debut', flat=True).first()
    if premiere is None or premiere == semestre.date_debut:
        return False
    semestre.date_debut = premiere
    semestre.save(update_fields=['date_debut'])
    return True


# ═════════════════════════════════════════════════════════════════════════════
# EDT : duplication de la grille type
# ═════════════════════════════════════════════════════════════════════════════
# Index Django du jour (0 = lundi) par libellé, pour dater les séances.
_INDEX_JOUR = {
    'lundi': 0, 'mardi': 1, 'mercredi': 2, 'jeudi': 3,
    'vendredi': 4, 'samedi': 5, 'dimanche': 6,
}


def _date_seance(semaine: SemaineIPGEI, jour) -> object:
    """Date réelle d'une séance = lundi de la semaine + décalage du jour."""
    libelle = (getattr(jour, 'jour', '') or '').strip().lower()
    decalage = _INDEX_JOUR.get(libelle)
    if decalage is None:
        return None
    return semaine.date_debut + timedelta(days=decalage)


@transaction.atomic
def dupliquer_grille(grille: GrilleType, semaines, *, ecraser=False) -> dict:
    """
    Duplique la grille type sur les semaines demandées.

    Par défaut, une case déjà occupée est **laissée telle quelle** : la
    duplication n'écrase jamais une édition manuelle ou une permutation déjà
    appliquée. `ecraser=True` force le retour au patron pour les seules séances
    d'origine « grille ».
    """
    modeles = list(
        grille.seances.select_related('jour', 'creneau', 'matiere', 'prof', 'salle', 'sous_groupe')
    )
    creees = ignorees = remplacees = 0

    for semaine in semaines:
        if semaine.type_semaine != SemaineIPGEI.TYPE_COURS:
            continue
        for modele in modeles:
            filtre = {
                'classe':      grille.classe,
                'semaine':     semaine,
                'jour':        modele.jour,
                'creneau':     modele.creneau,
                'sous_groupe': modele.sous_groupe,
            }
            existante = SeanceReelle.objects.filter(**filtre).first()
            if existante is not None:
                if not ecraser or existante.origine != SeanceReelle.ORIGINE_GRILLE:
                    ignorees += 1
                    continue
                existante.delete()
                remplacees += 1
            SeanceReelle.objects.create(
                **filtre,
                matiere=modele.matiere,
                prof=modele.prof,
                salle=modele.salle,
                type_seance=modele.type_seance,
                date=_date_seance(semaine, modele.jour),
                origine=SeanceReelle.ORIGINE_GRILLE,
                seance_type=modele,
            )
            creees += 1

    return {'creees': creees, 'ignorees': ignorees, 'remplacees': remplacees}


def semaines_du_lot(semaine_depart: SemaineIPGEI, nb_semaines: int):
    """
    Les `nb_semaines` semaines de cours consécutives à partir d'une semaine
    donnée — support commun de l'édition « en lot » et des permutations de
    portée N (plan §4.5 et §4.6).
    """
    return list(
        SemaineIPGEI.objects
        .filter(
            semestre=semaine_depart.semestre,
            date_debut__gte=semaine_depart.date_debut,
            type_semaine=SemaineIPGEI.TYPE_COURS,
        )
        .order_by('date_debut')[:max(1, nb_semaines)]
    )


# ═════════════════════════════════════════════════════════════════════════════
# Permutations
# ═════════════════════════════════════════════════════════════════════════════
def _seance_equivalente(reference: SeanceReelle, semaine: SemaineIPGEI):
    """Même case (classe, jour, créneau, sous-groupe) sur une autre semaine."""
    return SeanceReelle.objects.filter(
        classe=reference.classe, semaine=semaine, jour=reference.jour,
        creneau=reference.creneau, sous_groupe=reference.sous_groupe,
    ).first()


@transaction.atomic
def appliquer_permutation_prof(permutation: PermutationProf, utilisateur=None) -> int:
    """
    Applique l'échange (prof, salle, matière) entre les deux séances, sur la
    portée demandée. Le créneau ne bouge pas — c'est la contrainte du cadrage.

    `prof_initial` conserve qui devait assurer la séance, mais la charge et le
    pointage se basent sur `prof`, donc sur le prof effectif de la semaine.
    """
    if permutation.statut == StatutWorkflow.APPLIQUEE:
        raise ValidationError('Permutation déjà appliquée.')
    if permutation.statut != StatutWorkflow.VALIDEE:
        raise ValidationError('La permutation doit être validée par le directeur avant application.')

    a, b = permutation.seance_a, permutation.seance_b
    semaines_a = semaines_du_lot(a.semaine, permutation.nb_semaines)
    impactees = 0

    for semaine in semaines_a:
        sa = _seance_equivalente(a, semaine)
        sb = _seance_equivalente(b, semaine)
        if sa is None or sb is None:
            # Semaine où l'une des deux séances n'existe pas (férié, séance
            # supprimée) : on saute plutôt que de créer un échange bancal.
            continue

        sa_prof, sa_salle, sa_matiere = sa.prof_id, sa.salle_id, sa.matiere_id
        sa.prof_initial_id = sa.prof_initial_id or sa.prof_id
        sb.prof_initial_id = sb.prof_initial_id or sb.prof_id

        sa.prof_id, sa.salle_id, sa.matiere_id = sb.prof_id, sb.salle_id, sb.matiere_id
        sb.prof_id, sb.salle_id, sb.matiere_id = sa_prof, sa_salle, sa_matiere
        sa.origine = sb.origine = SeanceReelle.ORIGINE_PERMUTATION

        champs = ['prof', 'salle', 'matiere', 'origine', 'prof_initial']
        sa.save(update_fields=champs)
        sb.save(update_fields=champs)
        impactees += 2

    permutation.statut            = StatutWorkflow.APPLIQUEE
    permutation.date_application  = timezone.now()
    permutation.seances_impactees = impactees
    if utilisateur is not None and getattr(utilisateur, 'is_authenticated', False):
        permutation.valide_par = permutation.valide_par or utilisateur
    permutation.save(update_fields=[
        'statut', 'date_application', 'seances_impactees', 'valide_par',
    ])
    return impactees


@transaction.atomic
def _deplacer(inscription: InscriptionIPGEI, classe_cible, sous_groupe_cible,
              motif: str, utilisateur=None) -> None:
    """
    Rattache une inscription à une autre classe, en journalisant le mouvement.

    Rien n'est copié ni recréé : notes, absences et lignes de délibération
    pointent sur l'INSCRIPTION, dont l'identité ne change pas. L'historique
    reste donc intact et suit l'étudiant dans sa nouvelle classe.
    """
    HistoriqueClasse.objects.create(
        inscription=inscription,
        classe_avant=inscription.classe,
        classe_apres=classe_cible,
        motif=motif,
        decide_par=utilisateur if getattr(utilisateur, 'is_authenticated', False) else None,
    )
    inscription.classe = classe_cible
    # Le sous-groupe appartient à l'ancienne classe : on le remplace par celui
    # choisi dans la cible, ou on le vide pour éviter un rattachement incohérent.
    inscription.sous_groupe = sous_groupe_cible
    # `save()` complet et non `update_fields` : c'est lui qui fait suivre le
    # département de l'étudiant, dont dépend la liste d'appel.
    inscription.save()


@transaction.atomic
def appliquer_permutation_etudiant(permutation: PermutationEtudiant, utilisateur=None) -> InscriptionIPGEI:
    """
    Applique le mouvement : échange réciproque, ou transfert simple.

    Les deux déplacements d'un échange se font dans la MÊME transaction. Traités
    séparément, un échec sur le second laisserait une classe en surnombre et
    l'autre incomplète, sans que rien ne relie les deux moitiés.
    """
    if permutation.statut == StatutWorkflow.APPLIQUEE:
        raise ValidationError('Permutation déjà appliquée.')
    if permutation.statut != StatutWorkflow.VALIDEE:
        raise ValidationError('La permutation doit être validée par le directeur avant application.')

    inscription = permutation.inscription
    classe_a    = inscription.classe

    if permutation.inscription_b_id:
        contrepartie = permutation.inscription_b
        # Les classes se lisent AVANT tout déplacement : après le premier, la
        # seconde inscription pointerait déjà sur une classe qui a changé.
        classe_b = contrepartie.classe
        _deplacer(inscription, classe_b, permutation.sous_groupe_cible,
                  permutation.motif, utilisateur)
        _deplacer(contrepartie, classe_a, permutation.sous_groupe_cible_b,
                  permutation.motif, utilisateur)
    else:
        _deplacer(inscription, permutation.classe_cible, permutation.sous_groupe_cible,
                  permutation.motif, utilisateur)

    permutation.statut           = StatutWorkflow.APPLIQUEE
    permutation.date_application = timezone.now()
    permutation.save(update_fields=['statut', 'date_application'])
    return inscription


# ═════════════════════════════════════════════════════════════════════════════
# Absences par exception
# ═════════════════════════════════════════════════════════════════════════════
@transaction.atomic
def saisir_absences(seance: SeanceReelle, saisies: list, utilisateur=None) -> dict:
    """
    Enregistre la feuille d'une séance en mode « par exception ».

    `saisies` = [{'inscription': id, 'statut': 'absent'|'retard'|'justifiee',
                  'justificatif': str}]. Toute inscription absente de la liste
    est considérée **présente** : sa ligne éventuelle est supprimée, ce qui
    permet de corriger une saisie sans laisser de trace fantôme.
    """
    inscriptions_eligibles = set(
        InscriptionIPGEI.objects
        .filter(classe=seance.classe, actif=True)
        .values_list('id', flat=True)
    )

    vus = set()
    for saisie in saisies:
        inscription_id = int(saisie['inscription'])
        if inscription_id not in inscriptions_eligibles:
            raise ValidationError(
                f"L'inscription {inscription_id} n'appartient pas à la classe de cette séance."
            )
        AbsenceSeance.objects.update_or_create(
            seance=seance, inscription_id=inscription_id,
            defaults={
                'statut':       saisie.get('statut', AbsenceSeance.STATUT_ABSENT),
                'justificatif': saisie.get('justificatif', ''),
                'saisie_par':   utilisateur if getattr(utilisateur, 'is_authenticated', False) else None,
            },
        )
        vus.add(inscription_id)

    supprimees, _ = (
        AbsenceSeance.objects.filter(seance=seance).exclude(inscription_id__in=vus).delete()
    )

    # Le registre est celui du socle (cf. `bilan_absences`) : ce qui est relevé
    # ici doit y parvenir, sans quoi les écrans ABS, les rapports et les fiches
    # ignoreraient l'épreuve. Si le suivi de la semaine n'existe pas encore,
    # rien n'est écrit — le report se fera à sa génération, qui appelle la même
    # fonction. Un échec ne fait pas échouer la saisie : la feuille d'appel doit
    # rester enregistrable même si le reflet, lui, achoppe.
    semaine = seance.semaine
    try:
        from .miroir import refleter_absences
        refleter_absences(semaine.semestre.annee_universitaire,
                          semaine.semestre.type_semestre, semaine.numero)
    except Exception:                                          # noqa: BLE001
        logger.exception('Report au registre du socle impossible (séance %s)',
                         seance.pk)

    return {'enregistrees': len(vus), 'reinitialisees': supprimees}


#: Statuts du socle (`absence.Presence`). Les valeurs sont numériques là-bas ;
#: on les nomme ici plutôt que d'écrire `statut=1` dans les requêtes.
PRESENCE_ABSENT     = 1
PRESENCE_SANCTIONNE = 2
PRESENCE_JUSTIFIEE  = 3


def bilan_absences(inscription: InscriptionIPGEI, semestre: SemestreIPGEI = None) -> dict:
    """
    Compte des absences d'un étudiant, éventuellement bornées à un semestre.

    Lit `absence.Presence`, le registre du socle : l'IPGEI y saisit ses
    absences depuis le module « Absences », qui offre la saisie par séance, le
    mode salle, les rapports et l'arbitrage des justificatifs. En tenir un
    second dans `ipgei` obligeait à tout réécrire, et deux registres finissent
    par diverger.

    Le rattachement passe par le SUIVI, lui-même daté par sa semaine et son
    année : c'est ce qui permet de borner à un semestre.
    """
    from apps.absence.models import Presence

    qs = Presence.objects.filter(etudiant_id=inscription.etudiant_id)
    if semestre is not None:
        # Les semaines du semestre, telles que le suivi les numérote.
        numeros = list(
            semestre.semaines.filter(numero__isnull=False)
            .values_list('numero', flat=True)
        )
        qs = qs.filter(
            suivi__annee_universitaire=semestre.annee_universitaire,
            suivi__numero_semaine__in=numeros or [0],
        )

    absences   = qs.filter(statut=PRESENCE_ABSENT).count()
    justifiees = qs.filter(statut=PRESENCE_JUSTIFIEE).count()
    sanctions  = qs.filter(statut=PRESENCE_SANCTIONNE).count()
    return {
        'absences':   absences,
        # Le socle n'a pas de « retard » : il a « sanctionné ». On expose la
        # clé attendue par les gabarits plutôt que d'en changer partout.
        'retards':    sanctions,
        'justifiees': justifiees,
        'total':      absences + justifiees + sanctions,
    }


# ═════════════════════════════════════════════════════════════════════════════
# Relevés (support des documents officiels)
# ═════════════════════════════════════════════════════════════════════════════
#: Intitulés de repli, pour une base antérieure au référentiel de niveaux.
#: Ne servent qu'à défaut : l'intitulé se saisit dans « Niveaux du cursus »,
#: sans quoi un niveau ajouté — MPI — n'aurait jamais le sien.
LIBELLES_CURSUS_DEFAUT = {
    NIVEAU_MPSI: "Math Physique Sciences de l'Ingénieur",
    NIVEAU_MP:   'Mathématique Physique',
}


def libelle_cursus(code_niveau: str, institution=None) -> str:
    """
    Intitulé développé du cursus, tel qu'il coiffe les documents de séance.

    Lu dans le référentiel, où il s'administre. Une table en dur ignorait tout
    niveau ajouté après coup et lui aurait imprimé son code brut.
    """
    fiche = niveau_ref(code_niveau, institution)
    if fiche is not None and fiche.libelle:
        return fiche.libelle
    return LIBELLES_CURSUS_DEFAUT.get(code_niveau, code_niveau)


def classe_attente(code_niveau: str, annee_universitaire: str, institution) -> 'Classe':
    """
    Classe d'attente d'un niveau pour une année — créée au besoin.

    Sans suffixe : « MPSI », et non « MPSI A ». C'est là qu'arrivent les
    inscrits tant qu'on ignore combien de classes ouvrir. Elle porte le NIVEAU,
    ce qui suffit pour que l'étudiant reçoive sa maquette et ses frais dès
    l'inscription — un rattachement en attente n'est pas une inscription
    incomplète.

    Marquée conteneur par `Classe.save()`, elle reste hors des emplois du
    temps, du suivi et des listes d'appel.
    """
    from .socle import niveau_socle

    classe, _ = Classe.objects.get_or_create(
        institution=institution,
        niveau=niveau_socle(code_niveau),
        groupe='',
        annee_universitaire=annee_universitaire,
    )
    return classe


def classes_reelles(annee_universitaire: str, institution=None):
    """Classes planifiables — la classe d'attente exclue."""
    qs = Classe.objects.filter(annee_universitaire=annee_universitaire).exclude(groupe='')
    if institution is not None:
        qs = qs.filter(institution=institution)
    return qs


def affecter_a_une_classe(inscription: InscriptionIPGEI, classe: 'Classe',
                          utilisateur=None) -> InscriptionIPGEI:
    """
    Sort un inscrit de la classe d'attente pour le poser dans une vraie classe.

    Passe par le même chemin qu'un changement de classe : le mouvement est
    journalisé et le département de l'étudiant suit, sans quoi il resterait
    absent des listes d'appel.
    """
    if classe.est_conteneur:
        raise ValidationError("Une classe d'attente n'est pas une affectation.")
    if inscription.classe_id == classe.pk:
        raise ValidationError(f'{inscription.etudiant} est déjà en {classe.nom}.')
    if inscription.classe.niveau_id != classe.niveau_id:
        raise ValidationError("L'affectation reste au même niveau.")

    _deplacer(inscription, classe, None, "Affectation depuis la classe d'attente",
              utilisateur)
    return inscription


#: Signataire de repli, quand le référentiel est vide. C'est ce que les
#: gabarits imprimaient en dur avant qu'il existe.
SIGNATAIRE_DEFAUT = {'nom': '', 'titre': 'Chef service de la Scolarité'}


def signataire_du_document(institution, signataire_id=None) -> dict:
    """
    Qui signe : le signataire demandé, sinon celui par défaut.

    Renvoie un DICTIONNAIRE et non l'objet : ce qui part sur le document doit
    être recopié, pas pointé. Une pièce déjà remise ne peut pas changer de
    signataire parce que l'intéressé a quitté l'établissement.

    Le titre seul suffit si le nom manque — c'est ce que faisaient les gabarits
    jusqu'ici, et cela reste valable pour un établissement qui signe par
    fonction plutôt que par personne.
    """
    from .models import SignataireIPGEI

    from django.core.exceptions import ValidationError

    requete = SignataireIPGEI.objects.filter(institution=institution, actif=True)
    if signataire_id:
        signataire = requete.filter(pk=signataire_id).first()
        # Un identifiant inconnu — venu d'une autre institution, ou désactivé
        # depuis l'ouverture de l'écran — ne retombe PAS silencieusement sur le
        # signataire par défaut : l'agent croirait avoir édité au nom de celui
        # qu'il a choisi, et le document porterait un autre nom.
        if signataire is None:
            raise ValidationError(
                {'signataire': 'Signataire inconnu ou désactivé pour cette institution.'})
    else:
        signataire = requete.filter(par_defaut=True).first() or requete.first()
    if signataire is None:
        return dict(SIGNATAIRE_DEFAUT)
    return {
        'nom':      signataire.nom_fr,
        'titre':    signataire.titre_fr,
        'nom_ar':   signataire.nom_ar,
        'titre_ar': signataire.titre_ar,
    }


def libelle_annee_du_niveau(code_niveau: str, institution=None) -> str:
    """« 1re année », « 2e année » — déduit du RANG du niveau.

    Le déduire du code, en testant `== 'MPSI'`, rangeait tout autre niveau de
    première année — MPI — en deuxième.
    """
    rang = rang_du_niveau(code_niveau, institution)
    return '1re année' if rang == 1 else f'{rang}e année'


def libelle_pv(deliberation) -> str:
    """
    Titre du procès-verbal, dans la forme que l'institut édite depuis
    toujours : « PV des Résultats annuels de la Classe MPSI A ».

    Il nomme la CLASSE : deux PV d'un même niveau ne se distinguaient
    autrement qu'à leur contenu, ce qui, sur une table de jury où circulent
    quatre feuillets, ne suffit pas. Les jurys tenus avant que la
    délibération ne soit rattachée à une classe portent leur niveau — dire
    « Classe MPSI » d'un document qui couvre deux classes serait faux.
    """
    # Le rattrapage se nomme, et ne se devine pas : posés côte à côte, ses
    # feuillets et ceux du jury annuel portent la même classe et la même année.
    portee = ('de la session de rattrapage'
              if deliberation.portee == deliberation.PORTEE_RATTRAPAGE
              else 'annuels' if deliberation.portee == deliberation.PORTEE_ANNUELLE
              else f'du {deliberation.semestre.code}'
                   if deliberation.semestre_id else '')
    cible = (f'la Classe {deliberation.classe.nom}' if deliberation.classe_id
             else f'du niveau {deliberation.niveau}')
    return f'PV des Résultats {portee} de {cible}'.replace('  ', ' ')


def libelle_classe(classe) -> str:
    """
    Classe telle qu'elle se nomme sur les documents : « Cursus (Classe) ».

        Math Physique Sciences de l'Ingénieur (MPSI A)
        Mathématique Physique (MP C)

    Le seul code de classe — « MPSI A » — ne dit rien à qui lit l'attestation
    hors de l'établissement : ni le contenu du cursus, ni son niveau. C'est la
    forme employée par l'IPGEI sur ses propres relevés.
    """
    if classe is None:
        return ''
    intitule = libelle_cursus(classe.niveau_code, getattr(classe, 'institution', None))
    return f'{intitule} ({classe.nom})' if intitule and intitule != classe.niveau_code else classe.nom


def libelle_semestre_ordinal(code_semestre: str) -> str:
    """
    « 1er Semestre », « 2e Semestre »… — la forme employée sur les documents.

    Le rang du semestre dans l'année ne suffit pas : S3 est le troisième
    semestre du cursus et s'écrit ainsi, même s'il ouvre la deuxième année.
    """
    try:
        numero = int(code_semestre[1:])
    except (TypeError, ValueError):
        return code_semestre
    return f'{numero}er Semestre' if numero == 1 else f'{numero}e Semestre'


def statistiques_classe(inscription: InscriptionIPGEI, semestre: SemestreIPGEI) -> dict:
    """
    Par matière : moyenne de la CLASSE et rang de l'étudiant dedans.

    Le repère est la classe et non le niveau : un étudiant de MPSI A se situe
    parmi ses camarades, pas parmi tous les premières années — c'est ce que
    signale l'en-tête du relevé, qui nomme la classe.

    Une seule requête pour toute la classe, puis un regroupement en mémoire :
    interroger matière par matière coûtait autant d'allers-retours que de
    lignes au relevé.

    Rang : les ex æquo partagent le même rang, et le suivant saute d'autant —
    deux premiers, puis un troisième. Les copies sans note retenue ne sont pas
    classées ; elles ne comptent pas non plus dans la moyenne de classe, sans
    quoi une absence de saisie la ferait chuter.
    """
    lignes = (
        Note.objects
        .filter(semestre=semestre,
                inscription__classe_id=inscription.classe_id,
                inscription__actif=True,
                note_retenue__isnull=False)
        .values_list('matiere_id', 'inscription_id', 'note_retenue')
    )

    par_matiere = {}
    for matiere_id, inscription_id, note in lignes:
        par_matiere.setdefault(matiere_id, []).append((inscription_id, Decimal(note)))

    stats = {}
    for matiere_id, notes in par_matiere.items():
        valeurs = [n for _, n in notes]
        moyenne = _arrondi(sum(valeurs) / len(valeurs))

        mienne = next((n for i, n in notes if i == inscription.pk), None)
        rang = None
        if mienne is not None:
            # Rang = nombre de notes STRICTEMENT supérieures, plus un.
            rang = sum(1 for n in valeurs if n > mienne) + 1

        stats[matiere_id] = {
            'moyenne_classe': moyenne,
            'rang':           rang,
            'effectif':       len(valeurs),
        }
    return stats


def rang_general_classe(inscription: InscriptionIPGEI, semestre: SemestreIPGEI) -> dict:
    """
    Rang de l'étudiant dans sa classe sur la moyenne du SEMESTRE, et effectif
    classé.

    Le classement se refait ici plutôt que de s'appuyer sur la délibération :
    un relevé s'imprime en cours d'année, avant tout jury, et devrait alors
    afficher un rang vide alors que les notes existent.

    La moyenne de chaque camarade est recalculée à partir des mêmes lignes de
    note, avec les coefficients FIGÉS dessus — ceux qui ont servi au calcul.
    Reprendre ceux de la matière classerait sur une pondération que personne
    n'a subie.

    Une seule requête pour toute la classe. Les ex æquo partagent leur rang,
    comme pour le classement par matière.
    """
    return _classer(inscription, Note.objects.filter(
        semestre=semestre,
        inscription__classe_id=inscription.classe_id,
        inscription__actif=True,
        note_retenue__isnull=False))


def _classer(inscription: InscriptionIPGEI, lignes) -> dict:
    """
    Rang de l'étudiant parmi les moyennes tirées de `lignes`, et effectif classé.

    Le périmètre est décidé par l'appelant — une classe, une promotion, un
    semestre ou l'année — et le calcul reste le même : moyenne pondérée par les
    coefficients FIGÉS sur chaque note, ex æquo partagés.
    """
    cumuls = {}
    for inscription_id, note, coef_fige, coef_matiere in lignes.values_list(
        'inscription_id', 'note_retenue', 'coefficient', 'matiere__coefficient',
    ):
        coef = Decimal(coef_fige if coef_fige is not None else coef_matiere)
        cumul, poids = cumuls.get(inscription_id, (Decimal('0'), Decimal('0')))
        cumuls[inscription_id] = (cumul + Decimal(note) * coef, poids + coef)

    moyennes = {i: c / p for i, (c, p) in cumuls.items() if p > 0}
    mienne = moyennes.get(inscription.pk)

    return {
        'rang':     (sum(1 for m in moyennes.values() if m > mienne) + 1)
                    if mienne is not None else None,
        'effectif': len(moyennes),
    }


def rang_general_promotion(inscription: InscriptionIPGEI,
                           semestre: SemestreIPGEI) -> dict:
    """
    Rang sur la PROMOTION — toutes les classes du niveau confondues.

    Une promotion de MPSI répartie en MPSI A et MPSI B suit le même programme
    et compose les mêmes épreuves : le rang dans sa classe ne dit pas où l'on
    se situe dans sa promotion, et un premier de MPSI B peut être quinzième de
    la promotion. Les deux chiffres sont donnés côte à côte plutôt que l'un à
    la place de l'autre : le premier situe dans le groupe de travail, le second
    dans le concours à venir.

    La répartition entre classes est administrative — elle n'a pas à peser sur
    la lecture d'un résultat.
    """
    return _classer(inscription, Note.objects.filter(
        semestre=semestre,
        inscription__classe__niveau_id=inscription.classe.niveau_id,
        inscription__annee_universitaire=inscription.annee_universitaire,
        inscription__actif=True,
        note_retenue__isnull=False))


def rang_annuel_promotion(inscription: InscriptionIPGEI,
                          annee_universitaire: str) -> dict:
    """
    Rang sur la promotion à l'ANNÉE, tous semestres cumulés.

    C'est le classement qui compte pour le passage et pour le concours ; celui
    d'un semestre isolé ne le préjuge pas.
    """
    return _classer(inscription, Note.objects.filter(
        semestre__annee_universitaire=annee_universitaire,
        inscription__classe__niveau_id=inscription.classe.niveau_id,
        inscription__annee_universitaire=annee_universitaire,
        inscription__actif=True,
        note_retenue__isnull=False))


def rang_annuel_classe(inscription: InscriptionIPGEI,
                       annee_universitaire: str) -> dict:
    """Rang dans sa classe à l'année — le pendant de `rang_annuel_promotion`."""
    return _classer(inscription, Note.objects.filter(
        semestre__annee_universitaire=annee_universitaire,
        inscription__classe_id=inscription.classe_id,
        inscription__actif=True,
        note_retenue__isnull=False))


def seuil_applicable(inscription: InscriptionIPGEI, annee_universitaire: str,
                     semestre: SemestreIPGEI = None):
    """
    Le seuil qui juge un relevé : celui FIGÉ par le jury qui a statué sur ce
    dossier, sinon le paramétrage courant.

    Le PV lit `Deliberation.seuil_validation` ; le relevé lisait le paramètre
    du jour. Tant que le seuil ne bougeait pas, les deux coïncidaient. Il a
    bougé — 7,50 puis 9,00 — et un étudiant admis au jury recevait un relevé
    qui le déclarait « non validé ». Deux pièces officielles de la même
    scolarité se contredisaient, et c'est celle remise à l'élève qui avait tort.

    Seule une délibération VALIDÉE fait foi : un jury calculé mais non signé
    n'a encore rien décidé. En cas de second jury (rattrapage), le plus récent.
    """
    lignes = LigneDeliberation.objects.filter(
        inscription=inscription,
        deliberation__statut=Deliberation.STATUT_VALIDEE,
    )
    if semestre is not None:
        lignes = lignes.filter(deliberation__portee=Deliberation.PORTEE_SEMESTRE,
                               deliberation__semestre=semestre)
    else:
        lignes = lignes.filter(
            deliberation__annee_universitaire=annee_universitaire,
            deliberation__portee__in=[Deliberation.PORTEE_ANNUELLE,
                                      Deliberation.PORTEE_RATTRAPAGE],
        )
    ligne = (lignes.select_related('deliberation')
             .order_by('-deliberation__date_validation', '-deliberation__pk')
             .first())
    if ligne is not None:
        return ligne.deliberation.seuil_validation
    return ParametresIPGEI.pour(inscription.classe.institution).seuil_validation


def releve_semestre(inscription: InscriptionIPGEI, semestre: SemestreIPGEI) -> dict:
    """Données d'un relevé de semestre — consommées par l'API et le PDF."""
    moyenne, poids, notes = moyenne_semestrielle(inscription, semestre)
    seuil = seuil_applicable(inscription, semestre.annee_universitaire, semestre)
    return {
        'inscription': inscription,
        'semestre':    semestre,
        'notes':       sorted(notes, key=lambda n: (n.matiere.ordre, n.matiere.code)),
        'moyenne':     moyenne,
        'total_coefficients': poids,
        'seuil':       seuil,
        'valide':      moyenne is not None and moyenne >= seuil,
        'absences':    bilan_absences(inscription, semestre),
        'stats_classe': statistiques_classe(inscription, semestre),
        'rang_general': rang_general_classe(inscription, semestre),
        # Le rang sur la PROMOTION, toutes classes du niveau confondues : la
        # répartition en MPSI A et MPSI B est administrative, elle n'a pas à
        # peser sur la lecture d'un résultat.
        'rang_promotion': rang_general_promotion(inscription, semestre),
        'niveau':         inscription.classe.niveau_code,
    }


def releve_annuel(inscription: InscriptionIPGEI, annee_universitaire: str) -> dict:
    """
    Données d'un relevé annuel, regroupées par semestre.

    Chaque bloc porte EXACTEMENT ce que porte un relevé semestriel — moyennes
    de classe, rangs par matière, rang général, total des coefficients. Le
    relevé annuel n'est pas un résumé de l'année : c'est le relevé de chaque
    semestre, réuni sous une même signature. Un rang qui figure sur la pièce
    semestrielle et disparaît de l'annuelle se lit comme une omission.
    """
    moyenne, poids, notes = moyenne_annuelle(inscription, annee_universitaire)
    seuil = seuil_applicable(inscription, annee_universitaire)
    par_semestre = {}
    for n in notes:
        par_semestre.setdefault(n.semestre.code, []).append(n)

    semestres = []
    for code, lignes in sorted(par_semestre.items()):
        # Le semestre porté par les notes elles-mêmes : le regroupement se fait
        # par code, mais les statistiques de classe demandent l'objet.
        semestre = lignes[0].semestre
        moyenne_bloc, poids_bloc = _agreger(lignes)[:2]
        semestres.append({
            'code':     code,
            'semestre': semestre,
            'notes':    sorted(lignes, key=lambda n: (n.matiere.ordre, n.matiere.code)),
            'moyenne':  moyenne_bloc,
            'total_coefficients': poids_bloc,
            'stats_classe':       statistiques_classe(inscription, semestre),
            'rang_general':       rang_general_classe(inscription, semestre),
            'rang_promotion':     rang_general_promotion(inscription, semestre),
            'absences':           bilan_absences(inscription, semestre),
        })
    return {
        'inscription':         inscription,
        'annee_universitaire': annee_universitaire,
        'semestres':           semestres,
        'moyenne':             moyenne,
        'total_coefficients':  poids,
        'seuil':               seuil,
        'valide':              moyenne is not None and moyenne >= seuil,
        'absences':            bilan_absences(inscription),
        # Les deux classements de l'ANNÉE : celui qui compte pour le passage et
        # pour le concours. Le rang d'un semestre isolé ne le préjuge pas.
        'rang_general':        rang_annuel_classe(inscription, annee_universitaire),
        'rang_promotion':      rang_annuel_promotion(inscription, annee_universitaire),
        'niveau':              inscription.classe.niveau_code,
    }


# ═════════════════════════════════════════════════════════════════════════════
# Inscription
# ═════════════════════════════════════════════════════════════════════════════
@transaction.atomic
def creer_semestres_annee(annee_universitaire: str, institution) -> list:
    """
    Pose d'un coup les semestres manquants d'une année universitaire.

    Le calendrier IPGEI est daté, année par année : c'est ce qui permet de
    distinguer le S1 d'un redoublant de celui de l'année précédente. Mais rien
    n'obligeait à saisir les quatre semestres un par un, et c'est ainsi qu'une
    année se retrouvait à moitié ouverte — ses inscrits n'ayant alors que la
    moitié de leurs matières, sans que rien ne le signale à l'inscription.

    Les dates suivent la convention lisible dans les années déjà saisies :
    semestres impairs à la mi-septembre de la première année du libellé, pairs
    au 1er février de la seconde. Ce sont des repères de départ, modifiables
    ensuite, pas une décision figée.

    Idempotent : un semestre déjà créé n'est ni recréé ni redaté.
    """
    from datetime import date

    from .models import CODE_SEMESTRE_CHOICES

    try:
        debut, fin = (int(part) for part in str(annee_universitaire).split('-'))
    except (AttributeError, ValueError):
        raise ValidationError(
            {'annee_universitaire': "Année attendue sous la forme « 2026-2027 »."}
        )

    # L'année est d'abord déclarée au REGISTRE du socle : c'est lui l'autorité
    # — l'écran Paramètres → Années la montre, la grille tarifaire s'y accroche.
    # L'ouvrir ici sans l'y inscrire laissait les deux sources diverger.
    from .socle import annee_socle
    annee_socle(annee_universitaire)

    parametres = ParametresIPGEI.pour(institution)
    crees = []
    for code, _ in CODE_SEMESTRE_CHOICES:
        if SemestreIPGEI.objects.filter(
            institution=institution, code=code, annee_universitaire=annee_universitaire,
        ).exists():
            continue
        impair = code in ('S1', 'S3')
        crees.append(SemestreIPGEI.objects.create(
            institution=institution, code=code,
            annee_universitaire=annee_universitaire,
            date_debut=date(debut, 9, 15) if impair else date(fin, 2, 1),
            nb_semaines=parametres.nb_semaines_defaut,
        ))
    return crees


def semestres_du_niveau(niveau: str, annee_universitaire: str, institution=None):
    """
    Semestres d'une année d'étude : S1/S2 en première année, S3/S4 en deuxième.

    `institution` cloisonne le résultat. Sans elle, deux établissements ayant la
    même année et les mêmes codes verraient leurs calendriers se mélanger, et un
    inscrit recevrait des lignes de note pointant vers le semestre d'un autre —
    sans que rien ne le signale.
    """
    semestres = SemestreIPGEI.objects.filter(
        code__in=codes_semestres_du_niveau(niveau, institution),
        annee_universitaire=annee_universitaire,
    )
    if institution is not None:
        semestres = semestres.filter(institution=institution)
    return semestres.order_by('code')


@transaction.atomic
def inscrire_aux_matieres(inscription: InscriptionIPGEI) -> int:
    """
    Inscription pédagogique — automatique et entière.

    À l'IPGEI il n'y a ni dette ni crédit : on ne choisit pas ses matières, et
    on n'en traîne aucune d'une année sur l'autre. Inscrire un étudiant dans
    une classe, c'est donc l'inscrire à TOUTES les matières des deux semestres
    de son niveau — l'étape pédagogique de l'inscription universitaire, mais
    sans rien à décider, donc sans rien à oublier.

    Idempotent : relancer ne crée que ce qui manque et n'écrase aucune note.
    """
    total = 0
    for semestre in semestres_du_niveau(
        inscription.classe.niveau_code, inscription.annee_universitaire,
        inscription.classe.institution,
    ):
        total += initialiser_notes(inscription, semestre)
    return total


def _derogation_active(etudiant, classe) -> bool:
    """
    Une dérogation d'inscription en cours pour cette année.

    Elle ne lève JAMAIS l'interdiction de redoubler la 1re année : cette règle
    n'admet pas d'exception, et l'appeler ici la rendrait négociable.
    """
    from apps.inscriptions.models import Derogation
    from apps.parametres.models import Year

    annee = Year.objects.filter(annee=classe.annee_universitaire).first()
    if annee is None:
        return False
    return Derogation.objects.filter(
        etudiant=etudiant, annee_univ=annee,
        type_derogation='derogation_inscription', statut='actif',
    ).exists()


def controler_reinscription(etudiant, classe) -> tuple:
    """
    Dit si l'étudiant peut être inscrit dans cette classe, et à quel titre.

    Se réinscrire dans le niveau déjà suivi, c'est redoubler. Or le
    redoublement n'existe pas en 1re année : un MPSI est admis en 2e année ou
    réorienté, jamais maintenu. En 2e année il est possible, mais il se
    décide — c'est le jury qui l'accorde, en posant le statut « redoublant »
    sur l'inscription de l'année écoulée, et le droit s'épuise.

    Refuser ici plutôt qu'au jury évite le cas où l'on rattrape la règle trop
    tard : une fois l'étudiant inscrit, ses notes et ses absences existent.

    Retourne `(statut, nb_redoublements)` à poser sur la nouvelle inscription.
    Lève `ValidationError` si l'inscription est interdite.
    """
    anterieures = list(
        InscriptionIPGEI.objects
        .filter(etudiant=etudiant)
        .exclude(annee_universitaire=classe.annee_universitaire)
        .select_related('classe')
        .order_by('-annee_universitaire')
    )
    memes = [i for i in anterieures if i.classe.niveau_code == classe.niveau_code]
    if not memes:
        return InscriptionIPGEI.STATUT_ACTIF, 0

    precedente = memes[0]
    # Le droit au redoublement se lit sur le niveau. Le référentiel le refuse
    # d'office en première année ; un niveau ajouté suit sa propre case, sans
    # que cette règle ait à connaître son code.
    fiche = niveau_ref(classe.niveau_code, classe.institution)
    redoublement_ouvert = (fiche.redoublement_autorise if fiche is not None
                           else classe.niveau_code != NIVEAU_MPSI)
    if not redoublement_ouvert:
        raise ValidationError(
            f"Le redoublement n'est pas autorisé en "
            f'{fiche.libelle_rang if fiche is not None else "1re année"}. '
            f'{etudiant} a déjà suivi {precedente.classe.niveau_code} en '
            f'{precedente.annee_universitaire}.'
        )

    # Une dérogation d'inscription accordée par l'administration tient lieu de
    # décision : c'est la voie prévue quand le jury n'a pas statué, ou quand le
    # droit est épuisé mais qu'une circonstance le justifie. Elle est datée,
    # motivée et signée — à la différence d'un contournement en base.
    if _derogation_active(etudiant, classe):
        return InscriptionIPGEI.STATUT_REDOUBLANT, precedente.nb_redoublements

    if precedente.statut != InscriptionIPGEI.STATUT_REDOUBLANT:
        raise ValidationError(
            f'Le redoublement en 2e année se décide en jury. '
            f"L'inscription de {precedente.annee_universitaire} porte le statut "
            f'« {precedente.get_statut_display()} », pas « Redoublant ».'
        )

    droit_max = ParametresIPGEI.pour(classe.institution).droit_redoublement_max
    if precedente.nb_redoublements > droit_max:
        raise ValidationError(
            f'Droit de redoublement épuisé : {precedente.nb_redoublements} '
            f'redoublement(s) pour un maximum de {droit_max}. '
            "Une dérogation d'inscription est nécessaire."
        )

    # Le compteur suit l'étudiant : c'est lui qui borne le droit, et le jury
    # l'a déjà incrémenté en accordant le redoublement.
    return InscriptionIPGEI.STATUT_REDOUBLANT, precedente.nb_redoublements


@transaction.atomic
def rattacher_inscrits(semestre: SemestreIPGEI) -> int:
    """
    Inscrit aux matières d'un semestre tous les étudiants déjà inscrits.

    L'automatisme ne peut pas être à sens unique. Un étudiant inscrit en
    septembre l'est pour l'année, mais le semestre pair n'existe souvent pas
    encore à cette date, et la matière d'un semestre s'ajoute parfois après la
    rentrée. Sans ce rattrapage, ces étudiants n'auraient jamais la ligne de
    note correspondante — et rien ne le signalerait avant la délibération.

    Idempotent.

    Un semestre clos — ou une année close — ne reçoit plus de ligne : une
    matière créée après le jury injectait sinon une note vide, non verrouillée,
    dans chaque dossier délibéré, et la moyenne recalculée tombait sous ce que
    le PV signé affirme.
    """
    if semestre.est_cloture or annee_cloturee(semestre.annee_universitaire):
        return 0

    inscriptions = (
        InscriptionIPGEI.objects
        .filter(actif=True,
                annee_universitaire=semestre.annee_universitaire,
                classe__annee_universitaire=semestre.annee_universitaire)
        .select_related('classe')
    )
    total = 0
    for inscription in inscriptions:
        # Comparaison sur le RANG et non sur le code : `semestre.niveau` déduit
        # « MP » de S3, si bien qu'un inscrit d'un autre niveau de 2e année —
        # MPI — n'aurait jamais reçu ses lignes de note, et rien ne l'aurait
        # signalé avant la délibération.
        if (rang_du_niveau(inscription.classe.niveau_code, semestre.institution)
                == rang_du_semestre(semestre.code)):
            total += initialiser_notes(inscription, semestre)
    return total


def frais_pour(classe) -> Decimal:
    """
    Montant dû pour une inscription dans cette classe, d'après la grille du
    socle (`inscriptions.GrilleFrais`).

    On ne recopie pas la grille côté IPGEI : elle est déjà tenue par la
    scolarité, par (institution, année, type de diplôme, niveau). Ne manquait
    que la traduction du niveau IPGEI — MPSI vaut 1re année, MP la 2e — et de
    l'année, que le socle porte en table quand IPGEI la porte en texte.

    Retourne 0 si aucun tarif n'est défini : mieux vaut une inscription à zéro,
    visible et corrigeable, qu'une inscription refusée en pleine rentrée.
    """
    from apps.inscriptions.models import GrilleFrais
    from apps.parametres.models import Year

    annee = Year.objects.filter(annee=classe.annee_universitaire).first()
    if annee is None:
        return Decimal('0')

    type_diplome = getattr(getattr(classe, 'filiere', None), 'type_diplome', None)
    montant = GrilleFrais.objects.filter(
        institution_id=classe.institution_id,
        annee_univ_id=annee.pk,
        niveau=rang_du_niveau(classe.niveau_code, classe.institution),
        actif=True,
        **({'type_diplome': type_diplome} if type_diplome else {}),
    ).values_list('montant', flat=True).first()
    return montant if montant is not None else Decimal('0')


# ═════════════════════════════════════════════════════════════════════════════
# Passage en année supérieure
# ═════════════════════════════════════════════════════════════════════════════
def annee_suivante(annee_universitaire: str) -> str:
    """« 2026-2027 » → « 2027-2028 ». Rendue telle quelle si la forme surprend."""
    try:
        debut, fin = (int(part) for part in annee_universitaire.split('-'))
    except (AttributeError, ValueError):
        return annee_universitaire
    return f'{debut + 1}-{fin + 1}'


def niveaux_du_rang(rang: int, institution=None) -> list:
    """Niveaux actifs d'un rang d'année, dans l'ordre d'affichage."""
    from .models import NiveauIPGEI

    requete = NiveauIPGEI.objects.filter(rang=rang, actif=True)
    if institution is not None:
        requete = requete.filter(institution=institution)
    return list(requete.order_by('ordre', 'code'))


#: Ce qu'une décision de jury commande pour l'année suivante.
#:
#: Seuls deux cas font repartir un étudiant : l'admission le fait monter d'un
#: rang, le redoublement le laisse au sien. Réorienté, exclu et abandon ne
#: repartent pas ; « autorisé à concourir » non plus — c'est une décision de
#: DEUXIÈME année, qui ouvre le concours, pas une troisième année de prépa.
PASSAGE_MONTE     = 'monte'
PASSAGE_REDOUBLE  = 'redouble'
SUITE_PAR_STATUT  = {
    InscriptionIPGEI.STATUT_ADMIS:      PASSAGE_MONTE,
    InscriptionIPGEI.STATUT_REDOUBLANT: PASSAGE_REDOUBLE,
}


#: Décisions qui NE DÉCIDENT PAS de la suite : elles ouvrent une étape de plus.
#: « Autorisé au rattrapage » attend le second jury ; « autorisé à concourir »
#: attend le résultat du concours. Ce que l'étudiant devient est alors porté par
#: son inscription, et non par cette ligne-ci.
DECISIONS_EN_SUSPENS = (
    LigneDeliberation.DECISION_RATTRAPAGE,
    LigneDeliberation.DECISION_CNIM,
)


def candidats_passage(deliberation: Deliberation) -> list:
    """
    Qui repart l'an prochain, et vers quel niveau.

    Lit les LIGNES du jury : c'est la décision prononcée qui fait foi, et elle
    est là même si le report sur l'inscription a été défait depuis.

    Deux décisions font exception, et il fallait les traiter : « autorisé au
    rattrapage » et « autorisé à concourir » ne disent pas la suite — elles
    ouvrent une étape de plus. Ce que l'étudiant devient est alors décidé par le
    second jury ou par le concours, et vit sur son INSCRIPTION. Sans cette
    lecture, un redoublant issu d'un concours manqué restait invisible du
    passage : sa ligne disait « autorisé à concourir », et personne ne le
    réinscrivait.

    Chaque entrée dit l'inscription, la suite qui lui est faite et le code de
    niveau visé.
    """
    institution = deliberation.institution
    rang_actuel = rang_du_niveau(deliberation.niveau, institution)

    sortie = []
    for ligne in deliberation.lignes.select_related('inscription__etudiant',
                                                    'inscription__classe'):
        if ligne.decision in DECISIONS_EN_SUSPENS:
            # La suite ne se lit plus ici, mais sur l'inscription : c'est le
            # jury de rattrapage ou le concours qui l'a arrêtée.
            statut = ligne.inscription.statut
        else:
            statut = {
                LigneDeliberation.DECISION_ADMIS:      InscriptionIPGEI.STATUT_ADMIS,
                LigneDeliberation.DECISION_REDOUBLANT: InscriptionIPGEI.STATUT_REDOUBLANT,
            }.get(ligne.decision)
        suite = SUITE_PAR_STATUT.get(statut)
        if suite is None:
            continue
        rang_cible = rang_actuel + 1 if suite == PASSAGE_MONTE else rang_actuel
        niveaux = niveaux_du_rang(rang_cible, institution)
        sortie.append({
            'ligne':       ligne,
            'inscription': ligne.inscription,
            'suite':       suite,
            'rang_cible':  rang_cible,
            'niveaux':     [n.code for n in niveaux],
        })
    return sortie


def passer_en_annee_suivante(deliberation: Deliberation, *, niveau_cible: str = '',
                             annee_cible: str = '', utilisateur=None) -> dict:
    """
    Ouvre l'année suivante aux étudiants que le jury a fait repartir.

    Chaque inscription est créée dans la CLASSE D'ATTENTE du niveau visé, jamais
    dans une classe nommée : à ce moment on ignore encore combien de classes
    ouvrir et comment les équilibrer. L'écran « Affectation aux classes » les
    répartit ensuite, et l'étudiant y reçoit déjà sa maquette et ses frais.

    Réservé à une délibération VALIDÉE : inscrire sur des décisions qui peuvent
    encore changer créerait des dossiers à défaire un par un.

    Rejoué, il ne fait rien de plus : un étudiant déjà inscrit sur l'année cible
    est compté « déjà inscrit » et laissé tel quel. C'est ce qui permet de
    relancer après avoir corrigé un cas sans redouter les doublons.
    """
    if deliberation.statut != Deliberation.STATUT_VALIDEE:
        raise ValidationError(
            'Le passage suppose un jury validé : tant que les décisions peuvent '
            'changer, les inscriptions créées seraient à défaire une par une.'
        )
    if deliberation.portee != Deliberation.PORTEE_ANNUELLE:
        raise ValidationError(
            "Seul un jury ANNUEL prononce un passage : un jury de semestre n'est "
            "qu'un bilan intermédiaire."
        )

    from apps.parametres.models import Year

    institution = deliberation.institution
    cible = annee_cible or annee_suivante(deliberation.annee_universitaire)
    # Le passage OUVRE une année : elle doit exister au registre du socle avant
    # qu'on y inscrive qui que ce soit, sans quoi la grille tarifaire ne s'y
    # accroche pas et les frais sortent à zéro.
    #
    # Créer l'année est un acte discret — un libellé, sans dates ni tarif — mais
    # il ne doit pas être SILENCIEUX : c'est en la découvrant après coup qu'on
    # se demande d'où elle sort. Le bilan le dira.
    from .socle import annee_socle
    annee_ouverte = not Year.objects.filter(annee=cible).exists()
    annee_socle(cible)

    crees, deja, refuses = [], [], []
    with transaction.atomic():
        for candidat in candidats_passage(deliberation):
            inscription = candidat['inscription']
            etudiant    = inscription.etudiant

            codes = candidat['niveaux']
            if niveau_cible:
                codes = [niveau_cible] if niveau_cible in codes else []
            if not codes:
                refuses.append({
                    'etudiant': etudiant.matricule,
                    'motif': f"Aucun niveau actif de rang {candidat['rang_cible']} "
                             f'ne correspond.',
                })
                continue
            # Un seul niveau du rang visé : le choix ne se pose pas. Plusieurs,
            # et sans consigne, on ne devine pas — mieux vaut le dire.
            if len(codes) > 1:
                refuses.append({
                    'etudiant': etudiant.matricule,
                    'motif': 'Plusieurs niveaux possibles (' + ', '.join(codes)
                             + ') : précisez lequel.',
                })
                continue

            if InscriptionIPGEI.objects.filter(
                etudiant=etudiant, annee_universitaire=cible,
            ).exists():
                deja.append(etudiant.matricule)
                continue

            classe = classe_attente(codes[0], cible, institution)
            try:
                statut, nb = controler_reinscription(etudiant, classe)
                montant = frais_pour(classe)
                nouvelle = InscriptionIPGEI.objects.create(
                    etudiant=etudiant, classe=classe, annee_universitaire=cible,
                    statut=statut, nb_redoublements=nb,
                    montant_frais=montant,
                    # D'où vient ce dossier : la ligne de jury qui l'a décidé.
                    # Sans ce lien, une inscription de MP ne dit pas de quelle
                    # décision elle procède, ni quelle décision est restée
                    # lettre morte.
                    issue_de=candidat['ligne'],
                )
                inscrire_aux_matieres(nouvelle)
            except ValidationError as exc:
                refuses.append({
                    'etudiant': etudiant.matricule,
                    'motif': (getattr(exc, 'messages', None) or [str(exc)])[0][:200],
                })
                continue
            crees.append({
                'etudiant': etudiant.matricule,
                'niveau':   codes[0],
                'suite':    candidat['suite'],
                'montant':  str(montant),
            })

    # Un tarif à zéro n'est pas une erreur — la grille du socle n'est
    # simplement pas encore renseignée pour l'année visée — mais ouvrir
    # quatorze dossiers à zéro sans le dire les rendrait invisibles jusqu'au
    # jour où la scolarité s'étonne qu'aucun ne soit dû.
    sans_tarif = [c['etudiant'] for c in crees if Decimal(c['montant']) == 0]

    return {
        'annee_cible':   cible,
        'inscriptions':  len(crees),
        'deja_inscrits': len(deja),
        'crees':         crees,
        'refuses':       refuses,
        'sans_tarif':    len(sans_tarif),
        'annee_ouverte': annee_ouverte,
    }


def recalculer_frais(annee_universitaire: str, institution=None,
                     classe_id=None) -> dict:
    """
    Repose le tarif de la grille sur les inscriptions d'une année.

    Le montant est FIGÉ sur l'inscription à sa création : c'est ce qui permet
    à un reçu de rester exact quand la grille change en cours d'année. Mais
    quand la grille est renseignée APRÈS coup — le cas d'un passage en année
    supérieure ouvert avant que la scolarité n'ait fixé les tarifs —, les
    dossiers restent à zéro et rien ne le rattrape.

    Les inscriptions DÉJÀ PAYÉES ne sont pas touchées : un reçu porte le
    montant encaissé, le réécrire ferait mentir une pièce déjà remise. Elles
    sont comptées à part, pour qu'un écart connu ne passe pas pour un oubli.
    """
    requete = (InscriptionIPGEI.objects
               .filter(annee_universitaire=annee_universitaire)
               .select_related('classe'))
    if institution is not None:
        requete = requete.filter(classe__institution=institution)
    if classe_id:
        requete = requete.filter(classe_id=classe_id)

    tarifs = {}
    modifiees, payees, inchangees = 0, 0, 0
    for inscription in requete:
        if inscription.est_payee:
            payees += 1
            continue
        if inscription.classe_id not in tarifs:
            tarifs[inscription.classe_id] = frais_pour(inscription.classe)
        montant = tarifs[inscription.classe_id]
        if inscription.montant_frais == montant:
            inchangees += 1
            continue
        inscription.montant_frais = montant
        inscription.save(update_fields=['montant_frais'])
        modifiees += 1

    return {
        'annee_universitaire': annee_universitaire,
        'mises_a_jour':        modifiees,
        'deja_payees':         payees,
        'inchangees':          inchangees,
    }


@transaction.atomic
def repartir_inscriptions(mouvements: list, utilisateur=None,
                          motif: str = 'Répartition entre classes') -> dict:
    """
    Déplace plusieurs inscrits d'un coup, d'une classe vers une autre.

    À la rentrée, on ne traite pas des demandes individuelles : on constitue
    des groupes. Le circuit de la permutation — formulaire signé, accord des
    deux élèves — a tout son sens quand un étudiant demande à changer en cours
    d'année, et aucun quand la scolarité répartit dix-sept arrivants entre deux
    classes. D'où ce chemin distinct, qui emprunte pourtant le MÊME mouvement :
    l'historique est journalisé ligne à ligne, et le département de l'étudiant
    suit — sans quoi il disparaîtrait des listes d'appel.

    Tout passe ou rien : un mouvement refusé annule les précédents. Une
    répartition à moitié appliquée laisserait deux classes fausses, et il
    faudrait deviner lesquelles reprendre.

    `mouvements` : `[{'inscription': id, 'classe': id}, …]`
    """
    from .models import Classe

    if not mouvements:
        raise ValidationError('Aucun mouvement demandé.')

    inscriptions = {
        i.pk: i for i in InscriptionIPGEI.objects
        .filter(pk__in=[m.get('inscription') for m in mouvements])
        .select_related('classe__niveau', 'etudiant')
    }
    classes = {
        c.pk: c for c in Classe.objects
        .filter(pk__in=[m.get('classe') for m in mouvements])
        .select_related('niveau')
    }

    deplacees = []
    for mouvement in mouvements:
        inscription = inscriptions.get(mouvement.get('inscription'))
        cible = classes.get(mouvement.get('classe'))
        if inscription is None or cible is None:
            raise ValidationError('Inscription ou classe introuvable.')
        if cible.est_conteneur:
            raise ValidationError(
                f"{cible.nom} est une classe d'attente : on n'y répartit pas, "
                "on en sort."
            )
        if inscription.classe_id == cible.pk:
            continue
        if inscription.classe.niveau_id != cible.niveau_id:
            raise ValidationError(
                f'{inscription.etudiant} est en {inscription.classe.niveau_code}, '
                f'{cible.nom} relève de {cible.niveau_code} : une répartition ne '
                'change pas de niveau.'
            )
        if inscription.annee_universitaire != cible.annee_universitaire:
            raise ValidationError(
                f'{inscription.etudiant} est inscrit en '
                f'{inscription.annee_universitaire} et {cible.nom} est une classe '
                f'de {cible.annee_universitaire}.'
            )
        _deplacer(inscription, cible, None, motif, utilisateur)
        deplacees.append({'etudiant': inscription.etudiant.matricule,
                          'vers': cible.nom})

    return {'deplacees': len(deplacees), 'mouvements': deplacees}


@transaction.atomic
def affecter_sous_groupes(affectations: list, utilisateur=None) -> dict:
    """
    Place plusieurs inscrits dans les sous-groupes de LEUR classe, en une fois.

    Distinct de `repartir_inscriptions`, qui change de CLASSE : ici la classe ne
    bouge pas, seule la moitié change. C'est le geste de la rentrée, quand la
    scolarité constitue ses groupes de TD — et celui d'après une répartition,
    qui vide les sous-groupes puisqu'un groupe appartient à une classe.

    Aucun `HistoriqueClasse` n'est écrit : ce journal retrace les changements de
    CLASSE, et y consigner un déplacement interne le rendrait illisible — on n'y
    distinguerait plus un vrai transfert d'un simple regroupement de TD.

    `affectations` : `[{'inscription': id, 'sous_groupe': id | None}, …]`.
    `sous_groupe` à `None` sort l'élève de son groupe sans le sortir de sa classe.

    Tout passe ou rien : une répartition à moitié appliquée laisserait deux
    groupes faux, et rien ne dirait lesquels.
    """
    if not affectations:
        raise ValidationError('Aucune affectation demandée.')

    inscriptions = {
        i.pk: i for i in InscriptionIPGEI.objects
        .filter(pk__in=[a.get('inscription') for a in affectations])
        .select_related('classe', 'sous_groupe', 'etudiant')
    }
    groupes = {
        g.pk: g for g in SousGroupeTP.objects
        .filter(pk__in=[a.get('sous_groupe') for a in affectations
                        if a.get('sous_groupe')])
        .select_related('classe')
    }

    touchees = []
    for affectation in affectations:
        inscription = inscriptions.get(affectation.get('inscription'))
        if inscription is None:
            raise ValidationError(
                f"Inscription introuvable : {affectation.get('inscription')}.")

        cible_id = affectation.get('sous_groupe')
        cible = groupes.get(cible_id) if cible_id else None
        if cible_id and cible is None:
            raise ValidationError(f'Sous-groupe introuvable : {cible_id}.')
        # Un sous-groupe appartient à UNE classe : l'y placer un élève d'une
        # autre le rendrait invisible de sa propre liste d'appel.
        if cible is not None and cible.classe_id != inscription.classe_id:
            raise ValidationError(
                f'{inscription.etudiant} est en {inscription.classe.nom} ; '
                f'{cible.groupe} appartient à {cible.classe.nom}.')

        if inscription.sous_groupe_id == (cible.pk if cible else None):
            continue
        inscription.sous_groupe = cible
        # `save()` complet, comme `_deplacer` : c'est lui qui fait suivre le
        # département de l'étudiant, dont dépendent les listes d'appel.
        inscription.save()
        touchees.append({'etudiant': inscription.etudiant.matricule,
                         'vers': cible.groupe if cible else None})

    return {'affectees': len(touchees), 'mouvements': touchees}
