"""Helpers pour le calcul du numero de semaine pedagogique par departement.

Contexte :
    Departement.decalage_impair et Departement.decalage_pair representent le
    nombre de semaines de cours sautees au debut de chaque semestre.

    Cas typique :
      - Impair : formation militaire L1 = decalage_impair = 3
      - Pair   : decalage_pair = 0 (par defaut)

    Cas plus rares :
      - Impair : redoublement, retard administratif
      - Pair   : stage de demarrage L3, examens reportes...

Architecture :
    - Le numero `numero_semaine` en BD reste toujours GLOBAL.
    - L'AFFICHAGE peut etre converti en numero pedagogique local au dept
      pour les pages qui montrent un seul dept/semestre a la fois.
    - Les FILTRES utilisateurs peuvent etre exprimes en numerotation locale
      et traduits en global avant la query SQL.

Aucune mutation de donnees : ces helpers sont en lecture seule.
"""


def _decalage_for_dept(dept, type_semestre):
    """Retourne le decalage applicable au dept pour ce type de semestre.

    Resilience : si le dept est None ou ne porte pas les champs, retourne 0.
    """
    if dept is None:
        return 0
    if type_semestre == 'I':
        return getattr(dept, 'decalage_impair', 0) or 0
    if type_semestre == 'P':
        return getattr(dept, 'decalage_pair', 0) or 0
    return 0


def numero_pedagogique_dept(numero_global, dept, type_semestre):
    """Convertit un numero global en numero pedagogique pour un dept.

    Args:
        numero_global: numero de semaine en BD (1, 2, 3, ...) ou None.
        dept: instance Departement (utilise decalage_impair ou decalage_pair
              selon type_semestre).
        type_semestre: 'I' (Impair) ou 'P' (Pair).

    Returns:
        - None si numero_global est None.
        - None si la semaine tombe avant le demarrage du dept (n < 1).
        - Sinon : numero_global - decalage_effectif (entier >= 1).

    Exemples :
        # L1 Impair (decalage_impair=3) :
        numero_pedagogique_dept(5, dept_L1, 'I')  # -> 2
        numero_pedagogique_dept(3, dept_L1, 'I')  # -> None (formation militaire)

        # L1 Pair (decalage_pair=0) :
        numero_pedagogique_dept(5, dept_L1, 'P')  # -> 5

        # L3 Pair avec stage demarrage (decalage_pair=2) :
        numero_pedagogique_dept(5, dept_L3, 'P')  # -> 3

        # L2 sans aucun decalage :
        numero_pedagogique_dept(5, dept_L2, 'I')  # -> 5
    """
    if numero_global is None:
        return None
    decalage = _decalage_for_dept(dept, type_semestre)
    n = numero_global - decalage
    return n if n >= 1 else None


def numero_global_from_local(numero_local, dept, type_semestre):
    """Convertit un numero pedagogique local en numero global pour query SQL.

    Args:
        numero_local: numero saisi par l'utilisateur dans son referentiel local.
        dept: instance Departement.
        type_semestre: 'I' ou 'P'.

    Returns:
        int : numero global a utiliser dans WHERE numero_semaine = ...

    Exemples :
        # L1 Impair (decalage_impair=3) : "Semaine 2 L1" = global S5
        numero_global_from_local(2, dept_L1, 'I')  # -> 5

        # L1 Pair (decalage_pair=0) : "Semaine 2 L1" = global S2
        numero_global_from_local(2, dept_L1, 'P')  # -> 2

        # L3 Pair avec decalage_pair=2 : "Semaine 3 L3" = global S5
        numero_global_from_local(3, dept_L3, 'P')  # -> 5
    """
    if numero_local is None:
        return None
    decalage = _decalage_for_dept(dept, type_semestre)
    return int(numero_local) + decalage


def decalage_for_code_semestre(code_semestre, type_semestre):
    """Deduit le decalage applicable a partir d'un code semestre.

    Utilise par les pages filtrees par semestre (ex: fiches-individuelles)
    pour appliquer le decalage sans connaitre le dept exact.

    Convention :
        Cherche le decalage MAX (parmi decalage_impair ou decalage_pair selon
        type_semestre) chez les dept qui partagent ce code_semestre. Si plusieurs
        dept du meme code ont des decalages differents (cas rare), on prend
        le max par securite (eviter de rater des semaines pre-demarrage).

    Args:
        code_semestre: ex 'S1', 'S2', 'S3'.
        type_semestre: 'I' ou 'P'.

    Returns:
        int : decalage en semaines (0 si aucun dept du niveau n'a de decalage).

    Exemples :
        decalage_for_code_semestre('S1', 'I')  # -> 1 (L1 a decalage_impair=1)
        decalage_for_code_semestre('S2', 'P')  # -> 0 (L1 Pair par defaut)
        decalage_for_code_semestre('S3', 'I')  # -> 0 (L2 sans decalage)
    """
    if not code_semestre:
        return 0
    # Import differe pour eviter cycle d'import au chargement du module
    from django.db.models import Max
    from apps.departement.models import Departement
    from apps.parametres.models import Semestre

    # Le lien Departement <-> Semestre passe par Niveau :
    #   Semestre.niveau_semestre (FK Niveau)
    #   Departement.niveau       (FK Niveau)
    niveau_ids = (Semestre.objects
                  .filter(code_semestre=code_semestre, type_semestre=type_semestre)
                  .values_list('niveau_semestre_id', flat=True)
                  .distinct())
    if not niveau_ids:
        return 0

    field_name = 'decalage_impair' if type_semestre == 'I' else 'decalage_pair'
    result = (Departement.objects
              .filter(niveau_id__in=list(niveau_ids), **{f'{field_name}__gt': 0})
              .aggregate(max_dec=Max(field_name)))
    return result['max_dec'] or 0
