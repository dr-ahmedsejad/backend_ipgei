"""
Règles métier IPGEI — calcul des notes, délibération, EDT et permutations.

Toute la logique vit ici plutôt que dans les vues : les mêmes règles servent
l'API, l'admin Django, les commandes de gestion et la génération de documents.
"""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from .models import (
    NIVEAU_MP, NIVEAU_MPSI, SEMESTRES_PAR_NIVEAU, SEMESTRES_PAR_RANG,
    AbsenceSeance, AnonymatIPGEI, Classe, Deliberation, DetailLigneDeliberation,
    Evaluation,
    GrilleType, HistoriqueClasse, InscriptionIPGEI, LigneDeliberation, Matiere,
    MembreJuryIPGEI,
    Note, ParametresIPGEI, PermutationEtudiant, PermutationProf, SeanceReelle,
    SemaineIPGEI, SemestreIPGEI, SessionEvaluationIPGEI, StatutWorkflow,
)

CENT = Decimal('100')
DEUX_DECIMALES = Decimal('0.01')


def _arrondi(valeur):
    """Arrondit une note à 2 décimales (demi vers le haut), en tolérant None."""
    if valeur is None:
        return None
    return Decimal(valeur).quantize(DEUX_DECIMALES, rounding=ROUND_HALF_UP)


def _moyenne_arithmetique(valeurs):
    """Moyenne arithmétique simple ; None si aucune valeur (plan §4.3)."""
    valeurs = [Decimal(v) for v in valeurs if v is not None]
    if not valeurs:
        return None
    return _arrondi(sum(valeurs) / Decimal(len(valeurs)))


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
def calculer_note(note: Note, *, plafond_rattrapage=None, sauvegarder=True) -> Note:
    """
    Recalcule une ligne de note et **fige la pondération** de la matière dedans.

    moy_DS   = moyenne arithmétique des DS
    moy_exam = moyenne arithmétique des examens
    moyenne  = pct_ds·moy_DS + pct_tp·TP + pct_exam·moy_exam

    Une composante sans note (aucun DS saisi, pas de TP…) est neutralisée : sa
    part est redistribuée sur les composantes présentes, faute de quoi un
    semestre en cours afficherait des moyennes artificiellement basses.

    La note retenue applique ensuite la règle SIGA `max(moyenne, rattrapage)`,
    bornée par le plafond éventuel.
    """
    if note.verrouillee:
        raise ValidationError('Note verrouillée par une délibération validée — recalcul refusé.')

    evaluations = list(note.evaluations.all())
    moy_ds   = _moyenne_arithmetique(
        [e.valeur for e in evaluations if e.type_evaluation == Evaluation.TYPE_DS]
    )
    moy_exam = _moyenne_arithmetique(
        [e.valeur for e in evaluations if e.type_evaluation == Evaluation.TYPE_EXAM]
    )

    matiere = note.matiere
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
            'pct_ds', 'pct_tp', 'pct_exam', 'coefficient',
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


def recalculer_notes(queryset, *, plafond_rattrapage=None) -> int:
    """Recalcule un lot de notes ; renvoie le nombre de lignes traitées."""
    total = 0
    qs = queryset.filter(verrouillee=False).select_related('matiere').prefetch_related('evaluations')
    for note in qs:
        calculer_note(note, plafond_rattrapage=plafond_rattrapage)
        total += 1
    return total


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


