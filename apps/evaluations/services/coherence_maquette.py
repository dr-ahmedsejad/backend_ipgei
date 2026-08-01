"""
Cohérence des maquettes — convention Art. 14 Arrêté 562 / Art. 19 Décret 2018-070.

La MGS de SIGA est calculée sur les EM (Σ note_EM × coeff_EM / Σ coeff_EM),
formule identique au relevé de notes officiel. Elle n'est mathématiquement
égale à la « moyenne pondérée des modules » exigée par les textes QUE si le
coefficient déclaré de chaque module vaut la somme des coefficients de ses EM
(= le poids effectif du module dans la MGS).

Ce service détecte les modules qui violent cette convention. Il est :
  - appelé par PVDeliberationViewSet.peupler (warnings non bloquants) ;
  - exposé en batch via `python manage.py verifier_maquettes [--fix]`.

Le coefficient effectif d'un EM suit la même règle de fallback que le moteur
de calcul (calcul_module.ResultatModuleService.calculer) :
  coefficient EM planification (em.coefficient) sinon 1.
Pour un module sans EM de planification, fallback sur ses ElementModule LMD.
"""
from decimal import Decimal


def verifier_coherence_coefficients(filiere=None, semestre_code=None,
                                    institution=None) -> list[dict]:
    """
    Retourne la liste des modules actifs dont le coefficient déclaré diffère
    de la somme des coefficients effectifs de leurs EM.

    Chaque anomalie : {
      'module_id', 'module_code', 'filiere_code', 'semestre_code',
      'coefficient_module': Decimal, 'somme_coefficients_em': Decimal, 'nb_em': int,
    }
    Les modules sans aucun EM ni ElementModule sont ignorés (maquette vide,
    problème distinct — couvert par Module.clean sur les crédits).
    """
    from apps.modules.models import Module
    from apps.em.models import EM

    qs = Module.objects.filter(actif=True).select_related('semestre', 'filiere')
    if filiere is not None:
        qs = qs.filter(filiere=filiere)
    if semestre_code:
        qs = qs.filter(semestre__code_semestre__iexact=semestre_code)
    if institution is not None:
        qs = qs.filter(institution=institution)

    anomalies = []
    for module in qs:
        ems = list(EM.objects.filter(module_lmd=module))
        if ems:
            somme = sum(
                (Decimal(str(e.coefficient)) if e.coefficient else Decimal('1'))
                for e in ems
            )
            nb = len(ems)
        else:
            elements = list(module.elements.all())
            if not elements:
                continue
            somme = sum(
                (e.coefficient if e.coefficient else Decimal('1'))
                for e in elements
            )
            nb = len(elements)

        coeff_module = module.coefficient if module.coefficient else Decimal('0')
        if coeff_module != somme:
            anomalies.append({
                'module_id':              module.pk,
                'module_code':            module.code,
                'filiere_code':           module.filiere.code if module.filiere_id else '',
                'semestre_code':          (module.semestre.code_semestre or ''
                                           if module.semestre_id else ''),
                'coefficient_module':     coeff_module,
                'somme_coefficients_em':  somme,
                'nb_em':                  nb,
            })
    return anomalies


# Bornes (min, max) du nombre de modules actifs par semestre et par régime.
# Art. 8 Arrêté 562 (LP) : S1/S2/S3/S5 → 3 à 5 modules ; S4 → 1 module
# d'enseignement + 1 module stage (= 2) ; S6 → 1 seul module (stage).
# Art. 13-14 Décret 2018-070 (ING) : S1 à S5 → 3 à 5 modules ; S6 → 1 module
# (PFE) décomposé en 3 éléments.
REGLES_MODULES_PAR_SEMESTRE = {
    'LP':  {'S1': (3, 5), 'S2': (3, 5), 'S3': (3, 5),
            'S4': (2, 2), 'S5': (3, 5), 'S6': (1, 1)},
    'ING': {'S1': (3, 5), 'S2': (3, 5), 'S3': (3, 5),
            'S4': (3, 5), 'S5': (3, 5), 'S6': (1, 1)},
}

MAX_ELEMENTS_PAR_MODULE = 3   # Art. 8 Arrêté 562 / Art. 13 Décret 2018-070


def user_peut_depasser_max_elements(user) -> bool:
    """
    Exception au plafond de 3 éléments par module : seul un administrateur
    (is_superuser OU role='admin') peut ajouter un 4e élément. Les autres rôles
    (scolarité, DE, enseignant…) restent bloqués par Art. 8 / Art. 13.

    Hors contexte requête (shell, import, tests sans request) → user None →
    pas d'exception : le plafond s'applique.
    """
    return bool(user and (
        getattr(user, 'is_superuser', False)
        or getattr(user, 'role', None) == 'admin'
    ))


