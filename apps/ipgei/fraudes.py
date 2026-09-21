"""
Sanctions pour fraude : mettre des notes à zéro, sur pièce, et pouvoir revenir.

Trois règles tiennent tout le module.

1. ON NE SANCTIONNE QUE CE QUI A EU LIEU. Une épreuve est sanctionnable si la
   CLASSE l'a passée : le diviseur d'une moyenne est le nombre d'épreuves tenues
   par la classe (`services.epreuves_tenues`). Créer un « DS 3 » pour un seul
   étudiant ferait passer toute sa classe de deux à trois devoirs.

2. LA SANCTION PASSE LE VERROU DU JURY, ET LE DIT. Une fraude se découvre
   souvent après la délibération ; c'est alors une note arrêtée qu'il faut
   mettre à zéro. Le jury concerné n'est pas rouvert d'office — il est signalé
   à redélibérer (`jurys_a_redeliberer`), c'est à lui de reprendre sa décision.

3. LA NOTE SANCTIONNÉE NE SE RESAISIT PAS. Tant que la fiche est appliquée,
   aucun chemin de saisie (grille, anonymat, import, rattrapage, API unitaire)
   ne peut y remettre autre chose que zéro : sans quoi le réimport d'un
   classeur d'enseignant effacerait la sanction sans que personne le voie.
   La sanction se LÈVE, avec un motif, et les valeurs d'origine reviennent.

Enseignement partagé (`Matiere.partage_avec`) : les DS et le TP d'une matière
rattachée sont ceux de sa porteuse. Ils se sanctionnent donc sur la porteuse,
et la rattachée suit — la rattachée n'offre que ses examens et son rattrapage.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    Deliberation, ElementSanctionne, Evaluation, Matiere, Note, SanctionFraude,
)

ZERO = Decimal('0')

LIBELLE_COMPOSANTE = dict(ElementSanctionne.COMPOSANTE_CHOICES)


# ── Ce qui peut être sanctionné ──────────────────────────────────────────────
def _numeros_tenus(matiere_id, semestre_id, classe_id, type_evaluation) -> list[int]:
    """Numéros des épreuves d'un type que la classe a passées, dans l'ordre."""
    return sorted(set(
        Evaluation.objects
        .filter(note__matiere_id=matiere_id, note__semestre_id=semestre_id,
                note__inscription__classe_id=classe_id,
                type_evaluation=type_evaluation)
        .values_list('numero', flat=True)
    ))


def _tp_tenu(note: Note) -> bool:
    """La classe a-t-elle un TP noté dans cette matière, et la règle le compte-t-elle ?"""
    from . import services

    regle = services.ponderation_applicable(note.matiere, note.inscription.classe)
    if not regle.has_tp:
        return False
    return Note.objects.filter(
        matiere_id=note.matiere_id, semestre_id=note.semestre_id,
        inscription__classe_id=note.inscription.classe_id,
        note_tp__isnull=False,
    ).exists()


def epreuves_sanctionnables(note: Note) -> list[dict]:
    """
    Les épreuves de cette ligne qu'une sanction peut viser, une par entrée.

    `{'composante', 'numero', 'libelle', 'valeur'}` — `valeur` est la note
    actuelle de l'étudiant (None s'il n'a pas rendu la copie : elle compte déjà
    zéro, mais la sanction l'écrit pour qu'elle se voie et se garde).
    """
    partagee = note.matiere.partage_avec_id is not None
    classe_id = note.inscription.classe_id
    valeurs = {(e.type_evaluation, e.numero): e.valeur for e in note.evaluations.all()}
    sortie = []

    if not partagee:
        for numero in _numeros_tenus(note.matiere_id, note.semestre_id, classe_id,
                                     Evaluation.TYPE_DS):
            sortie.append({'composante': ElementSanctionne.COMPOSANTE_DS, 'numero': numero,
                           'valeur': valeurs.get((Evaluation.TYPE_DS, numero))})
    for numero in _numeros_tenus(note.matiere_id, note.semestre_id, classe_id,
                                 Evaluation.TYPE_EXAM):
        sortie.append({'composante': ElementSanctionne.COMPOSANTE_EXAM, 'numero': numero,
                       'valeur': valeurs.get((Evaluation.TYPE_EXAM, numero))})
    if not partagee and _tp_tenu(note):
        sortie.append({'composante': ElementSanctionne.COMPOSANTE_TP, 'numero': None,
                       'valeur': note.note_tp})
    # Le rattrapage est individuel : il n'existe que si l'étudiant l'a composé.
    if note.note_rattrapage is not None:
        sortie.append({'composante': ElementSanctionne.COMPOSANTE_RATTRAPAGE,
                       'numero': None, 'valeur': note.note_rattrapage})

    for e in sortie:
        rang = f" {e['numero']}" if e['numero'] else ''
        e['libelle'] = f"{LIBELLE_COMPOSANTE[e['composante']]}{rang}"
    return sortie


# ── Qui est déjà sanctionné ──────────────────────────────────────────────────
def elements_actifs(note_ids) -> dict[int, list[ElementSanctionne]]:
    """Éléments des sanctions APPLIQUÉES, groupés par ligne de note."""
    sortie: dict[int, list[ElementSanctionne]] = {}
    for el in (ElementSanctionne.objects
               .filter(note_id__in=list(note_ids),
                       sanction__statut=SanctionFraude.STATUT_APPLIQUEE)
               .select_related('sanction')):
        sortie.setdefault(el.note_id, []).append(el)
    return sortie


def _couvre(element: ElementSanctionne, composante: str, numero) -> bool:
    if element.portee == ElementSanctionne.PORTEE_MATIERE:
        return True
    if element.composante != composante:
        return False
    return element.numero is None or numero is None or element.numero == numero


def controler_saisie_sanctionnee(note: Note, composante: str, numero=None,
                                 valeur=None, *, elements=None) -> None:
    """
    Refuse d'écrire sur une note sanctionnée autre chose que zéro.

    Resoumettre zéro est admis : la grille renvoie toutes ses cases, celle de la
    sanction comprise. Effacer (valeur None) est refusé — la case redeviendrait
    « non rendue » et la sanction ne se verrait plus.

    `elements` évite une requête par ligne quand l'appelant a déjà chargé
    `elements_actifs` pour tout un lot.
    """
    if note is None or note.pk is None:
        return
    if elements is None:
        elements = elements_actifs([note.pk]).get(note.pk, [])
    for el in elements:
        if not _couvre(el, composante, numero):
            continue
        if valeur is not None and Decimal(valeur) == ZERO:
            return
        rang = f' {numero}' if numero else ''
        raise ValidationError(
            f'{note.inscription.etudiant.matricule} — {note.matiere.code}, '
            f'{LIBELLE_COMPOSANTE.get(composante, composante)}{rang} : note mise à '
            f'zéro par la sanction pour fraude n° {el.sanction_id}. Elle ne se '
            'modifie pas ; la sanction se lève depuis l’écran « Fraudes ».'
        )


def controler_evaluation(note: Note, type_evaluation: str, numero, valeur, *, elements=None):
    """Raccourci pour un DS ou un examen."""
    composante = (ElementSanctionne.COMPOSANTE_DS if type_evaluation == Evaluation.TYPE_DS
                  else ElementSanctionne.COMPOSANTE_EXAM)
    controler_saisie_sanctionnee(note, composante, numero, valeur, elements=elements)


# ── Recalcul ─────────────────────────────────────────────────────────────────
def _plafond(note: Note):
    """Le plafond qu'avait retenu le dernier calcul, sinon celui de la session."""
    from . import services

    if note.plafond_rattrapage_applique is not None:
        return note.plafond_rattrapage_applique
    return services.plafond_effectif(note.semestre)


