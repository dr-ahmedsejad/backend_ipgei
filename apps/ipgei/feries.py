"""
Jours fériés — marquage d'un jour du calendrier, et ses effets sur l'emploi du temps.

LA CONVENTION, dont tout le reste découle :

  * un jour férié ISOLÉ garde le numéro de sa semaine et prend le type
    `ferie` ;
  * une semaine ENTIÈRE hors cours garde son comportement : numéro NULL.

Appartenir à la séquence pédagogique, c'est AVOIR UN NUMÉRO — et non avoir le
type « cours ». Sans cela, marquer le 28 novembre sortait toute sa semaine de
la séquence et renumérotait les suivantes, comme le faisait le seul marquage
qui existait, celui d'une semaine entière.

OÙ VIT LE CALENDRIER. Deux tables, et il faut savoir laquelle fait foi :

  * `parametres.Semaine` — une ligne par JOUR. C'est le calendrier : on y
    marque le férié ;
  * `ipgei.SemaineIPGEI` — une ligne par SEMAINE, reflet du premier
    (`services.synchroniser_calendrier`). C'est elle qui porte les séances.

Une séance se rattache à son jour par `semaine` + `jour`, et porte sa `date`.
C'est par la date qu'on retrouve les séances d'un jour marqué.

CE QUE DEVIENNENT LES SÉANCES. Annulées, jamais supprimées, avec
`motif_annulation = 'ferie'`. La projection vers le suivi ignore déjà les
séances annulées (`miroir.projeter_semaine_reelle`, `annulee=False`) : ni
pointage ni paie. Le motif est ce qui permet au retrait du férié de rétablir
CES séances-là, et jamais une annulation faite à la main.
"""
from dataclasses import dataclass
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from .socle import SEMESTRES

#: État d'un jour, du point de vue de l'emploi du temps.
COURS      = 'cours'
FERIE      = 'ferie'        # férié ISOLÉ : numéroté, dans une semaine de cours
HORS_COURS = 'hors_cours'   # semaine entière de vacances, d'examens…

JOURS_FR = ('Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi', 'Dimanche')


class JourIntrouvable(Exception):
    """Aucun jour du calendrier à cette date — l'API le rend en 404."""


class MarquageBloque(Exception):
    """Refus d'état (suivi généré, semaine hors séquence…) — rendu en 409."""


@dataclass
class EtatJour:
    etat:    str
    libelle: str = ''
    date:    object = None

    @property
    def ferme(self) -> bool:
        return self.etat != COURS


def libelle_date(d) -> str:
    """« Samedi 28/11/2026 » — la forme des messages."""
    return f'{JOURS_FR[d.weekday()]} {d.strftime("%d/%m/%Y")}'


def _pluriel(n: int, singulier: str, pluriel: str = None) -> str:
    return f'{n} {singulier if n == 1 else (pluriel or singulier + "s")}'


def codes_du_type(type_semestre: str) -> list:
    """Les codes IPGEI d'une parité : 'I' → S1, S3 ; 'P' → S2, S4."""
    return [code for code, (_, t) in SEMESTRES.items() if t == type_semestre]


# ═════════════════════════════════════════════════════════════════════════════
# Lire l'état d'un jour
# ═════════════════════════════════════════════════════════════════════════════
def _etat_de_la_ligne(ligne) -> EtatJour:
    from apps.parametres.models import Semaine

    if ligne is None or ligne.type_semaine == Semaine.TYPE_COURS:
        return EtatJour(COURS, date=getattr(ligne, 'date', None))
    if ligne.numero_semaine is not None:
        return EtatJour(FERIE, ligne.description or 'Jour férié', ligne.date)
    return EtatJour(HORS_COURS, ligne.get_type_semaine_display(), ligne.date)