def verifier_structure_maquette(filiere, semestre_code=None) -> list[str]:
    """
    Contrôle de structure des maquettes — Art. 8 Arrêté 562 (LP) /
    Art. 13-14 Décret 2018-070 (ING) :
      1. nombre de modules actifs par semestre dans les bornes du régime ;
      2. au plus 3 éléments (EM planification OU ElementModule) par module ;
      3. ING S6 : le module PFE est décomposé en exactement 3 éléments (Art. 14).

    Non bloquant : retourne des messages. Les semestres SANS AUCUN module sont
    ignorés (maquette non commencée — la borne basse ne s'applique qu'aux
    semestres en cours de constitution). La borne crédits entiers est garantie
    par les types de champs (IntegerField).
    """
    from collections import defaultdict
    from apps.modules.models import Module
    from apps.em.models import EM

    if filiere is None:
        return []

    regime  = 'ING' if filiere.type_diplome == 'ING' else 'LP'
    regles  = REGLES_MODULES_PAR_SEMESTRE[regime]
    article = 'Art. 8 Arrêté 562' if regime == 'LP' else 'Art. 13-14 Décret 2018-070'

    qs = Module.objects.filter(actif=True, filiere=filiere).select_related('semestre')
    if semestre_code:
        qs = qs.filter(semestre__code_semestre__iexact=semestre_code)
    modules = list(qs)

    anomalies = []

    # 1 — Nombre de modules par semestre
    par_sem = defaultdict(list)
    for m in modules:
        code = (m.semestre.code_semestre or '').upper() if m.semestre_id else ''
        par_sem[code].append(m)

    for code, mods in sorted(par_sem.items()):
        bornes = regles.get(code)
        if bornes is None:
            continue
        lo, hi = bornes
        n = len(mods)
        if not (lo <= n <= hi):
            attendu = str(lo) if lo == hi else f'{lo} à {hi}'
            anomalies.append(
                f"Structure — {filiere.code} {code} : {n} module(s) actif(s), "
                f"attendu {attendu} ({article})."
            )

    # 2 — Au plus 3 éléments par module (+ ING S6 : exactement 3 — Art. 14)
    for m in modules:
        code = (m.semestre.code_semestre or '').upper() if m.semestre_id else ''
        nb_em = EM.objects.filter(module_lmd=m).count()
        nb_el = m.elements.count()
        nb = max(nb_em, nb_el)

        if nb > MAX_ELEMENTS_PAR_MODULE:
            anomalies.append(
                f"Structure — module {m.code} ({filiere.code} {code}) : "
                f"{nb} éléments, maximum {MAX_ELEMENTS_PAR_MODULE} ({article})."
            )
        elif regime == 'ING' and code == 'S6' and nb and nb != 3:
            anomalies.append(
                f"Structure — module PFE {m.code} ({filiere.code} S6) : "
                f"{nb} élément(s), le PFE est décomposé en 3 éléments "
                f"(Art. 14 Décret 2018-070)."
            )

    return anomalies


def verifier_minimum_deux_notes(session, filiere=None, semestre_code=None,
                                max_detail=5) -> list[str]:
    """
    Art. 12 Arrêté 562 / Art. 17 Décret 2018-070 : « L'évaluation de chaque
    élément de module doit faire l'objet d'un minimum de deux notes. »

    Contrôle non bloquant sur la session NORMALE : signale les EM pour lesquels
    une seule composante (CC / TP / EXAM) a été saisie sur toute la cohorte.
    Les EM sans aucune note sont ignorés (saisie pas commencée — autre sujet).
    La session de rattrapage est exclue : seul l'examen y est repassé
    (Art. 11 Arrêté 562 / Art. 16 Décret 2018-070).

    Le TP compte comme une note à part entière : le Décret (Art. 17) définit la
    moyenne comme « moyenne pondérée des notes de l'élément », sans restreindre
    les composantes.
    """
    if session is None or session.type_session != 'normale':
        return []

    from apps.evaluations.models import Note

    qs = Note.objects.filter(session=session)
    if filiere is not None:
        qs = qs.filter(
            inscription_element__inscription_ped__inscription_admin__filiere=filiere,
        )
    if semestre_code:
        qs = qs.filter(
            inscription_element__inscription_ped__semestre__code_semestre__iexact=semestre_code,
        )

    rows = qs.values_list(
        'inscription_element__em_id',
        'inscription_element__em__code_em',
        'type_note',
    ).distinct()

    types_par_em: dict[tuple, set] = {}
    for em_id, code_em, type_note in rows:
        if em_id is None:
            continue
        types_par_em.setdefault((em_id, code_em or f'EM#{em_id}'), set()).add(type_note)

    incomplets = sorted(
        ((code_em, types) for (_, code_em), types in types_par_em.items()
         if len(types) < 2),
        key=lambda x: x[0],
    )

    msgs = []
    for code_em, types in incomplets[:max_detail]:
        msgs.append(
            f"Saisie — EM {code_em} : une seule composante saisie "
            f"({', '.join(sorted(types))}). Minimum deux notes par élément "
            f"(Art. 12 Arrêté 562 / Art. 17 Décret 2018-070)."
        )
    if len(incomplets) > max_detail:
        msgs.append(
            f"Saisie — {len(incomplets) - max_detail} autre(s) EM avec une "
            f"seule composante saisie."
        )
    return msgs


def warnings_coefficients(anomalies, max_detail=5) -> list[str]:
    """Formate les anomalies en messages courts pour la réponse de peupler."""
    msgs = []
    for a in anomalies[:max_detail]:
        msgs.append(
            f"Maquette — module {a['module_code']} ({a['semestre_code']}) : "
            f"coefficient déclaré {a['coefficient_module']} ≠ somme des coefficients "
            f"de ses {a['nb_em']} EM ({a['somme_coefficients_em']}). La MGS pondère "
            f"ce module à {a['somme_coefficients_em']} (Art. 14 Arrêté 562 / "
            f"Art. 19 Décret 2018-070)."
        )
    if len(anomalies) > max_detail:
        msgs.append(
            f"Maquette — {len(anomalies) - max_detail} autre(s) module(s) "
            f"incohérent(s). Audit complet : python manage.py verifier_maquettes"
        )
    return msgs