def _recalculer(note: Note) -> None:
    """
    La ligne, puis les lignes de l'étudiant qui lisent ses DS et son TP.

    Pas le groupe entier : une sanction n'ajoute aucune épreuve à la classe, le
    diviseur de ses camarades ne bouge pas — les recalculer réécrirait des
    notes arrêtées par le jury sans aucune raison.
    """
    from . import services

    note = (Note.objects.select_related('matiere', 'inscription', 'semestre')
            .prefetch_related('evaluations').get(pk=note.pk))
    services.calculer_note(note, plafond_rattrapage=_plafond(note), malgre_verrou=True)
    rattachees = Matiere.objects.filter(partage_avec_id=note.matiere_id).values_list('pk', flat=True)
    for dependante in (Note.objects
                       .filter(inscription_id=note.inscription_id, semestre_id=note.semestre_id,
                               matiere_id__in=list(rattachees))
                       .select_related('matiere', 'inscription', 'semestre')
                       .prefetch_related('evaluations')):
        services.calculer_note(dependante, plafond_rattrapage=_plafond(dependante),
                               malgre_verrou=True)


# ── Prononcer ────────────────────────────────────────────────────────────────
def _decimal(v):
    return None if v is None else str(v)


def _mettre_a_zero(note: Note, cibles: list[dict], utilisateur) -> dict:
    """Écrit les zéros ; rend ce qu'ils remplacent, pour la levée."""
    origine: dict = {'evaluations': []}
    existantes = {(e.type_evaluation, e.numero): e for e in note.evaluations.all()}
    champs = []
    for c in cibles:
        if c['composante'] in (ElementSanctionne.COMPOSANTE_DS, ElementSanctionne.COMPOSANTE_EXAM):
            type_ev = (Evaluation.TYPE_DS if c['composante'] == ElementSanctionne.COMPOSANTE_DS
                       else Evaluation.TYPE_EXAM)
            ev = existantes.get((type_ev, c['numero']))
            origine['evaluations'].append({
                'type_evaluation': type_ev, 'numero': c['numero'],
                'valeur': _decimal(ev.valeur) if ev else None,
                'creee': ev is None,
            })
            if ev is None:
                Evaluation.objects.create(note=note, type_evaluation=type_ev,
                                          numero=c['numero'], valeur=ZERO,
                                          saisie_par=utilisateur)
            elif ev.valeur != ZERO:
                ev.valeur = ZERO
                ev.save(update_fields=['valeur'])
        elif c['composante'] == ElementSanctionne.COMPOSANTE_TP:
            origine['note_tp'] = _decimal(note.note_tp)
            note.note_tp = ZERO
            champs.append('note_tp')
        elif c['composante'] == ElementSanctionne.COMPOSANTE_RATTRAPAGE:
            origine['note_rattrapage'] = _decimal(note.note_rattrapage)
            note.note_rattrapage = ZERO
            champs.append('note_rattrapage')
    if champs:
        note.save(update_fields=champs + ['date_modification'])
    return origine