def etats_de_la_semaine(semaine) -> dict:
    """
    `{jour_id: EtatJour}` pour une semaine IPGEI, en une requête.

    Un jour SANS ligne au calendrier compte comme cours : des semaines IPGEI ont
    existé avant que le calendrier du socle ne fasse foi, et y bloquer toute
    saisie aurait gelé des emplois du temps entiers pour une absence de
    paramétrage.
    """
    from apps.parametres.models import Jour, Semaine

    ferme_semaine = semaine.type_semaine != semaine.TYPE_COURS
    lignes = {
        l.date: l for l in Semaine.objects.filter(
            annee_universitaire=semaine.semestre.annee_universitaire,
            type_semestre=semaine.semestre.type_semestre,
            date__gte=semaine.date_debut, date__lte=semaine.date_fin,
        )
    }
    etats = {}
    for jour in Jour.objects.all():
        libelle = (jour.jour or '').strip().capitalize()
        if libelle not in JOURS_FR:
            continue
        d = semaine.date_debut + timedelta(days=JOURS_FR.index(libelle))
        if ferme_semaine:
            etats[jour.pk] = EtatJour(HORS_COURS, semaine.get_type_semaine_display(), d)
            continue
        etat = _etat_de_la_ligne(lignes.get(d))
        etat.date = d
        etats[jour.pk] = etat
    return etats


def etat_du_jour(semaine, jour) -> EtatJour:
    if semaine is None or jour is None:
        return EtatJour(COURS)
    return etats_de_la_semaine(semaine).get(jour.pk, EtatJour(COURS))


# ═════════════════════════════════════════════════════════════════════════════
# Les refus — côté serveur, pour tous les chemins
# ═════════════════════════════════════════════════════════════════════════════
def refuser_ajout(semaine, jour):
    """AJOUTER une séance : refusé sur tout jour hors cours."""
    etat = etat_du_jour(semaine, jour)
    if etat.ferme:
        raise ValidationError(
            f"{libelle_date(etat.date)} n'est pas un jour de cours "
            f'({etat.libelle}) : on ne peut pas y placer de séance.')


def refuser_modification(seance, verbe: str = 'modifier'):
    """
    MODIFIER, SUPPRIMER, PARTAGER : refusé sur un férié ISOLÉ seulement.

    Supprimée, la séance ne pourrait plus être rétablie au retrait du férié ;
    modifiée, elle reviendrait autre qu'elle n'était. Une semaine ENTIÈRE de
    vacances, elle, n'a pas de rétablissement : y bloquer la suppression
    empêcherait de la nettoyer.
    """
    etat = etat_du_jour(seance.semaine, seance.jour)
    if etat.etat == FERIE:
        raise ValidationError(
            f'{libelle_date(etat.date)} est férié ({etat.libelle}) : impossible '
            f'de {verbe} cette séance. Elle est annulée et sera rétablie telle '
            "quelle au retrait du férié — retirez-le d'abord si elle doit changer.")


def refuser_retablissement(seance):
    """Rétablir une séance annulée revient à la placer : même règle que l'ajout."""
    etat = etat_du_jour(seance.semaine, seance.jour)
    if etat.ferme:
        raise ValidationError(
            f"{libelle_date(etat.date)} n'est pas un jour de cours "
            f'({etat.libelle}) : la séance ne peut pas y être rétablie.')


# ═════════════════════════════════════════════════════════════════════════════
# Marquer et retirer un jour
# ═════════════════════════════════════════════════════════════════════════════
def _ligne(annee, type_semestre, d):
    from apps.parametres.models import Semaine

    ligne = (Semaine.objects
             .filter(annee_universitaire=annee, type_semestre=type_semestre, date=d)
             .first())
    if ligne is None:
        raise JourIntrouvable(
            f'Aucun jour du calendrier au {d.strftime("%d/%m/%Y")} pour '
            f'{annee}, semestres {"impairs" if type_semestre == "I" else "pairs"}.')
    return ligne


def _bloque_par_suivi(annee, type_semestre, numero, geste: str):
    from apps.suivi.models import Suivie

    if numero is not None and Suivie.objects.filter(
            annee_universitaire=annee, type_semestre=type_semestre,
            numero_semaine=numero).exists():
        raise MarquageBloque(
            f'{geste} impossible : le suivi de la semaine {numero} est déjà '
            "généré, et il a été tiré de l'emploi du temps tel qu'il était. "
            'Supprimez d\'abord ce suivi (Suivi → Générer, suppression de la '
            'dernière semaine en premier), puis recommencez.')


