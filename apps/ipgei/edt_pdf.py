"""
Impression des emplois du temps IPGEI.

Reprend les gabarits du socle — `emploi_filiere_pdf.html`, `emploi_prof_pdf.html`
et `emploi_salle_pdf.html` — afin qu'un emploi du temps de prépa se présente
exactement comme celui d'une filière SIGA : mêmes couleurs par type de séance,
même cadre de salle, même en-tête d'institution.

La source reste `ipgei.SeanceReelle` et non `suivi.Suivie` : elle seule couvre
les semaines à venir, et elle seule porte les annulations, les permutations et
le sous-groupe. Le calendrier `SemaineIPGEI` fournit le numéro de semaine et les
dates qui la bornent.

Les gabarits attendent le vocabulaire du socle (CM / TD / TP / DS) : les types
IPGEI y sont traduits, faute de quoi les cases sortiraient sans couleur.
"""
import logging
from collections import defaultdict

from django.utils.formats import date_format

from apps.parametres.models import Creneau, Jour
from core.types_seance import TYPES_EPREUVE

logger = logging.getLogger('siga')

# Types de séance IPGEI → libellés attendus par les gabarits, qui portent une
# couleur par classe CSS (`.session-type.CM`, `.TD`, `.TP`, `.PR`).
TYPE_AFFICHE = {'cours': 'CM', 'td': 'TD', 'tp': 'TP', 'ds': 'DS'}


def _date_fr(valeur):
    """« 15 septembre » — même forme que les emplois du temps du socle."""
    if valeur is None:
        return ''
    try:
        return date_format(valeur, format='d F', use_l10n=True)
    except Exception:
        return valeur.strftime('%d/%m/%Y')


def _ligne(seance, axe):
    """
    Ligne décrivant UNE séance à l'intérieur d'un bloc matière.

    Ce qu'elle nomme dépend de l'axe : sur l'emploi du temps d'un enseignant,
    répéter son nom n'apprend rien, c'est la classe qui manque. Le sujet du
    document n'est donc jamais répété dans ses propres cases.
    """
    groupe = seance.sous_groupe.groupe if seance.sous_groupe_id else ''
    prof   = seance.prof.nom if seance.prof else ''
    salle  = seance.salle.nom if seance.salle else ''
    classe = seance.classe.nom if seance.classe_id else ''

    if axe == 'prof':
        principal, complement = classe, salle
    elif axe == 'salle':
        principal, complement = prof, classe
    else:                                    # axe classe
        principal, complement = prof, salle

    # Une séance spéciale — sport, instruction militaire — n'a ni enseignant ni
    # salle : la ligne n'aurait rien à dire, autant ne pas l'écrire.
    if not principal and not complement and not groupe:
        return ''
    texte = principal or '—'
    if groupe:
        texte = f'{texte} ({groupe})'
    if complement:
        texte = f'{texte} — {complement}'
    return texte


#: Les jours dans l'ordre de `date.weekday()`. Le référentiel `Jour` est
#: libellé en toutes lettres et son ordre n'est pas garanti : le nom est la
#: seule correspondance stable entre une date et une ligne de la grille.
JOURS_SEMAINE = ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi',
                 'dimanche']


