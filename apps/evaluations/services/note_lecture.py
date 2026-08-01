"""Lectures du domaine notes (feuilles/agrégats) extraites des vues."""
from rest_framework import status
from rest_framework.response import Response

from apps.evaluations.models import Note, ResultatElement, SessionEvaluation
from .note_access import peut_acceder_em


def em_acquis_consolide(etudiant, em, annee_univ) -> bool:
    """
    True si l'EM est ACQUIS (validé directement OU par compensation / capitalisation,
    y compris CROSS-ANNÉE) pour l'étudiant à l'année donnée, selon le relevé
    CONSOLIDÉ (`calculer_resultat_semestre_consolide` — même moteur que le relevé,
    la délibération et l'éligibilité diplôme : source UNIQUE de vérité).

    Robuste au cas où le `ResultatElement` BRUT par session reste 'NV' alors que
    l'EM est en réalité acquis : ex. un élément < 10 dont le MODULE est validé
    grâce à un AUTRE élément du module validé une année antérieure (compensation
    Art. 13) — invisible au calcul par session, mais reconnu par la consolidation.
    """
    if not em or not getattr(em, 'semestre_id', None):
        return False
    from apps.documents.services import calculer_resultat_semestre_consolide
    try:
        res = calculer_resultat_semestre_consolide(etudiant, em.semestre, annee_univ)
    except Exception:
        return False
    for mod in res.get('modules', []):
        for e in mod.get('elements', []):
            if e.get('code') == em.code_em:
                # 'decision' = statut rétropropagé (Validé pour les VCI/VCS) ;
                # 'est_valide' = note ≥ 10 (chemin rapide).
                return bool(e.get('est_valide')) or e.get('decision') == 'Validé'
    return False


def statut_em_consolide(etudiant, em, annee_univ) -> str:
    """Statut CONSOLIDÉ d'un EM : 'V' / 'VCI' / 'VCS' / 'E' / 'NV' / '' (inconnu).

    Distingue, au sein du relevé consolidé, la VALIDATION DIRECTE (V, note ≥ 10),
    la compensation INTRA-MODULE (VCI : module ≥ 10, élément < 10) et la
    compensation SEMESTRIELLE (VCS : module 8-10 relevé par un semestre validé).
    Sert à l'exception « rattrapage VCS/VCI » : on ne peut ouvrir le rattrapage
    facultatif d'un EM compensé que si on connaît la NATURE de la compensation.
    """
    if not em or not getattr(em, 'semestre', None) or annee_univ is None:
        return ''
    from apps.documents.services import calculer_resultat_semestre_consolide
    try:
        res = calculer_resultat_semestre_consolide(etudiant, em.semestre, annee_univ)
    except Exception:
        return ''
    for mod in res.get('modules', []):
        for e in mod.get('elements', []):
            if e.get('code') == em.code_em:
                if e.get('est_eliminatoire'):
                    return 'E'
                if e.get('est_valide'):
                    return 'V'                       # note ≥ 10 : validation directe
                if e.get('decision') == 'Validé':
                    nm = mod.get('note_module')
                    return 'VCI' if (nm is not None and nm >= 10) else 'VCS'
                return 'NV'
    return ''


def _exception_vcs_vci(session):
    """Renvoie (vcs_actif, vci_actif) pour une session de rattrapage.

    Le drapeau d'exception est posé sur la session NORMALE (cf. commandes
    activer_rattrapage_vcs/vci) ; on le lit donc sur la SN de même année + parité
    + institution — sans oublier la session reçue elle-même. SOURCE UNIQUE
    utilisée par la voie obligation ET la voie dette (pour qu'elles ne divergent pas)."""
    from apps.evaluations.models import SessionEvaluation
    sn = SessionEvaluation.objects.filter(
        annee_univ=session.annee_univ,
        type_semestre=session.type_semestre,
        type_session='normale',
        institution_id=session.institution_id,
    ).first()
    vcs = bool(getattr(session, 'rattrapage_vcs_actif', False) or (sn and sn.rattrapage_vcs_actif))
    vci = bool(getattr(session, 'rattrapage_vci_actif', False) or (sn and sn.rattrapage_vci_actif))
    return vcs, vci