def seances_du_jour(annee, type_semestre, d):
    """Les séances IPGEI d'une date, sur les semestres de cette parité."""
    from .models import SeanceReelle

    libelle = JOURS_FR[d.weekday()]
    return SeanceReelle.objects.filter(
        semaine__semestre__annee_universitaire=annee,
        semaine__semestre__code__in=codes_du_type(type_semestre),
    ).filter(
        # La date d'abord ; le repli par semaine et jour pour une séance que
        # rien n'a datée — il n'y en a pas en base, mais la colonne le permet.
        Q(date=d) | Q(date__isnull=True, semaine__date_debut__lte=d,
                      semaine__date_fin__gte=d, jour__jour__iexact=libelle)
    )


def _annuler_pour_ferie(seances) -> int:
    """Sauvegardes unitaires : `update()` ne poserait pas `modifiee_le`."""
    from .models import SeanceReelle

    n = 0
    for seance in seances.filter(annulee=False):
        seance.annulee = True
        seance.motif_annulation = SeanceReelle.MOTIF_FERIE
        seance.save(update_fields=['annulee', 'motif_annulation', 'modifiee_le'])
        n += 1
    return n


def retablir_seances_ferie(seances) -> int:
    """Rétablit CELLES du férié. Une annulation à la main reste annulée."""
    from .models import SeanceReelle

    n = 0
    for seance in seances.filter(annulee=True,
                                 motif_annulation=SeanceReelle.MOTIF_FERIE):
        seance.annulee = False
        seance.motif_annulation = ''
        seance.save(update_fields=['annulee', 'motif_annulation', 'modifiee_le'])
        n += 1
    return n


@transaction.atomic
def marquer_jour_ferie(annee, type_semestre, d, libelle) -> dict:
    """Marque UN jour férié. Il garde son numéro ; rien n'est renuméroté."""
    from apps.parametres.models import Semaine

    libelle = (libelle or '').strip()
    if not libelle:
        raise ValidationError('Le nom du jour férié est obligatoire.')

    ligne = _ligne(annee, type_semestre, d)
    if ligne.numero_semaine is None:
        raise MarquageBloque(
            f'{libelle_date(d)} appartient à une semaine entière hors cours '
            f'({ligne.get_type_semaine_display()}) : il n\'y a pas de cours à '
            'y suspendre.')
    if ligne.type_semaine == Semaine.TYPE_FERIE and ligne.description == libelle:
        return {'changed': False, 'date': d.isoformat(), 'seances_annulees': 0,
                'message': f'{libelle_date(d)} est déjà férié ({libelle}).'}

    _bloque_par_suivi(annee, type_semestre, ligne.numero_semaine, 'Marquage')

    ligne.type_semaine = Semaine.TYPE_FERIE
    ligne.description  = libelle
    ligne.save(update_fields=['type_semaine', 'description'])

    annulees = _annuler_pour_ferie(seances_du_jour(annee, type_semestre, d))
    return {
        'changed':          True,
        'date':             d.isoformat(),
        'numero_semaine':   ligne.numero_semaine,
        'libelle':          libelle,
        'seances_annulees': annulees,
        'message': (f'{libelle_date(d)} marqué férié — '
                    f'{_pluriel(annulees, "séance annulée", "séances annulées")}'),
    }


@transaction.atomic
def retirer_jour_ferie(annee, type_semestre, d) -> dict:
    """Rend le jour aux cours, et rétablit les séances que le férié avait annulées."""
    from apps.parametres.models import Semaine

    from .models import SeanceReelle

    ligne = _ligne(annee, type_semestre, d)
    if ligne.type_semaine != Semaine.TYPE_FERIE or ligne.numero_semaine is None:
        raise MarquageBloque(f"{libelle_date(d)} n'est pas un jour férié isolé.")

    _bloque_par_suivi(annee, type_semestre, ligne.numero_semaine, 'Retrait')

    ligne.type_semaine = Semaine.TYPE_COURS
    ligne.description  = ''
    ligne.save(update_fields=['type_semaine', 'description'])

    seances = seances_du_jour(annee, type_semestre, d)
    retablies = retablir_seances_ferie(seances)
    restees = seances.filter(annulee=True).exclude(
        motif_annulation=SeanceReelle.MOTIF_FERIE).count()

    message = (f'{libelle_date(d)} rendu aux cours — '
               f'{_pluriel(retablies, "rétablie")}')
    if restees:
        message += (f' ({_pluriel(restees, "annulée")} à la main '
                    f'{"reste annulée" if restees == 1 else "restent annulées"})')
    return {'changed': True, 'date': d.isoformat(), 'seances_retablies': retablies,
            'annulations_manuelles': restees, 'message': message}