def construire_grille(seances, *, axe='classe', epreuves=()):
    """
    Transforme des séances en (créneaux, lignes) pour le gabarit IPGEI.

    Les séances d'une même case sont **regroupées par matière et par type** :
    un TP dédoublé s'affiche sous un seul en-tête « INFO / TP », suivi d'une
    ligne par groupe. Empiler deux blocs complets doublait la hauteur de la
    case, et donc celle de toute la ligne du jour.

    La grille est complète : tous les jours du référentiel et tous les créneaux
    actifs, occupés ou non. Une case vide est une information — le créneau est
    libre — et masquer les jours creux décalerait les colonnes d'une semaine à
    l'autre, rendant deux tirages incomparables.

    `epreuves` — des `EpreuveIPGEI` — sont posées EN SURIMPRESSION, fusionnant
    les colonnes que leur intervalle recouvre. Elles ne sont pas des séances :
    leur durée ne tombe pas sur la grille de 1h30, elles convoquent plusieurs
    classes et n'ont ni salle ni enseignant. Le document devait pourtant les
    porter — un emploi du temps distribué qui tairait l'examen serait pire
    qu'inutile, et une semaine d'examens n'a souvent rien d'autre à montrer.

    Chaque cellule rendue porte son `colspan` : une table HTML ne sait fusionner
    que des colonnes contiguës, et c'est au constructeur de la grille de le
    décider, non au gabarit.
    """
    seances = [s for s in seances if s.creneau_id and not s.annulee]

    creneaux = list(Creneau.objects.filter(is_actif=True).order_by('ordre', 'creneau'))

    # (jour, créneau) → (matière, type) → bloc
    grille = defaultdict(lambda: defaultdict(dict))
    for seance in seances:
        jour = seance.jour.jour if seance.jour_id else ''
        type_affiche = seance.type_seance.type_seance
        cle = (seance.matiere_id, type_affiche)

        blocs = grille[jour][seance.creneau_id]
        if cle not in blocs:
            blocs[cle] = {
                'type_seance': type_affiche,
                # Le référentiel porte lui-même la distinction : sport et
                # instruction militaire n'ont ni enseignant ni salle.
                'type_seance_is_special': seance.type_seance.is_special,
                # Devoir, examen final et rattrapage : les trois creneaux
                # qu'un etudiant ne peut pas manquer. Ils partagent le meme
                # traitement visuel.
                'est_evaluation': type_affiche in TYPES_EPREUVE,
                # Une séance spéciale n'a rien d'autre à montrer que son nom :
                # ni matière, ni intervenant, ni salle. Le badge de type ferait
                # doublon avec le seul texte de la case.
                'est_speciale': getattr(seance.type_seance, 'is_special', False),
                'em_code':     seance.matiere.code if seance.matiere else '',
                # Sans matière, le type porte seul le sens de la case.
                'em_intitule': seance.matiere.intitule if seance.matiere else type_affiche,
                'lignes':      [],
            }
        ligne = _ligne(seance, axe)
        if ligne:
            blocs[cle]['lignes'].append(ligne)

    # Les épreuves du jour, avec la plage de colonnes qu'elles recouvrent.
    par_jour = defaultdict(list)
    index_creneau = {c.id: i for i, c in enumerate(creneaux)}
    for epreuve in epreuves or ():
        indices = [index_creneau[c.id] for c in creneaux if epreuve.recouvre(c)]
        if not indices:
            # Une épreuve logée dans une pause — 13h00-15h10 — ne recouvre
            # aucun créneau. Elle ne bloque rien, donc elle ne fusionne rien.
            continue
        par_jour[JOURS_SEMAINE[epreuve.date.weekday()]].append(
            (min(indices), max(indices), epreuve))

    def cellules(jour):
        """Les cases d'une ligne, l'épreuve fusionnant celles qu'elle couvre."""
        plages = sorted(par_jour.get(jour.lower(), []), key=lambda p: (p[0], p[1]))
        rendu, i = [], 0
        while i < len(creneaux):
            plage = next((p for p in plages if p[0] == i), None)
            if plage is not None:
                debut, fin, epreuve = plage
                rendu.append({
                    'colspan': fin - debut + 1,
                    'blocs':   [],
                    'epreuve': {
                        'type':    epreuve.type_epreuve,
                        'intitule': epreuve.intitule_affiche,
                        'horaire': (f'{epreuve.heure_debut:%Hh%M} → '
                                    f'{epreuve.heure_fin:%Hh%M} · {epreuve.duree_libelle}'),
                    },
                })
                i = fin + 1
                continue
            rendu.append({
                'colspan': 1,
                'blocs':   list(grille[jour][creneaux[i].id].values()),
                'epreuve': None,
            })
            i += 1
        return rendu

    lignes = [
        {'jour': jour.jour, 'cells': cellules(jour.jour)}
        for jour in Jour.objects.all().order_by('id')
    ]
    return creneaux, lignes


def reponse_pdf(template, contexte, nom_fichier):
    """
    Rend un emploi du temps en PDF, en paysage et **sans pied de page**.

    `core.pdf_renderer.render_pdf_response` numérote les pages (« Page 1 / 1 »),
    ce qui convient à un état de plusieurs feuillets mais pas à un emploi du
    temps : il tient sur une page, et cette mention affichée seule en bas d'une
    grille distribuée aux étudiants n'apprend rien. On ne touche pas au socle —
    ses autres documents gardent leur numérotation.
    """
    from datetime import date

    from django.http import HttpResponse
    from rest_framework.response import Response

    from core.pdf_renderer import render_pdf_bytes
    from core.pdf_utils import get_institution_context

    contexte = {
        **contexte,
        **get_institution_context(),
        'date_impression': date.today().strftime('%d/%m/%Y'),
    }
    options = {
        'margin-top':               '0.40in',
        'margin-right':             '0.40in',
        'margin-bottom':            '0.40in',
        'margin-left':              '0.40in',
        'orientation':              'Landscape',
        'page-size':                'A4',
        'enable-local-file-access': '',
        'encoding':                 'UTF-8',
    }
    try:
        pdf = render_pdf_bytes(template, contexte, options)
    except Exception as exc:                       # pragma: no cover - dépend de wkhtmltopdf
        logger.error('PDF emploi du temps : %s', exc)
        return Response({'error': f'Erreur génération PDF : {exc}'}, status=500)

    reponse = HttpResponse(pdf, content_type='application/pdf')
    reponse['Content-Disposition'] = f'attachment; filename="{nom_fichier}"'
    return reponse


def contexte_semaine(semaine):
    """
    Numéro, bornes et **état** de la semaine.

    L'état figure sur le document : un emploi du temps imprimé circule détaché
    de l'écran qui l'a produit, et rien ne disait s'il correspondait encore au
    suivi. « Prévisionnel » et « modifié après génération » ne s'opposent pas
    de la même façon à ce qui a été pointé, donc payé.
    """
    if semaine is None:
        return {'semaine': '', 'debut': '', 'fin': '', 'semestre_nom': '',
                'etat_semaine': ''}
    return {
        'semaine':      semaine.numero or '',
        'debut':        _date_fr(semaine.date_debut),
        'fin':          _date_fr(semaine.date_fin),
        'semestre_nom': f'Semestre {semaine.semestre.code}' if semaine.semestre_id else '',
        'etat_semaine': semaine.libelle_coherence,
        'est_divergent': semaine.etat_coherence == semaine.ETAT_DIVERGENT,
    }
