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
    NIVEAU_MP, NIVEAU_MPSI, SEMESTRES_PAR_NIVEAU,
    AbsenceSeance, Classe, Deliberation, DetailLigneDeliberation, Evaluation,
    GrilleType, HistoriqueClasse, InscriptionIPGEI, LigneDeliberation, Matiere,
    Note, ParametresIPGEI, PermutationEtudiant, PermutationProf, SeanceReelle,
    SemaineIPGEI, SemestreIPGEI, StatutWorkflow,
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
    """
    matieres = Matiere.objects.filter(
        institution=semestre.institution, code_semestre=semestre.code, actif=True,
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


def moyenne_semestrielle(inscription: InscriptionIPGEI, semestre: SemestreIPGEI):
    """
    Moyenne pondérée par les coefficients sur un semestre.
    Renvoie (moyenne, total_coefficients, [notes]).
    """
    notes = list(
        Note.objects.filter(inscription=inscription, semestre=semestre)
        .select_related('matiere')
    )
    return _agreger(notes)


def _agreger(notes):
    """Agrège des lignes de note en moyenne pondérée par coefficient."""
    cumul  = Decimal('0')
    poids  = Decimal('0')
    for n in notes:
        if n.note_retenue is None:
            continue
        # Coefficient snapshoté s'il existe, sinon celui de la matière : une
        # note jamais calculée ne doit pas faire disparaître la matière du total.
        coef = Decimal(n.coefficient if n.coefficient is not None else n.matiere.coefficient)
        cumul += Decimal(n.note_retenue) * coef
        poids += coef
    if poids == 0:
        return None, Decimal('0'), notes
    return _arrondi(cumul / poids), poids, notes


def moyenne_annuelle(inscription: InscriptionIPGEI, annee_universitaire: str):
    """
    Moyenne annuelle : tous les semestres du niveau de l'étudiant pour l'année.
    Les semestres pèsent au prorata des coefficients de leurs matières, ce qui
    revient à une moyenne pondérée unique sur l'ensemble des matières.
    """
    codes = SEMESTRES_PAR_NIVEAU[inscription.niveau]
    notes = list(
        Note.objects.filter(
            inscription=inscription,
            semestre__annee_universitaire=annee_universitaire,
            semestre__code__in=codes,
        ).select_related('matiere', 'semestre')
    )
    return _agreger(notes)


# ═════════════════════════════════════════════════════════════════════════════
# Délibération
# ═════════════════════════════════════════════════════════════════════════════
def decision_automatique(niveau: str, moyenne, seuil, nb_redoublements: int,
                         droit_redoublement_max: int) -> str:
    """
    Décision proposée par le calcul (plan §4.4).

    MPSI (1re année) : admis si moyenne ≥ seuil, sinon réorienté — le
    redoublement n'existe pas en 1re année.
    MP (2e année)    : autorisé CNIM si moyenne ≥ seuil ; sinon redoublant, et
    exclu une fois le droit de redoublement épuisé.
    """
    if moyenne is None:
        return ''
    atteint = Decimal(moyenne) >= Decimal(seuil)
    if niveau == NIVEAU_MPSI:
        return LigneDeliberation.DECISION_ADMIS if atteint else LigneDeliberation.DECISION_REORIENTE
    if atteint:
        return LigneDeliberation.DECISION_CNIM
    if nb_redoublements >= droit_redoublement_max:
        return LigneDeliberation.DECISION_EXCLU
    return LigneDeliberation.DECISION_REDOUBLANT


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

    inscriptions = (
        InscriptionIPGEI.objects
        .filter(
            annee_universitaire=deliberation.annee_universitaire,
            classe__niveau__niveau=deliberation.niveau,
            classe__institution=deliberation.institution,
            actif=True,
        )
        .select_related('etudiant', 'classe')
    )

    resultats = []
    for inscription in inscriptions:
        if deliberation.portee == Deliberation.PORTEE_SEMESTRE:
            moyenne, poids, notes = moyenne_semestrielle(inscription, deliberation.semestre)
        else:
            moyenne, poids, notes = moyenne_annuelle(
                inscription, deliberation.annee_universitaire,
            )
        resultats.append((inscription, moyenne, poids, notes))

    # Rang : les étudiants sans moyenne restent non classés plutôt que derniers.
    classables = sorted(
        [r for r in resultats if r[1] is not None], key=lambda r: r[1], reverse=True,
    )
    rangs = {r[0].id: i + 1 for i, r in enumerate(classables)}

    for inscription, moyenne, poids, notes in resultats:
        auto = decision_automatique(
            inscription.niveau, moyenne, deliberation.seuil_validation,
            inscription.nb_redoublements, droit_max,
        )
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
        # arbitré, ou si la ligne vient d'être créée.
        if cree or not ligne.decision:
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
        inscription.statut = statut_par_decision.get(ligne.decision, inscription.statut)
        champs = ['statut']
        if ligne.decision == LigneDeliberation.DECISION_REDOUBLANT:
            inscription.nb_redoublements += 1
            champs.append('nb_redoublements')
        inscription.save(update_fields=champs)

    notes = Note.objects.filter(inscription__in=[l.inscription_id for l in lignes])
    if deliberation.portee == Deliberation.PORTEE_SEMESTRE:
        notes = notes.filter(semestre=deliberation.semestre)
    else:
        notes = notes.filter(
            semestre__annee_universitaire=deliberation.annee_universitaire,
            semestre__code__in=SEMESTRES_PAR_NIVEAU[deliberation.niveau],
        )
    notes.update(verrouillee=True)

    deliberation.statut          = Deliberation.STATUT_VALIDEE
    deliberation.validee_par     = utilisateur if getattr(utilisateur, 'is_authenticated', False) else None
    deliberation.date_validation = timezone.now()
    deliberation.save(update_fields=['statut', 'validee_par', 'date_validation'])
    return deliberation


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
def appliquer_permutation_etudiant(permutation: PermutationEtudiant, utilisateur=None) -> InscriptionIPGEI:
    """
    Déplace l'inscription vers la classe cible et journalise le mouvement.

    Rien n'est copié : notes, absences et lignes de délibération pointent sur
    l'inscription, donc l'historique reste intact et suit l'étudiant.
    """
    if permutation.statut == StatutWorkflow.APPLIQUEE:
        raise ValidationError('Permutation déjà appliquée.')
    if permutation.statut != StatutWorkflow.VALIDEE:
        raise ValidationError('La permutation doit être validée par le directeur avant application.')

    inscription = permutation.inscription
    classe_avant = inscription.classe

    HistoriqueClasse.objects.create(
        inscription=inscription,
        classe_avant=classe_avant,
        classe_apres=permutation.classe_cible,
        motif=permutation.motif,
        decide_par=utilisateur if getattr(utilisateur, 'is_authenticated', False) else None,
    )

    inscription.classe = permutation.classe_cible
    # Le sous-groupe appartient à l'ancienne classe : on le remplace par celui
    # choisi dans la cible, ou on le vide pour éviter un rattachement incohérent.
    inscription.sous_groupe = permutation.sous_groupe_cible
    inscription.save(update_fields=['classe', 'sous_groupe'])

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


def bilan_absences(inscription: InscriptionIPGEI, semestre: SemestreIPGEI = None) -> dict:
    """Compte des absences d'un étudiant, éventuellement bornées à un semestre."""
    qs = AbsenceSeance.objects.filter(inscription=inscription)
    if semestre is not None:
        qs = qs.filter(seance__semaine__semestre=semestre)
    return {
        'absences':   qs.filter(statut=AbsenceSeance.STATUT_ABSENT).count(),
        'retards':    qs.filter(statut=AbsenceSeance.STATUT_RETARD).count(),
        'justifiees': qs.filter(statut=AbsenceSeance.STATUT_JUSTIFIEE).count(),
        'total':      qs.count(),
    }


# ═════════════════════════════════════════════════════════════════════════════
# Relevés (support des documents officiels)
# ═════════════════════════════════════════════════════════════════════════════
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
def semestres_du_niveau(niveau: str, annee_universitaire: str):
    """Les deux semestres d'une année d'étude : S1/S2 en MPSI, S3/S4 en MP."""
    return SemestreIPGEI.objects.filter(
        code__in=SEMESTRES_PAR_NIVEAU[niveau],
        annee_universitaire=annee_universitaire,
    ).order_by('code')


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
    ):
        total += initialiser_notes(inscription, semestre)
    return total


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
    if classe.niveau_code == NIVEAU_MPSI:
        raise ValidationError(
            f"Le redoublement n'est pas autorisé en 1re année. "
            f'{etudiant} a déjà suivi {precedente.classe.niveau_code} en '
            f'{precedente.annee_universitaire}.'
        )

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
            f'redoublement(s) pour un maximum de {droit_max}.'
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
        if inscription.classe.niveau_code == semestre.niveau:
            total += initialiser_notes(inscription, semestre)
    return total