@transaction.atomic
def sanctionner(*, inscription, nature: str, date_faits, motif: str, pv_deliberation,
                elements: list[dict], utilisateur=None) -> SanctionFraude:
    """
    Prononce une sanction et met les notes désignées à zéro.

    `elements` : `[{'note': id, 'portee': 'matiere'|'epreuve',
    'composante': 'ds'|'exam'|'tp'|'rattrapage', 'numero': n}]` — composante et
    numéro seulement pour une épreuve.

    Tout ou rien : un seul élément invalide et rien n'est écrit.
    """
    motif = (motif or '').strip()
    if not motif:
        raise ValidationError({'motif': 'Le motif est obligatoire.'})
    if not pv_deliberation:
        raise ValidationError({'pv_deliberation': 'Le PV de délibération est obligatoire.'})
    if nature not in dict(SanctionFraude.NATURE_CHOICES):
        raise ValidationError({'nature': 'Nature de la fraude inconnue.'})
    if date_faits is None:
        raise ValidationError({'date_faits': 'La date des faits est obligatoire.'})
    if date_faits > timezone.localdate():
        raise ValidationError({'date_faits': 'La date des faits ne peut pas être future.'})
    if not elements:
        raise ValidationError({'elements': 'Désignez au moins une matière ou une épreuve.'})

    note_ids = []
    for brut in elements:
        try:
            note_ids.append(int(brut.get('note')))
        except (TypeError, ValueError):
            raise ValidationError({'elements': 'Chaque élément doit désigner une note.'})
    notes = {n.pk: n for n in (Note.objects
                               .select_for_update(of=('self',))
                               .filter(pk__in=note_ids)
                               .select_related('matiere', 'semestre', 'inscription__classe',
                                               'inscription__etudiant')
                               .prefetch_related('evaluations'))}
    deja = elements_actifs(note_ids)

    # Validation complète AVANT la moindre écriture.
    plan: dict[int, dict] = {}
    for brut in elements:
        note = notes.get(int(brut['note']))
        if note is None or note.inscription_id != inscription.pk:
            raise ValidationError({'elements': "Une note désignée n'appartient pas à cet étudiant."})
        if note.pk in deja:
            raise ValidationError({'elements': (
                f'{note.matiere.code} : déjà visée par la sanction n° '
                f'{deja[note.pk][0].sanction_id}. Levez-la avant d’en prononcer une autre.')})
        portee = brut.get('portee')
        possibles = epreuves_sanctionnables(note)
        entree = plan.setdefault(note.pk, {'note': note, 'portee': None, 'cibles': []})
        if portee == ElementSanctionne.PORTEE_MATIERE:
            if entree['portee'] is not None:
                raise ValidationError({'elements': f'{note.matiere.code} est désignée deux fois.'})
            if not possibles:
                raise ValidationError({'elements': (
                    f'{note.matiere.code} : aucune épreuve tenue, rien à mettre à zéro.')})
            entree['portee'] = portee
            entree['cibles'] = possibles
        elif portee == ElementSanctionne.PORTEE_EPREUVE:
            if entree['portee'] == ElementSanctionne.PORTEE_MATIERE:
                raise ValidationError({'elements': f'{note.matiere.code} est désignée deux fois.'})
            entree['portee'] = portee
            composante = brut.get('composante')
            numero = brut.get('numero') or None
            numero = int(numero) if numero is not None else None
            cible = next((p for p in possibles
                          if p['composante'] == composante and p['numero'] == numero), None)
            if cible is None:
                raise ValidationError({'elements': (
                    f"{note.matiere.code} : « {LIBELLE_COMPOSANTE.get(composante, composante)}"
                    f"{' ' + str(numero) if numero else ''} » n’a pas été passée par la classe "
                    '— elle ne peut pas être sanctionnée.')})
            if cible in entree['cibles']:
                raise ValidationError({'elements': f"{note.matiere.code} : {cible['libelle']} désignée deux fois."})
            entree['cibles'].append(cible)
        else:
            raise ValidationError({'elements': 'Portée inconnue : « matiere » ou « epreuve ».'})

    sanction = SanctionFraude.objects.create(
        inscription=inscription, nature=nature, date_faits=date_faits, motif=motif,
        pv_deliberation=pv_deliberation, cree_par=utilisateur,
    )
    for entree in plan.values():
        note = entree['note']
        avant = note.note_retenue
        cibles = entree['cibles']
        origine = _mettre_a_zero(note, cibles, utilisateur)
        if entree['portee'] == ElementSanctionne.PORTEE_MATIERE:
            ElementSanctionne.objects.create(
                sanction=sanction, note=note, portee=ElementSanctionne.PORTEE_MATIERE,
                valeurs_origine=origine, note_retenue_avant=avant)
        else:
            for c in cibles:
                ElementSanctionne.objects.create(
                    sanction=sanction, note=note, portee=ElementSanctionne.PORTEE_EPREUVE,
                    composante=c['composante'], numero=c['numero'],
                    valeurs_origine=_origine_de(origine, c), note_retenue_avant=avant)
        _recalculer(note)

    _auditer(sanction, 'CREATE', 'Sanction pour fraude prononcée')
    return sanction