def dette_ie_ids_non_valides(em_id, session):
    """
    InscriptionElement de DETTE (est_dette=True) NON VALIDÉS pour un EM donné,
    dans l'année et la parité de la session.

    Pourquoi : une dette ramenée à un niveau supérieur est rangée sous une
    InscriptionPedagogique d'un semestre inférieur (ex. S1 dans une inscription
    L2). `generer_obligations` ne tourne que sur le PV du semestre courant
    (S3/S4) → la dette S1 n'a jamais d'ObligationRattrapage. Conséquence :
    l'étudiant figure en session NORMALE (feuille pilotée par l'inscription)
    mais PAS en RATTRAPAGE (feuille pilotée par l'obligation). Ce helper
    rétablit la cohérence SN <-> SR pour ces dettes.

    Scoping strict (aucun effet de bord) :
      - est_dette=True uniquement (les EM normaux restent pilotés par l'obligation) ;
      - NON VALIDÉ : `est_valide=False` (capte NV, E ET le statut non encore
        caractérisé d'un étudiant absent en SN — note_finale=0, code vide) ;
      - un EM VALIDÉ est EXCLU : `est_valide=True` ET, par sécurité (anomalie
        possible où VCI a est_valide=False), exclusion explicite des codes
        validés V/VCI/VCS → l'année/le cas de validation n'est JAMAIS touché ;
      - année courante (session.annee_univ) → les années antérieures où l'EM a
        pu être validé ne sont JAMAIS touchées ;
      - parité de la session (S1↔Impairs, S2↔Pairs).
    """
    vcs_actif, vci_actif = _exception_vcs_vci(session)

    _base = dict(
        inscription_element__em_id=em_id,
        inscription_element__est_dette=True,
        inscription_element__inscription_ped__inscription_admin__annee_univ=session.annee_univ,
        session__type_session='normale',
        session__type_semestre=session.type_semestre,
    )
    # Dettes NON validées en normale (NV/E/absent) → à rattraper.
    candidate_ids = set(
        ResultatElement.objects.filter(**_base, est_valide=False)
        .exclude(code_statut__in=['V', 'VCI', 'VCS'])
        .values_list('inscription_element_id', flat=True)
    )
    # Exception VCS/VCI : ajouter AUSSI les dettes dont le code STOCKÉ est déjà
    # VCS/VCI (écartées par l'exclude ci-dessus) quand la session ouvre le rattrapage
    # de ce type — sinon ces cas (ex. 24609/ST11 stocké VCS) échappent à la
    # condition 3. Le statut consolidé est revérifié dans la boucle.
    _codes_exc = (['VCS'] if vcs_actif else []) + (['VCI'] if vci_actif else [])
    if _codes_exc:
        candidate_ids |= set(
            ResultatElement.objects.filter(**_base, code_statut__in=_codes_exc)
            .values_list('inscription_element_id', flat=True)
        )
    candidate_ids = list(candidate_ids)
    if not candidate_ids:
        return []

    # Garde-fou CONSOLIDÉ + exception « validé au rattrapage EN COURS » :
    #  - un EM acquis SEULEMENT par compensation/capitalisation (jamais validé en
    #    propre, ResultatElement brut 'NV') est EXCLU — sinon l'étudiant figure à
    #    tort au rattrapage (cas 23631/23620) ;
    #  - MAIS une dette VALIDÉE via le rattrapage EN COURS (résultat dans la session
    #    SR de cette année+parité, code V/VCI/VCS ou est_valide) RESTE VISIBLE dans
    #    la feuille, avec sa note saisie (cas 23629/HE31) — symétrie avec la voie
    #    obligation, qui conserve déjà ces cas.
    from apps.inscriptions.models import InscriptionElement
    from apps.evaluations.models import SessionEvaluation
    from django.db.models import Q
    ies = InscriptionElement.objects.filter(id__in=candidate_ids).select_related(
        'em', 'em__semestre', 'inscription_ped__inscription_admin__etudiant',
    )
    sr_ids = list(SessionEvaluation.objects.filter(
        annee_univ=session.annee_univ,
        type_semestre=session.type_semestre,
        type_session='rattrapage',
    ).values_list('id', flat=True))
    gardes = []
    for ie in ies:
        etu = ie.inscription_ped.inscription_admin.etudiant
        if not em_acquis_consolide(etu, ie.em, session.annee_univ):
            gardes.append(ie.id)            # 1. vraie dette non acquise → à rattraper
            continue
        valide_au_rattrapage = bool(sr_ids) and ie.resultats.filter(
            session_id__in=sr_ids,
        ).filter(Q(code_statut__in=['V', 'VCI', 'VCS']) | Q(est_valide=True)).exists()
        if valide_au_rattrapage:
            gardes.append(ie.id)            # 2. validé via le rattrapage en cours → visible
            continue
        # 3. Exception VCS/VCI (cas 24603/ST11) : si la session ouvre le rattrapage
        # facultatif de ce type, une dette acquise par compensation de CE type reste
        # rattrapable (note plafonnée). Sinon (compensation seule, hors exception) → exclue.
        if vcs_actif or vci_actif:
            st = statut_em_consolide(etu, ie.em, session.annee_univ)
            if (st == 'VCS' and vcs_actif) or (st == 'VCI' and vci_actif):
                gardes.append(ie.id)
    return gardes


