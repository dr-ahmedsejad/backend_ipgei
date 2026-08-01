"""
Utilitaires partagés pour le module inscriptions.
"""


def creer_inscriptions_pedagogiques(inscription_admin, user):
    """
    Crée automatiquement les InscriptionPedagogique et InscriptionElement
    pour une InscriptionAdministrative donnée.

    Chaîne : Niveau → Semestres génériques → Module LMD (filière) → EM → InscriptionElement

    Depuis la simplification Section 1 du plan institution_V1, les Semestres sont
    génériques (sans filiere ni annee_univ) — un seul Semestre par (niveau × parité).
    L'année et la filière sont portées par inscription_admin.

    Idempotent : utilise get_or_create à chaque étape.
    """
    from apps.parametres.models import Semestre
    from apps.modules.models import Module
    from apps.em.models import EM
    from apps.inscriptions.models import InscriptionPedagogique, InscriptionElement

    filiere    = inscription_admin.filiere
    niveau_int = inscription_admin.niveau

    # ── 1. Trouver les Semestres génériques du niveau ───────────────────────
    semestres = list(
        Semestre.objects
        .filter(niveau_semestre__niveau__icontains=f'L{niveau_int}')
        .select_related('niveau_semestre')
    )

    # Fallback : convention 'L{n}' non trouvée → n-ième Niveau par ordre alphabétique
    if not semestres:
        from apps.parametres.models import Niveau
        try:
            niveau_obj = Niveau.objects.order_by('niveau')[niveau_int - 1]
        except IndexError:
            return
        semestres = list(Semestre.objects.filter(niveau_semestre=niveau_obj))

    if not semestres:
        return

    # ── 2. Pour chaque semestre → InscriptionPedagogique ────────────────────
    for semestre in semestres:
        insc_ped, _ = InscriptionPedagogique.objects.get_or_create(
            inscription_admin=inscription_admin,
            semestre=semestre,
            defaults={'validee_par': user},
        )

        # ── 3. Modules LMD pour ce semestre + filière ───────────────────────
        modules = Module.objects.filter(
            filiere=filiere,
            semestre=semestre,
            actif=True,
        )

        for module in modules:
            # ── 4. EM liés à ce module → InscriptionElement ─────────────────
            ems = EM.objects.filter(module_lmd=module)
            for em_obj in ems:
                InscriptionElement.objects.get_or_create(
                    inscription_ped=insc_ped,
                    em=em_obj,
                )