def _origine_de(origine: dict, cible: dict) -> dict:
    """La part des valeurs d'origine qui revient à une seule épreuve."""
    if cible['composante'] == ElementSanctionne.COMPOSANTE_TP:
        return {'note_tp': origine.get('note_tp')}
    if cible['composante'] == ElementSanctionne.COMPOSANTE_RATTRAPAGE:
        return {'note_rattrapage': origine.get('note_rattrapage')}
    type_ev = (Evaluation.TYPE_DS if cible['composante'] == ElementSanctionne.COMPOSANTE_DS
               else Evaluation.TYPE_EXAM)
    return {'evaluations': [e for e in origine['evaluations']
                            if e['type_evaluation'] == type_ev and e['numero'] == cible['numero']]}


# ── Lever ────────────────────────────────────────────────────────────────────
def _restaurer(note: Note, origine: dict) -> None:
    for e in origine.get('evaluations', []):
        cle = {'note': note, 'type_evaluation': e['type_evaluation'], 'numero': e['numero']}
        if e.get('creee'):
            Evaluation.objects.filter(**cle).delete()
        else:
            Evaluation.objects.filter(**cle).update(valeur=Decimal(e['valeur']))
    champs = []
    for champ in ('note_tp', 'note_rattrapage'):
        if champ in origine:
            v = origine[champ]
            setattr(note, champ, None if v is None else Decimal(v))
            champs.append(champ)
    if champs:
        note.save(update_fields=champs + ['date_modification'])