def eligible_rattrapage_ie_ids(em_id, session):
    """
    Ensemble des InscriptionElement ÉLIGIBLES au rattrapage pour un EM, dans une
    session de rattrapage : possède une ObligationRattrapage (même année + parité)
    OU est une dette non validée (cf. dette_ie_ids_non_valides).

    SOURCE UNIQUE partagée par la feuille de saisie (build_feuille) ET l'import
    (note_saisie.do_importer) : la liste affichée et les écritures acceptées restent
    cohérentes — un étudiant non concerné ne peut ni apparaître dans la feuille, ni
    recevoir une note SR par import.
    """
    from apps.evaluations.models import ObligationRattrapage
    obl_ids = list(ObligationRattrapage.objects.filter(
        inscription_element__em_id=em_id,
        ligne__pv__session__annee_univ=session.annee_univ,
        ligne__pv__session__type_semestre=session.type_semestre,
    ).values_list('inscription_element_id', flat=True))

    # Exception « rattrapage VCS/VCI » figée par session (drapeau lu sur la SN —
    # source unique partagée avec la voie dette via _exception_vcs_vci).
    vcs_actif, vci_actif = _exception_vcs_vci(session)

    # Garde-fou CONSOLIDÉ sur les obligations : retirer celles dont l'EM est acquis
    # UNIQUEMENT par compensation/capitalisation (jamais validé dans sa PROPRE
    # session) — il n'y a rien à rattraper. On CONSERVE :
    #   - les EM validés via le rattrapage EN COURS (statut brut V/VCI/VCS/est_valide) ;
    #   - les vraies obligations non acquises ;
    #   - EXCEPTION : si la session ouvre le rattrapage facultatif VCS et/ou VCI, un EM
    #     acquis par compensation de CE type (au consolidé) RESTE rattrapable (la note
    #     reste plafonnée). Sinon (par défaut, drapeaux à False) → exclu comme avant.
    if obl_ids:
        from apps.inscriptions.models import InscriptionElement
        gardes = []
        for ie in InscriptionElement.objects.filter(id__in=obl_ids).select_related(
            'em', 'em__semestre', 'inscription_ped__inscription_admin__etudiant',
        ):
            etu = ie.inscription_ped.inscription_admin.etudiant
            valide_propre = (
                ie.resultats.filter(code_statut__in=['V', 'VCI', 'VCS', 'R']).exists()
                or ie.resultats.filter(est_valide=True).exists()
            )
            if (not valide_propre) and em_acquis_consolide(etu, ie.em, session.annee_univ):
                garder_exception = False
                if vcs_actif or vci_actif:
                    st = statut_em_consolide(etu, ie.em, session.annee_univ)
                    garder_exception = (st == 'VCS' and vcs_actif) or (st == 'VCI' and vci_actif)
                if not garder_exception:
                    continue  # acquis par compensation seule, hors exception → exclu
            gardes.append(ie.id)
        obl_ids = gardes

    return set(obl_ids) | set(dette_ie_ids_non_valides(em_id, session))