def moyenne_annuelle(inscription: InscriptionIPGEI, annee_universitaire: str,
                     *, absents_a_zero: bool = False):
    """
    Moyenne annuelle : tous les semestres du niveau de l'étudiant pour l'année.
    Les semestres pèsent au prorata des coefficients de leurs matières, ce qui
    revient à une moyenne pondérée unique sur l'ensemble des matières.

    `absents_a_zero` : voir `moyenne_semestrielle`. C'est ce qui permet au jury
    de statuer sur un étudiant dont un semestre entier manque — il pèse alors
    zéro, et la décision se prononce là-dessus plutôt que de rester en blanc.
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
    return _agreger(
        notes, absents_a_zero=absents_a_zero,
        maquette=(maquette_de_lannee(inscription, annee_universitaire)
                  if absents_a_zero else None),
    )


# ═════════════════════════════════════════════════════════════════════════════
# Délibération
# ═════════════════════════════════════════════════════════════════════════════
def niveau_ref(code: str, institution=None):
    """
    Fiche du référentiel correspondant à un code de niveau, ou None.

    Le code reste la clé d'usage : classes, semestres et délibérations le
    portent en texte. Cette recherche est le pont vers les règles associées.
    """
    from .models import NiveauIPGEI

    requete = NiveauIPGEI.objects.filter(code=code)
    if institution is not None:
        requete = requete.filter(institution=institution)
    return requete.first()


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

    Première année : admis si la moyenne atteint le seuil, sinon réorienté — le
    redoublement n'y existe pas.
    Deuxième année : autorisé CNIM si le seuil est atteint ; sinon redoublant,
    et exclu une fois le droit de redoublement épuisé.

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

    redoublement_ouvert = (fiche.redoublement_autorise if fiche is not None else rang > 1)
    if not redoublement_ouvert or nb_redoublements >= droit_redoublement_max:
        return retenue(LigneDeliberation.DECISION_EXCLU)
    return retenue(LigneDeliberation.DECISION_REDOUBLANT)


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
    if deliberation.classe_id:
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

    for inscription, moyenne, poids, notes in resultats:
        if semestrielle:
            auto = ''
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
    """
    if deliberation.est_verrouillee:
        raise ValidationError('Délibération déjà validée.')
    if deliberation.statut != Deliberation.STATUT_CALCULEE:
        raise ValidationError('Calculer la délibération avant de la valider.')

    lignes = deliberation.lignes.select_related('inscription').all()
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
        LigneDeliberation.DECISION_EXCLU:      InscriptionIPGEI.STATUT_ABANDON,
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
        return 0

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
def generer_semaines(semestre: SemestreIPGEI, *, nb=None, remplacer=False) -> int:
    """
    Reprend le calendrier du socle pour ce semestre.

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

    connues = set(semestre.semaines.values_list('numero', flat=True))
    creees = [
        SemaineIPGEI(
            semestre=semestre,
            numero=g['numero_semaine'],
            date_debut=g['debut'],
            date_fin=g['fin'],
            # Le socle connaît les mêmes qualifications, à « férié » près, qui
            # n'existe pas côté IPGEI : on le range en vacances, faute de quoi
            # un jour chômé se retrouverait compté comme semaine de cours.
            type_semaine=_TYPE_SEMAINE_SOCLE.get(
                g['type_semaine'], SemaineIPGEI.TYPE_COURS),
        )
        for g in groupes if g['numero_semaine'] not in connues
    ]
    SemaineIPGEI.objects.bulk_create(creees)
    return len(creees)


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
    portee = ('annuels' if deliberation.portee == deliberation.PORTEE_ANNUELLE
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
    lignes = (
        Note.objects
        .filter(semestre=semestre,
                inscription__classe_id=inscription.classe_id,
                inscription__actif=True,
                note_retenue__isnull=False)
        .values_list('inscription_id', 'note_retenue', 'coefficient',
                     'matiere__coefficient')
    )

    cumuls = {}
    for inscription_id, note, coef_fige, coef_matiere in lignes:
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


def releve_semestre(inscription: InscriptionIPGEI, semestre: SemestreIPGEI) -> dict:
    """Données d'un relevé de semestre — consommées par l'API et le PDF."""
    moyenne, poids, notes = moyenne_semestrielle(inscription, semestre)
    params = ParametresIPGEI.pour(semestre.institution)
    return {
        'inscription': inscription,
        'semestre':    semestre,
        'notes':       sorted(notes, key=lambda n: (n.matiere.ordre, n.matiere.code)),
        'moyenne':     moyenne,
        'total_coefficients': poids,
        'seuil':       params.seuil_validation,
        'valide':      moyenne is not None and moyenne >= params.seuil_validation,
        'absences':    bilan_absences(inscription, semestre),
        'stats_classe': statistiques_classe(inscription, semestre),
        'rang_general': rang_general_classe(inscription, semestre),
    }


def releve_annuel(inscription: InscriptionIPGEI, annee_universitaire: str) -> dict:
    """Données d'un relevé annuel, regroupées par semestre."""
    moyenne, poids, notes = moyenne_annuelle(inscription, annee_universitaire)
    params = ParametresIPGEI.pour(inscription.classe.institution)
    par_semestre = {}
    for n in notes:
        par_semestre.setdefault(n.semestre.code, []).append(n)
    semestres = [
        {
            'code':    code,
            'notes':   sorted(lignes, key=lambda n: (n.matiere.ordre, n.matiere.code)),
            'moyenne': _agreger(lignes)[0],
        }
        for code, lignes in sorted(par_semestre.items())
    ]
    return {
        'inscription':         inscription,
        'annee_universitaire': annee_universitaire,
        'semestres':           semestres,
        'moyenne':             moyenne,
        'total_coefficients':  poids,
        'seuil':               params.seuil_validation,
        'valide':              moyenne is not None and moyenne >= params.seuil_validation,
        'absences':            bilan_absences(inscription),
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
    """
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


def candidats_passage(deliberation: Deliberation) -> list:
    """
    Qui repart l'an prochain, et vers quel niveau.

    Lit les LIGNES du jury et non le statut des inscriptions : c'est la décision
    prononcée qui fait foi, et elle est là même si le report sur l'inscription
    a été défait depuis. Chaque entrée dit l'inscription, la suite qui lui est
    faite et le code de niveau visé.
    """
    institution = deliberation.institution
    rang_actuel = rang_du_niveau(deliberation.niveau, institution)

    sortie = []
    for ligne in deliberation.lignes.select_related('inscription__etudiant',
                                                    'inscription__classe'):
        suite = SUITE_PAR_STATUT.get(
            {
                LigneDeliberation.DECISION_ADMIS:      InscriptionIPGEI.STATUT_ADMIS,
                LigneDeliberation.DECISION_REDOUBLANT: InscriptionIPGEI.STATUT_REDOUBLANT,
            }.get(ligne.decision)
        )
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

    institution = deliberation.institution
    cible = annee_cible or annee_suivante(deliberation.annee_universitaire)

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
    }