@transaction.atomic
def lever(sanction: SanctionFraude, *, motif_levee: str, utilisateur=None) -> dict:
    """
    Lève la sanction : les notes d'origine reviennent, la fiche reste.

    Rend `{'sanction', 'ecarts'}` — `ecarts` liste les lignes dont la note
    retenue recalculée diffère de celle d'avant la sanction (pondération ou
    plafond modifiés entre-temps) : la restauration des SAISIES est exacte, le
    recalcul suit les règles du jour, et il faut le dire.
    """
    sanction = SanctionFraude.objects.select_for_update().get(pk=sanction.pk)
    if not sanction.est_active:
        raise ValidationError('Cette sanction est déjà levée.')
    motif_levee = (motif_levee or '').strip()
    if not motif_levee:
        raise ValidationError({'motif_levee': 'Le motif de la levée est obligatoire.'})

    elements = list(sanction.elements.select_related('note__matiere', 'note__semestre',
                                                     'note__inscription'))
    notes = {}
    for el in elements:
        _restaurer(el.note, el.valeurs_origine)
        notes.setdefault(el.note_id, (el.note, el.note_retenue_avant))

    sanction.statut = SanctionFraude.STATUT_LEVEE
    sanction.motif_levee = motif_levee
    sanction.levee_par = utilisateur
    sanction.levee_le = timezone.now()
    sanction.save(update_fields=['statut', 'motif_levee', 'levee_par', 'levee_le'])

    ecarts = []
    for note, avant in notes.values():
        _recalculer(note)
        apres = Note.objects.values_list('note_retenue', flat=True).get(pk=note.pk)
        if apres != avant:
            ecarts.append({'matiere': note.matiere.code, 'avant': _decimal(avant),
                           'apres': _decimal(apres)})

    _auditer(sanction, 'UPDATE', 'Sanction pour fraude levée')
    return {'sanction': sanction, 'ecarts': ecarts}


# ── Les jurys à reprendre ────────────────────────────────────────────────────
def jurys_a_redeliberer(sanction: SanctionFraude) -> list[Deliberation]:
    """
    Jurys validés AVANT la dernière action sur la fiche et qui couvrent ses notes.

    Un jury revalidé après la sanction (ou sa levée) l'a vue : il sort de la liste.
    """
    from . import services

    note_ids = set(sanction.elements.values_list('note_id', flat=True))
    candidats = (Deliberation.objects
                 .filter(statut=Deliberation.STATUT_VALIDEE,
                         lignes__inscription_id=sanction.inscription_id,
                         date_validation__lt=sanction.date_derniere_action)
                 .distinct().order_by('date_validation'))
    return [d for d in candidats
            if services.notes_couvertes(d).filter(pk__in=note_ids).exists()]


def sanctions_non_vues(deliberation: Deliberation) -> list[SanctionFraude]:
    """Les fiches posées ou levées après la validation de ce jury, sur ses notes."""
    from . import services

    if deliberation.statut != Deliberation.STATUT_VALIDEE or deliberation.date_validation is None:
        return []
    couvertes = services.notes_couvertes(deliberation).values('pk')
    return list(SanctionFraude.objects
                .filter(elements__note_id__in=couvertes)
                .filter(Q(cree_le__gt=deliberation.date_validation)
                        | Q(levee_le__gt=deliberation.date_validation))
                .select_related('inscription__etudiant')
                .distinct().order_by('cree_le'))


def _auditer(sanction: SanctionFraude, action: str, acte: str) -> None:
    try:
        from core.audit_helpers import write_audit
        write_audit(
            action=action, model_name='SanctionFraude', object_id=str(sanction.pk),
            changes={
                'etudiant': sanction.inscription.etudiant.matricule,
                'statut': sanction.statut,
                'elements': [
                    {'matiere': el.note.matiere.code, 'portee': el.portee,
                     'epreuve': el.libelle, 'origine': el.valeurs_origine,
                     'note_retenue_avant': _decimal(el.note_retenue_avant)}
                    for el in sanction.elements.select_related('note__matiere')
                ],
                'motif': sanction.motif, 'motif_levee': sanction.motif_levee,
            },
            label=f'{acte} — {sanction.inscription.etudiant.matricule}',
            institution_id=sanction.inscription.classe.institution_id,
            keep_forever=True,
        )
    except Exception:                                       # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning('Audit sanction %s échoué', sanction.pk, exc_info=True)