def build_feuille(request):
    """
    GET ?session=X&em=Y
    Retourne une ligne par InscriptionElement inscrit à cet EM (planification),
    avec les notes CC/TP/EXAM existantes agrégées.
    """
    from apps.inscriptions.models import InscriptionElement
    from apps.evaluations.models import SessionEvaluation
    session_id = request.query_params.get('session')
    em_id      = request.query_params.get('em')
    if not session_id or not em_id:
        return Response(
            {'detail': 'Paramètres session et em requis.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        session = SessionEvaluation.objects.get(pk=session_id)
    except SessionEvaluation.DoesNotExist:
        return Response({'detail': 'Session introuvable.'}, status=status.HTTP_404_NOT_FOUND)

    # Scope enseignant : un prof ne consulte la feuille que de SES EMs.
    if not peut_acceder_em(request.user, em_id):
        return Response({'detail': 'Accès non autorisé à cet EM.'}, status=status.HTTP_403_FORBIDDEN)

    is_ratt = session.type_session == 'rattrapage'

    if is_ratt:
        # Session de rattrapage : uniquement les étudiants ÉLIGIBLES — obligation
        # OU dette non validée. MÊME ensemble que celui imposé à l'écriture par
        # l'import (eligible_rattrapage_ie_ids = source unique).
        ie_ids = list(eligible_rattrapage_ie_ids(em_id, session))
        inscriptions = (
            InscriptionElement.objects
            .filter(id__in=ie_ids)
            .select_related('inscription_ped__inscription_admin__etudiant')
            .order_by('inscription_ped__inscription_admin__etudiant__matricule')
        )
    else:
        # Session normale : les inscrits à l'EM POUR L'ANNÉE DE LA SESSION.
        # Sans ce filtre annee_univ, un EM réutilisé d'une année sur l'autre
        # ferait remonter les étudiants des promotions précédentes ET
        # dupliquerait les redoublants (1 ligne ancienne année + 1 ligne dette
        # année courante). Le filtre par année de la session écarte les deux.
        inscriptions = (
            InscriptionElement.objects
            .filter(em_id=em_id,
                    inscription_ped__inscription_admin__annee_univ=session.annee_univ)
            .select_related('inscription_ped__inscription_admin__etudiant')
            .order_by('inscription_ped__inscription_admin__etudiant__matricule')
        )

    # Notes existantes indexées par (inscription_element_id, type_note)
    notes_qs = Note.objects.filter(
        session_id=session_id,
        inscription_element__em_id=em_id,
    )
    notes_index: dict[tuple, dict] = {}
    for n in notes_qs:
        notes_index[(n.inscription_element_id, n.type_note)] = {
            'id': n.id, 'valeur': float(n.valeur),
        }

    def cell(insc_id, type_note):
        return notes_index.get((insc_id, type_note), {'id': None, 'valeur': None})

    rows = []
    for ie in inscriptions:
        etudiant = ie.inscription_ped.inscription_admin.etudiant
        rows.append({
            'inscription_element': ie.id,
            'etudiant_nom':        etudiant.nom,
            'etudiant_matricule':  etudiant.matricule,
            'cc':   cell(ie.id, 'CC'),
            'tp':   cell(ie.id, 'TP'),
            'exam': cell(ie.id, 'EXAM'),
        })

    return Response(rows)


def build_agrege(request):
    """
    GET /api/v1/evaluations/notes/agrege/?session=X[&filiere=Y&page=N&page_size=10]
    Retourne une ligne agrégée par InscriptionElement (une par étudiant × EM),
    avec les colonnes CC / TP / EXAM et la note finale calculée.
    Seuls les InscriptionElement ayant au moins une note dans la session sont retournés.
    """
    import math
    from apps.inscriptions.models import InscriptionElement

    session_id = request.query_params.get('session')
    filiere_id = request.query_params.get('filiere')
    em_id      = request.query_params.get('em')
    try:
        page_num  = max(1, int(request.query_params.get('page', 1)))
        page_size = max(1, min(200, int(request.query_params.get('page_size', 10))))
    except (ValueError, TypeError):
        page_num, page_size = 1, 10

    if not session_id:
        return Response(
            {'detail': 'Paramètre session requis.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # InscriptionElements ayant au moins une note dans la session
    qs = (
        InscriptionElement.objects
        .filter(notes__session_id=session_id)
        .distinct()
        .select_related(
            'inscription_ped__inscription_admin__etudiant',
            'em',
        )
        .order_by(
            'inscription_ped__inscription_admin__etudiant__nom',
            'em__code_em',
        )
    )

    if filiere_id:
        qs = qs.filter(
            inscription_ped__inscription_admin__filiere_id=filiere_id,
        )

    if em_id:
        qs = qs.filter(em_id=em_id)

    count  = qs.count()
    pages  = math.ceil(count / page_size) if count else 1
    offset = (page_num - 1) * page_size
    items  = list(qs[offset:offset + page_size])

    insc_ids = [ie.id for ie in items]

    # Notes index : (inscription_element_id, type_note) → valeur (session courante)
    notes_index: dict[tuple, float] = {}
    for n in Note.objects.filter(
        session_id=session_id,
        inscription_element_id__in=insc_ids,
    ):
        notes_index[(n.inscription_element_id, n.type_note)] = float(n.valeur)

    # En session de RATTRAPAGE, l'étudiant ne repasse que l'EXAM : CC et TP sont
    # hérités de la session NORMALE correspondante (même année/parité/institution).
    # Sans ça, les colonnes CC/TP s'affichent vides. Aligné sur le calcul officiel.
    cc_tp_index = notes_index
    session_obj = SessionEvaluation.objects.filter(pk=session_id).first()
    if session_obj and session_obj.type_session == 'rattrapage':
        sn = SessionEvaluation.objects.filter(
            annee_univ=session_obj.annee_univ,
            type_semestre=session_obj.type_semestre,
            type_session='normale',
            institution=session_obj.institution,
        ).first()
        if sn:
            cc_tp_index = {}
            for n in Note.objects.filter(
                session_id=sn.id,
                inscription_element_id__in=insc_ids,
                type_note__in=['CC', 'TP'],
            ):
                cc_tp_index[(n.inscription_element_id, n.type_note)] = float(n.valeur)

    # ResultatElement index
    resultats_index: dict[int, ResultatElement] = {}
    for r in ResultatElement.objects.filter(
        session_id=session_id,
        inscription_element_id__in=insc_ids,
    ):
        resultats_index[r.inscription_element_id] = r

    # Statut CONSOLIDÉ (compensation/capitalisation, y compris cross-année) par
    # EM — 1 calcul de consolidation par (étudiant, semestre, année), mémoïsé.
    from apps.documents.services import calculer_resultat_semestre_consolide
    _conso_cache: dict = {}

    def _acquis(etu, em_, annee):
        if not em_ or not em_.semestre_id or not annee:
            return None
        key = (etu.id, em_.semestre_id, annee.id)
        if key not in _conso_cache:
            m = {}
            try:
                res = calculer_resultat_semestre_consolide(etu, em_.semestre, annee)
                for mod in res.get('modules', []):
                    for e in mod.get('elements', []):
                        m[e.get('code')] = bool(e.get('est_valide')) or e.get('decision') == 'Validé'
            except Exception:
                m = {}
            _conso_cache[key] = m
        return _conso_cache[key].get(em_.code_em)

    rows = []
    for ie in items:
        etudiant = ie.inscription_ped.inscription_admin.etudiant
        em       = ie.em
        resultat = resultats_index.get(ie.id)
        annee    = ie.inscription_ped.inscription_admin.annee_univ
        # est_valide CONSOLIDÉ (repli sur le brut de session si EM absent du consolidé)
        _acq = _acquis(etudiant, em, annee)
        est_valide = _acq if _acq is not None else (resultat.est_valide if resultat else False)
        rows.append({
            'id':                  ie.id,
            'inscription_element': ie.id,
            'etudiant':            etudiant.id,
            'etudiant_nom':        etudiant.nom,
            'etudiant_matricule':  etudiant.matricule,
            'element':             em.id if em else None,
            'element_code':        em.code_em if em else '',
            'element_nom':         em.intitule if em else '',
            'session':             int(session_id),
            'note_cc':             cc_tp_index.get((ie.id, 'CC')),
            'note_tp':             cc_tp_index.get((ie.id, 'TP')),
            'note_exam':           notes_index.get((ie.id, 'EXAM')),
            'note_finale':         float(resultat.note_finale) if resultat else None,
            'est_valide':          est_valide,
            'est_absent':          False,
            # Champs EM pour le calcul client-side de la NFE
            'credits':             em.credits if em else None,
            'coefficient':         em.coefficient if em else None,
            'has_tp':              bool(em and (em.TP or 0) > 0),
        })

    return Response({'count': count, 'pages': pages, 'results': rows})
