"""Les cases à cocher de l'écran « Annonces », et qui elles désignent.

À l'IPGEI, une case = une CLASSE de l'année en cours (MPSI A, MP B…). Les
lignes sont les niveaux (MPSI, MP), les colonnes les lettres de classe : cocher
la ligne MPSI, c'est « toutes les classes MPSI ». La classe d'accueil (sans
lettre, où attendent les nouveaux avant répartition) n'en fait pas partie.

Les élèves d'une classe : ses inscriptions de l'année, abandons exclus —
auditeurs libres compris, ils suivent les cours de la classe.

Identifiant d'une case : « c<classe> ». Il se décode côté serveur à l'envoi —
l'écran ne choisit jamais lui-même les destinataires.
"""
import re

from django.db.models import Count

CLE = re.compile(r'^c(\d+)$')


def annee_active():
    from apps.ipgei.models import Classe
    from apps.parametres.models import Year
    a = Year.objects.filter(est_active=True).values_list('annee', flat=True).first()
    if a:
        return a
    return (Classe.objects.exclude(annee_universitaire='')
            .order_by('-annee_universitaire').values_list('annee_universitaire', flat=True).first())


def _classes(annee):
    from apps.ipgei.models import Classe
    return (Classe.objects.filter(annee_universitaire=annee, actif=True, niveau__isnull=False)
            .exclude(groupe=''))


def _inscriptions(annee):
    from apps.ipgei.models import InscriptionIPGEI
    return (InscriptionIPGEI.objects
            .filter(annee_universitaire=annee, etudiant__user__isnull=False,
                    etudiant__user__is_active=True)
            .exclude(statut=InscriptionIPGEI.STATUT_ABANDON))


def _rangs():
    from apps.ipgei.models import NiveauIPGEI
    return dict(NiveauIPGEI.objects.values_list('code', 'rang'))


def grille(annee=None):
    """Lignes (niveaux), colonnes (lettres de classe) et classes existantes,
    avec leur nombre d'élèves ayant un compte."""
    annee = annee or annee_active()
    classes = list(_classes(annee).select_related('niveau'))
    comptes = dict(_inscriptions(annee).filter(classe__in=classes)
                   .values('classe').annotate(n=Count('etudiant', distinct=True))
                   .values_list('classe', 'n'))
    rangs = _rangs()
    niveaux = {c.niveau_id: c.niveau for c in classes}
    lettres = sorted({c.groupe for c in classes})
    nb_ens = enseignants_par_classe(annee, [c.pk for c in classes])
    return {
        'annee': annee,
        'nb_enseignants': _profs().count(),
        'lignes': [{'id': f'n{n.pk}', 'label': n.niveau,
                    'sub': f'{rangs[n.niveau]}{"re" if rangs[n.niveau] == 1 else "e"} année'
                    if rangs.get(n.niveau) else ''}
                   for n in sorted(niveaux.values(), key=lambda n: (rangs.get(n.niveau, 99), n.niveau))],
        'colonnes': [{'id': f'g{g}', 'label': f'Classe {g}', 'sub': ''} for g in lettres],
        'cases': [{'cle': f'c{c.pk}', 'ligne': f'n{c.niveau_id}', 'colonne': f'g{c.groupe}',
                   'nb': comptes.get(c.pk, 0),
                   'nb_enseignants': len(nb_ens.get(c.pk, ())),
                   # Les identifiants (Prof) : l'écran compte l'union sans
                   # doublon quand plusieurs classes partagent un enseignant.
                   'enseignants': sorted(nb_ens.get(c.pk, ()))} for c in classes],
    }


def destinataires(cles, annee=None):
    """Les comptes des élèves des classes désignées (identifiants inconnus ou
    d'une autre année ignorés). Retourne (ids des utilisateurs, cases retenues)."""
    annee = annee or annee_active()
    voulues = {int(m[1]) for m in (CLE.match(str(c)) for c in cles or []) if m}
    classes = set(_classes(annee).filter(pk__in=voulues).values_list('pk', flat=True))
    if not classes:
        return [], []
    ids = (_inscriptions(annee).filter(classe_id__in=classes)
           .values_list('etudiant__user_id', flat=True).distinct())
    return list(ids), [f'c{pk}' for pk in sorted(classes)]


# ── Les enseignants (décision du 11/10/2026, comme à l'ESP) ─────────────────
#
# « Ceux qui enseignent dans une classe » : les enseignants qui y ont cours
# cette année — séances de l'emploi du temps de la classe (sous-groupes de TP
# compris, ils sont rattachés à leur classe) et emploi du temps projeté.

def _profs():
    from apps.prof.models import Prof
    return Prof.objects.filter(actif=True, user__isnull=False, user__is_active=True)


def enseignants_par_classe(annee, classes):
    """{classe: {ids de Prof ayant un compte}} pour chaque classe donnée."""
    from apps.emplois.models import Emplois
    from apps.ipgei.models import SeanceReelle
    actifs = set(_profs().values_list('pk', flat=True))
    par_classe = {pk: set() for pk in classes}
    for classe, prof in (SeanceReelle.objects.filter(classe_id__in=classes)
                         .exclude(prof__isnull=True).values_list('classe_id', 'prof_id')):
        par_classe[classe].add(prof)
    # Une classe EST un groupe (`Departement`) : même identifiant.
    for classe, prof in (Emplois.objects.filter(departement_id__in=classes, annee_universitaire=annee)
                         .exclude(prof__isnull=True).values_list('departement_id', 'prof_id')):
        par_classe[classe].add(prof)
    return {pk: profs & actifs for pk, profs in par_classe.items()}


def destinataires_enseignants(mode, cles, annee=None):
    """Les comptes des enseignants visés : tous, ou ceux des classes cochées."""
    if mode == 'tous':
        return list(_profs().values_list('user_id', flat=True))
    if mode != 'cases':
        return []
    annee = annee or annee_active()
    voulues = {int(m[1]) for m in (CLE.match(str(c)) for c in cles or []) if m}
    classes = list(_classes(annee).filter(pk__in=voulues).values_list('pk', flat=True))
    if not classes:
        return []
    profs = set()
    for ids in enseignants_par_classe(annee, classes).values():
        profs |= ids
    return list(_profs().filter(pk__in=profs).values_list('user_id', flat=True))