# ═════════════════════════════════════════════════════════════════════════════
# Fériés fixes
# ═════════════════════════════════════════════════════════════════════════════
def _feries_actifs() -> dict:
    from apps.parametres.models import JourFerieFixe

    return {(f.jour, f.mois): f.libelle
            for f in JourFerieFixe.objects.filter(actif=True)}


def appliquer_feries_fixes(lignes) -> dict:
    """
    Applique les fériés fixes aux lignes de calendrier DÉSIGNÉES, et à elles seules.

    Chaque jour est traité à part : un jour bloqué par un suivi est ÉCARTÉ avec
    son motif, sans empêcher les autres. Un jour déjà férié, ou hors séquence,
    n'est pas touché. Idempotent.
    """
    from apps.parametres.models import Semaine

    feries = _feries_actifs()
    appliques, ecartes = [], []
    for ligne in lignes:
        libelle = feries.get((ligne.date.day, ligne.date.month))
        if libelle is None or ligne.type_semaine != Semaine.TYPE_COURS \
                or ligne.numero_semaine is None:
            continue
        try:
            bilan = marquer_jour_ferie(ligne.annee_universitaire,
                                       ligne.type_semestre, ligne.date, libelle)
        except (MarquageBloque, ValidationError) as exc:
            ecartes.append({'date': ligne.date.isoformat(), 'libelle': libelle,
                            'motif': getattr(exc, 'messages', [str(exc)])[0]
                            if isinstance(exc, ValidationError) else str(exc)})
            continue
        appliques.append({'date': ligne.date.isoformat(), 'libelle': libelle,
                          'seances_annulees': bilan['seances_annulees']})
    return {'appliques': appliques, 'ecartes': ecartes}


def apercu_feries_fixes(annee=None, type_semestre=None) -> list:
    """
    Les jours du calendrier qui tomberaient sur un férié fixe, SANS rien marquer.

    Pour décider en connaissance de cause : chaque jour, sa semaine, et le
    nombre de séances que le marquage annulerait.
    """
    from apps.parametres.models import Semaine

    feries = _feries_actifs()
    qs = Semaine.objects.filter(type_semaine=Semaine.TYPE_COURS,
                                numero_semaine__isnull=False)
    if annee:
        qs = qs.filter(annee_universitaire=annee)
    if type_semestre:
        qs = qs.filter(type_semestre=type_semestre)
    sortie = []
    for ligne in qs.order_by('date'):
        libelle = feries.get((ligne.date.day, ligne.date.month))
        if libelle is None:
            continue
        sortie.append({
            'date': ligne.date.isoformat(), 'jour': libelle_date(ligne.date),
            'libelle': libelle, 'annee_universitaire': ligne.annee_universitaire,
            'type_semestre': ligne.type_semestre,
            'numero_semaine': ligne.numero_semaine,
            'seances_a_annuler': seances_du_jour(
                ligne.annee_universitaire, ligne.type_semestre, ligne.date,
            ).filter(annulee=False).count(),
        })
    return sortie


def jours_feries_de_la_periode(annee, type_semestre=None) -> list:
    """Les jours fériés ISOLÉS marqués sur une période, pour la page dédiée."""
    from apps.parametres.models import Semaine

    qs = Semaine.objects.filter(annee_universitaire=annee,
                                type_semaine=Semaine.TYPE_FERIE,
                                numero_semaine__isnull=False)
    if type_semestre:
        qs = qs.filter(type_semestre=type_semestre)
    return [{
        'id': l.pk, 'date': l.date.isoformat(), 'jour': libelle_date(l.date),
        'libelle': l.description, 'numero_semaine': l.numero_semaine,
        'type_semestre': l.type_semestre, 'annee_universitaire': l.annee_universitaire,
    } for l in qs.order_by('date')]
