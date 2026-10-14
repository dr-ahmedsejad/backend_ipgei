"""
L'avancement par semestre, classe par classe (14/10/2026).

L'ancien calcul — repris du socle SIGA — rapportait les heures faites au volume
de la maquette compté UNE fois par matière, et il comptait ces heures par
ENSEIGNANT : pour chaque (matière, type), la somme des heures du professeur le
plus chargé. Le chiffre dépendait donc de la répartition des services, pas de
l'avancement :

  * quatre classes de MP, quatre professeurs de mathématiques : on retenait
    UNE classe, et le semestre paraissait aussi avancé que la plus avancée ;
  * un même professeur dans deux classes : ses heures s'additionnaient face à
    un volume compté une fois — 100 % fait affichait 200 %.

Il lisait aussi le volume VIVANT de la maquette, et non celui gelé pour
l'année (`em.volumes`), comptait le DS comme du TD, alors que les écrans
« Avancement par matière » et « par groupe » l'en ont sorti (750beed6), et
affichait des semestres sans aucune classe cette année-là (S3 en 2024-2025).

Le défaut de l'ISS — un même cours en plusieurs copies, une par filière — n'a
pas cours ici : un seul EM par matière, une seule maquette pour toutes les
promotions. Ce qui fait « qui suit quoi » est le référentiel lui-même :

  * PRÉVU — chaque CLASSE de l'année doit la maquette de son niveau pour le
    semestre (matières actives), au volume GELÉ pour l'année, sinon au volume
    de l'EM : le même que les écrans « par matière » et « par groupe ». Une
    classe sans rien de pointé figure avec zéro fait.
  * FAIT — les séances « Fait » (`SuiviePointage`), comptées pour chaque classe
    présente : un cours commun à deux classes avance les deux. Dans une classe,
    une séance est identifiée par (matière, semaine, jour, créneau, type) : deux
    enseignants sur le même créneau n'en font qu'une.
    Un TP dédoublé avance la classe de la MOYENNE de ses sous-groupes : chacun
    doit tout le programme (un demi-groupe n'a pas un demi-programme), et A1 à
    100 % avec A2 à 0 % font une classe à moitié faite. Un sous-groupe hérite
    des séances de sa classe, sans double compte (`par_groupe._cumul`). Seuls
    comptent les sous-groupes à l'emploi du temps cette année : un sous-groupe
    déclaré mais jamais planifié n'est pas un groupe en retard.
  * TAUX — fait ÷ prévu, par type de séance, sur les classes du semestre. Sans
    plafond : une classe qui dépasse son volume dépasse 100 %.

DS, EF et ER hors avancement, comme partout ailleurs dans ce module. Rien ici
n'écrit.
"""
import re
from collections import defaultdict

from django.db.models import Q

from apps.em.volumes import volumes_par_em
from apps.ipgei.models import Classe, Matiere, SousGroupeTP
from apps.ipgei.socle import SEMESTRES
from apps.suivi.models import Suivie, SuiviePointage

from .par_groupe import CATEGORIES, _cumul, _heures_par_unite


def _ordre_semestre(code: str):
    """« S2 » avant « S10 »."""
    m = re.search(r'\d+', code or '')
    return (int(m.group()) if m else 0, code)


def _maquette(type_semestre: str) -> dict:
    """{(code semestre, niveau): [EM]} — les matières actives, par leur miroir."""
    codes = [code for code, (_, type_) in SEMESTRES.items() if type_ == type_semestre]
    maquette = defaultdict(list)
    for matiere in (Matiere.objects
                    .filter(actif=True, em_miroir__isnull=False, code_semestre__in=codes)
                    .select_related('niveau_ref', 'em_miroir')
                    .order_by('code_semestre', 'pk')):
        maquette[(matiere.code_semestre, matiere.niveau)].append(matiere.em_miroir)
    return maquette


def _classes(annee: str, em_ids) -> dict:
    """
    {classe: [unités qui reçoivent l'enseignement]} pour l'année.

    Les classes actives, plus celles qu'un pointage ou l'emploi du temps
    mentionne : une classe fermée en cours d'année garde ses heures faites.
    Les unités sont les sous-groupes de la classe à l'emploi du temps, sinon la
    classe elle-même.
    """
    a_l_edt = set(Suivie.objects
                  .filter(annee_universitaire=annee, em_id__in=em_ids)
                  .values_list('departement_id', flat=True))
    a_l_edt |= set(SuiviePointage.objects
                   .filter(annee_universitaire=annee, em_id__in=em_ids)
                   .values_list('departements__id', flat=True))

    classes = list(Classe.objects
                   .filter(annee_universitaire=annee, is_container=False)
                   .filter(Q(actif=True) | Q(pk__in=a_l_edt))
                   .select_related('niveau'))
    unites = {c: [] for c in classes}
    par_id = {c.pk: c for c in classes}
    for sous_groupe_id, classe_id in (SousGroupeTP.objects
                                      .filter(classe_id__in=list(par_id), pk__in=a_l_edt)
                                      .order_by('pk')
                                      .values_list('pk', 'classe_id')):
        unites[par_id[classe_id]].append(sous_groupe_id)
    return {c: (u or [c.pk]) for c, u in unites.items()}


def avancement_par_semestre(annee: str, type_semestre: str, pointages=None) -> dict:
    """Prévu et fait, en heures, par semestre et par type de séance.

    Rend {code_semestre: {'prevu': {CM, TD, TP, PR}, 'fait': {…}, 'classes': n}}
    pour les semestres où au moins une classe de l'année doit une matière.

    `pointages` — les séances faites — n'est à fournir que pour une simulation :
    par défaut, les `SuiviePointage` « Fait » de l'année.
    """
    maquette = _maquette(type_semestre)
    em_ids = {em.pk for ems in maquette.values() for em in ems}
    classes = _classes(annee, em_ids)
    volumes = volumes_par_em(annee)

    if pointages is None:
        pointages = (SuiviePointage.objects
                     .filter(annee_universitaire=annee, commentaire='Fait',
                             em_id__in=em_ids)
                     .select_related('type_seance_fk')
                     .prefetch_related('departements'))
    par_em = defaultdict(list)
    for p in pointages:
        if p.em_id in em_ids:
            par_em[p.em_id].append(p)
    # Par matière, puis par famille et par unité : {clé de séance: durée}.
    faites = {em_id: _heures_par_unite(liste) for em_id, liste in par_em.items()}

    semestres = defaultdict(lambda: {'prevu': dict.fromkeys(CATEGORIES, 0.0),
                                     'fait': dict.fromkeys(CATEGORIES, 0.0),
                                     'classes': 0})
    for classe, unites in classes.items():
        niveau = classe.niveau.niveau if classe.niveau_id else ''
        for (code, niveau_maquette), ems in maquette.items():
            if niveau_maquette != niveau:
                continue
            s = semestres[code]
            s['classes'] += 1
            for em in ems:
                vol = volumes.get(em.pk) or em
                heures = faites.get(em.pk)
                for f in CATEGORIES:
                    s['prevu'][f] += float(getattr(vol, f) or 0)
                    if heures is None:
                        continue
                    parent = None if unites == [classe.pk] else classe.pk
                    s['fait'][f] += sum(_cumul(heures[f], u, parent)
                                        for u in unites) / len(unites)

    return {code: semestres[code] for code in sorted(semestres, key=_ordre_semestre)}


def pourcentage(fait: float, prevu: float) -> float:
    return round(fait / prevu * 100, 1) if prevu > 0 else 0
