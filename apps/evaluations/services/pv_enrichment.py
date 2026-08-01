"""
Helper d'enrichissement des donnees d'un PV de deliberation pour generation
PDF / Excel.

Pour chaque etudiant et chaque module du PV, retourne la liste des elements
de module (EM) avec :
  - Notes intermediaires CC / TP / EXAM (session normale uniquement)
  - Note finale ME pour la session normale (me_sn) et pour le rattrapage (me_sr)
  - Note retenue (me_retenue) selon Art. 18 (max SN/SR si SR cloturee)
  - Source de la note retenue ('SN' ou 'SR') pour indicateur visuel

REGLE CRITIQUE : me_sr = None si l'etudiant n'a PAS passe le rattrapage pour
cet EM (= aucune Note saisie en session SR). Eviter de confondre "SR=SN
parce que pas passee" avec "SR effective = SN".

Utilise par PVDeliberationViewSet.pdf et .excel pour eliminer la duplication
de logique d'enrichissement.
"""
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP


def _calculer_me_rattrapage(notes_sn, notes_sr, has_tp, params_pond) -> Decimal | None:
    """
    Calcule la note brute de rattrapage : CC et TP herites de la session
    NORMALE + EXAM de la session de rattrapage.

    Conforme a l'usage universitaire mauritanien : au rattrapage, l'etudiant
    ne refait que l'examen ; CC et TP de la session normale sont conserves.

    Retourne None si aucun EXAM n'est saisi en SR (= etudiant n'a pas pris
    le rattrapage). Sinon : note finale Decimal arrondie a 0.01 ROUND_HALF_UP.

    Reutilise _calculer_me_em() de calcul_notes.py pour garantir la coherence
    avec le calcul officiel persiste (ResultatElement.note_finale).
    """
    exam_sr = notes_sr.get('EXAM') if notes_sr else None
    if exam_sr is None:
        return None
    cc = notes_sn.get('CC') if notes_sn else None
    tp = notes_sn.get('TP') if notes_sn else None
    from apps.evaluations.services.calcul_notes import _calculer_me_em
    return _calculer_me_em(cc, tp, exam_sr, has_tp, params_pond)


def _parite_semestre(semestre) -> str | None:
    """Retourne 'Impairs' (S1/S3/S5) ou 'Pairs' (S2/S4/S6) ou None."""
    code = (getattr(semestre, 'code_semestre', '') or '').upper()
    digits = ''.join(c for c in code if c.isdigit())
    if not digits:
        return None
    return 'Impairs' if (int(digits) % 2 == 1) else 'Pairs'


def enrichir_lignes_pv(pv) -> tuple[list[dict], dict]:
    """
    Enrichit les lignes du PV avec les donnees consolidees SN/SR par EM.

    Retourne (lignes_enrichies, meta) ou :
      - lignes_enrichies : liste de dicts {ligne, modules: [...]}
      - meta : {
          'has_sr_par_parite': {'Impairs': bool, 'Pairs': bool},
          'parites_incluses': ['Impairs', 'Pairs'] | ['Impairs'] | ['Pairs'],
        }

    Format de chaque module dans modules :
      {
        'session_code': str,    # code de la session de reference (la retenue)
        'parite':       str,    # 'Impairs' ou 'Pairs'
        'module':       Module,
        'moyenne':      Decimal,
        'credits':      int,
        'code_statut':  str,
        'elements': [
          {
            'code': str, 'intitule': str, 'coeff': Decimal | None,
            'cc': Decimal | None, 'tp': Decimal | None, 'exam': Decimal | None,
            'me_sn': Decimal | None, 'me_sr': Decimal | None,
            'code_statut_sn': str | None, 'code_statut_sr': str | None,
            'me_retenue': Decimal | None, 'source_retenue': 'SN' | 'SR' | None,
            'me': Decimal | None,         # alias = me_retenue (compat template)
            'code_statut': str,            # code de la session retenue
          }, ...
        ],
      }
    """
    from apps.evaluations.models import (
        SessionEvaluation, ResultatElement, ResultatModule, Note,
    )
    from apps.inscriptions.models import (
        InscriptionPedagogique, InscriptionElement,
    )
    from apps.scolarite.models import ParametresPonderation
    params_pond = ParametresPonderation.get()

    # 1. Determiner les parites a inclure
    # Pour PV semestriel : la parite de la session du PV
    # Pour PV annuel : les 2 parites
    if pv.type_pv == 'semestriel' and pv.session_id:
        parites_incluses = [pv.session.type_semestre]
        # PV semestriel : annee_univ direct est NULL, prendre celle de la session
        annee_univ = pv.session.annee_univ
    else:
        parites_incluses = ['Impairs', 'Pairs']
        annee_univ = pv.annee_univ

    # 2. Cartographier les sessions (parite, type_session) -> SessionEvaluation
    sessions_qs = SessionEvaluation.objects.filter(
        annee_univ=annee_univ,
        institution=pv.institution,
        type_semestre__in=parites_incluses,
    )
    sessions_par_parite = defaultdict(dict)  # {parite: {'normale': S, 'rattrapage': S}}
    for s in sessions_qs:
        sessions_par_parite[s.type_semestre][s.type_session] = s

    has_sr_par_parite = {
        p: 'rattrapage' in sessions_par_parite.get(p, {}) for p in parites_incluses
    }

    # 3. Charger les lignes du PV
    lignes = pv.lignes.select_related(
        'inscription_admin__etudiant',
        'inscription_admin',
    ).all()

    lignes_enrichies = []

    # ── Statut CONSOLIDÉ pour l'AFFICHAGE seulement (export PDF/Excel) ────────
    # Reconnaît la compensation/capitalisation (y compris cross-année) qui n'est
    # PAS reflétée dans le ResultatElement brut de session. NE MODIFIE RIEN en
    # base : on ne fait que corriger le statut affiché du PV semestriel.
    # 1 calcul de consolidation par (étudiant, semestre, année), mémoïsé.
    from apps.documents.services import calculer_resultat_semestre_consolide
    _conso_cache: dict = {}

    def _conso_maps(etudiant, semestre, annee):
        """Maps consolidées mémoïsées par (étudiant, semestre, année) :
          [0] elements_acquis {code_em: bool}
          [1] modules_valides {code_module: bool}
          [2] me_par_em       {code_em: float | None}      # ME retenue du relevé
          [3] moyenne_par_mod {code_module: float | None}  # moyenne module du relevé
        Le relevé PDF et la consultation écran lisent CES MÊMES valeurs
        (calculer_resultat_semestre_consolide) ; les afficher ici garantit que le
        PV == relevé (Art. 18 max SN/SR, compensation Art. 12-15, report inter-années,
        consolidation redoublement) au lieu du ResultatElement/ResultatModule stocké."""
        key = (etudiant.id, getattr(semestre, 'id', semestre), getattr(annee, 'id', annee))
        if key not in _conso_cache:
            elems, mods, me_em, moy_mod = {}, {}, {}, {}
            try:
                res = calculer_resultat_semestre_consolide(etudiant, semestre, annee)
                for mod in res.get('modules', []):
                    mc = mod.get('code')
                    if mc:
                        mods[mc] = (mod.get('decision') == 'Validé')
                        moy_mod[mc] = mod.get('note_module')
                    for e in mod.get('elements', []):
                        ec = e.get('code')
                        elems[ec] = bool(e.get('est_valide')) or e.get('decision') == 'Validé'
                        me_em[ec] = e.get('me')   # peut être None (aucune note saisie)
            except Exception:
                pass
            _conso_cache[key] = (elems, mods, me_em, moy_mod)
        return _conso_cache[key]

    def _acquis_consolide(etudiant, em, annee):
        if not em or not getattr(em, 'semestre_id', None) or not annee:
            return None
        return _conso_maps(etudiant, em.semestre, annee)[0].get(em.code_em)

    def _module_valide_consolide(etudiant, module, semestre, annee):
        if not module or not getattr(module, 'code', None) or not semestre or not annee:
            return None
        return _conso_maps(etudiant, semestre, annee)[1].get(module.code)

    def _me_consolide(etudiant, em, annee):
        """(present, me) — present=False → EM absent de la consolidation :
        on garde alors la valeur stockée (aucun blanchiment défensif)."""
        if not em or not getattr(em, 'semestre_id', None) or not annee:
            return (False, None)
        m = _conso_maps(etudiant, em.semestre, annee)[2]
        if em.code_em in m:
            return (True, m[em.code_em])
        return (False, None)

    def _moyenne_module_consolide(etudiant, module, semestre, annee):
        """(present, moyenne) — même sémantique de fallback que _me_consolide."""
        if not module or not getattr(module, 'code', None) or not semestre or not annee:
            return (False, None)
        m = _conso_maps(etudiant, semestre, annee)[3]
        if module.code in m:
            return (True, m[module.code])
        return (False, None)

    for ligne in lignes:
        ia = ligne.inscription_admin
        modules_par_etudiant = []

        for parite in parites_incluses:
            sn = sessions_par_parite.get(parite, {}).get('normale')
            sr = sessions_par_parite.get(parite, {}).get('rattrapage')
            if not sn and not sr:
                continue

            # Trouver l'InscriptionPedagogique pour cette parite
            # NB: Semestre.type_semestre stocke 'I'/'P' (court),
            # SessionEvaluation.type_semestre stocke 'Impairs'/'Pairs' (long)
            sem_type_court = 'I' if parite == 'Impairs' else 'P'
            insc_ped = InscriptionPedagogique.objects.filter(
                inscription_admin=ia,
                semestre__type_semestre=sem_type_court,
            ).first()
            if not insc_ped:
                continue

            # Charger en une fois tous les InscriptionElement de cet etudiant
            ie_list = list(InscriptionElement.objects.filter(
                inscription_ped=insc_ped,
            ).select_related('em__module_lmd'))
            ie_ids = [ie.pk for ie in ie_list]

            # Notes intermediaires (CC/TP/EXAM) pour SN et SR
            # Cles : ie_id -> {'CC': val, 'TP': val, 'EXAM': val}
            notes_sn_index = {}
            if sn:
                for n in Note.objects.filter(
                    inscription_element_id__in=ie_ids, session=sn,
                ):
                    notes_sn_index.setdefault(n.inscription_element_id, {})[n.type_note] = n.valeur

            notes_sr_index = {}
            if sr:
                for n in Note.objects.filter(
                    inscription_element_id__in=ie_ids, session=sr,
                ):
                    notes_sr_index.setdefault(n.inscription_element_id, {})[n.type_note] = n.valeur

            # ResultatElement pour SN (utilise pour code_statut SN)
            re_sn_index = {}
            if sn:
                for re_ in ResultatElement.objects.filter(
                    inscription_element_id__in=ie_ids, session=sn,
                ):
                    re_sn_index[re_.inscription_element_id] = re_

            # ResultatElement pour SR (utilise pour code_statut SR — le note_finale
            # contient le max, mais on s'appuie sur les Notes SR pour la note brute)
            re_sr_index = {}
            if sr:
                for re_ in ResultatElement.objects.filter(
                    inscription_element_id__in=ie_ids, session=sr,
                ):
                    re_sr_index[re_.inscription_element_id] = re_

            # ResultatModule pour SN et SR — pour selectionner le module consolide
            rm_sn_index = {}
            if sn:
                for rm in ResultatModule.objects.filter(
                    inscription_ped=insc_ped, session=sn,
                ).select_related('module'):
                    rm_sn_index[rm.module_id] = rm
            rm_sr_index = {}
            if sr:
                for rm in ResultatModule.objects.filter(
                    inscription_ped=insc_ped, session=sr,
                ).select_related('module'):
                    rm_sr_index[rm.module_id] = rm

            # Liste unifiee des modules
            module_ids = set(rm_sn_index.keys()) | set(rm_sr_index.keys())
            for module_id in sorted(
                module_ids,
                key=lambda mid: (
                    rm_sn_index.get(mid) or rm_sr_index.get(mid)
                ).module.code,
            ):
                # Choix du ResultatModule consolide :
                # priorite SR cloturee > SN
                rm_sr = rm_sr_index.get(module_id)
                rm_sn = rm_sn_index.get(module_id)
                if rm_sr and sr and sr.est_close:
                    rm_used = rm_sr
                    src_session = sr
                else:
                    rm_used = rm_sn or rm_sr
                    src_session = sn or sr

                if not rm_used:
                    continue

                # Construire la liste des EMs de ce module
                elems_data = []
                for ie in ie_list:
                    if not ie.em or ie.em.module_lmd_id != module_id:
                        continue

                    re_sn = re_sn_index.get(ie.pk)
                    re_sr = re_sr_index.get(ie.pk)
                    nts_sn = notes_sn_index.get(ie.pk, {})
                    nts_sr = notes_sr_index.get(ie.pk, {})

                    # me_sn : note finale SN (depuis ResultatElement, deja calculee)
                    me_sn = re_sn.note_finale if re_sn else None

                    # me_sr : note brute du rattrapage = formule(CC_SN, TP_SN, EXAM_SR)
                    # Conforme au releve PDF + calcul officiel post-correction CC/TP herites.
                    # Retourne None si etudiant n'a pas pris le rattrapage (pas d'EXAM SR).
                    has_tp = bool(getattr(ie.em, 'has_tp', False)) if ie.em else False
                    me_sr = _calculer_me_rattrapage(nts_sn, nts_sr, has_tp, params_pond)

                    # Plafond rattrapage FIGE sur la session SR : un EM valide GRACE
                    # au rattrapage (echoue en SN <10, reussi en SR >=10) voit sa note
                    # plafonnee — aligne le releve sur ResultatElement.note_finale et
                    # le calcul officiel (appliquer_regle_maximum_rattrapage).
                    if (me_sr is not None and sr is not None
                            and sr.rattrapage_plafond_actif and sr.rattrapage_plafond is not None
                            and (me_sn is None or me_sn < Decimal('10'))
                            and me_sr >= Decimal('10')):
                        me_sr = min(me_sr, sr.rattrapage_plafond)

                    # Determiner la note retenue (Art. 18) :
                    # - Si SR cloturee ET etudiant a passe le rattrapage (me_sr != None)
                    #   ET me_sr > me_sn → SR retenue
                    # - Sinon (pas de rattrapage, ou rattrapage <= SN, ou SR ouverte) → SN
                    if (sr and sr.est_close
                            and me_sr is not None
                            and (me_sn is None or me_sr > me_sn)):
                        me_retenue = me_sr
                        source_retenue = 'SR'
                    elif me_sn is not None:
                        me_retenue = me_sn
                        source_retenue = 'SN'
                    elif me_sr is not None:
                        me_retenue = me_sr
                        source_retenue = 'SR'
                    else:
                        me_retenue = None
                        source_retenue = None

                    # Determiner code_statut consolide :
                    # Toujours prendre RE(SR) si SR cloturee + RE SR existe
                    # (rafraichir_codes_apres_semestre a propage VCI/VCS sur RE_SR
                    # apres calcul du semestre SR admis — c'est le statut de reference).
                    # Sinon retomber sur RE_SN.
                    if sr and sr.est_close and re_sr:
                        code_statut_consolide = re_sr.code_statut or ''
                    elif re_sn:
                        code_statut_consolide = re_sn.code_statut or ''
                    elif re_sr:
                        code_statut_consolide = re_sr.code_statut or ''
                    else:
                        code_statut_consolide = ''

                    # Lecture est_valide depuis ResultatElement de la session retenue
                    re_for_valide = re_sr if (sr and sr.est_close and re_sr) else re_sn
                    em_est_valide = bool(re_for_valide.est_valide) if re_for_valide else False

                    # Override AFFICHAGE : EM acquis au relevé CONSOLIDÉ
                    # (compensation/capitalisation cross-année) mais montré « NV »
                    # en brut de session → on l'affiche validé, comme le relevé.
                    # N'écrit RIEN ; ne fait que corriger l'affichage du PV.
                    if _acquis_consolide(ia.etudiant, ie.em, annee_univ):
                        em_est_valide = True
                        if code_statut_consolide not in ('V', 'VCI', 'VCS'):
                            # Dériver le VRAI statut au lieu d'un VCI systématique :
                            #   note EM ≥ 10       → V   (validation directe, ex. réussite au rattrapage)
                            #   sinon module ≥ 10  → VCI (compensation intra-module, Art. 13)
                            #   sinon              → VCS (compensation semestrielle, Art. 14)
                            _pm, _me_c = _me_consolide(ia.etudiant, ie.em, annee_univ)
                            _note_em = _me_c if _pm else me_retenue
                            _pmoy, _moy_c = _moyenne_module_consolide(
                                ia.etudiant, rm_used.module, insc_ped.semestre, annee_univ)
                            _moy_mod = _moy_c if _pmoy else rm_used.moyenne
                            if _note_em is not None and _note_em >= 10:
                                code_statut_consolide = 'V'
                            elif _moy_mod is not None and _moy_mod >= 10:
                                code_statut_consolide = 'VCI'
                            else:
                                code_statut_consolide = 'VCS'

                    # Alignement AFFICHAGE de la ME retenue sur le relevé consolidé
                    # (même moteur : max SN/SR, plafond, consolidation redoublement).
                    # Corrige la divergence PV↔relevé pour les redoublants (le stocké
                    # ne compte que la ré-inscription courante). Fallback stocké si
                    # l'EM est absent de la consolidation.
                    _present_me, _me_conso = _me_consolide(ia.etudiant, ie.em, annee_univ)
                    if _present_me:
                        me_retenue = _me_conso

                    em_credits    = ie.em.credits if (ie.em and ie.em.credits) else 0

                    elems_data.append({
                        'code':            getattr(ie.em, 'code_em', '—'),
                        'intitule':        getattr(ie.em, 'intitule', '—'),
                        'coeff':           ie.em.coefficient if ie.em else None,
                        'em_credits':      em_credits,
                        'cc':              nts_sn.get('CC'),
                        'tp':              nts_sn.get('TP'),
                        'exam':            nts_sn.get('EXAM'),
                        'exam_rat':        nts_sr.get('EXAM'),  # Note d'examen brute RAT (sans formule)
                        'me_sn':           me_sn,
                        'me_sr':           me_sr,
                        'code_statut_sn':  re_sn.code_statut if re_sn else None,
                        'code_statut_sr':  re_sr.code_statut if re_sr else None,
                        'me_retenue':      me_retenue,
                        'source_retenue':  source_retenue,
                        'me':              me_retenue,  # alias compat template
                        'code_statut':     code_statut_consolide or '',
                        'est_valide':      em_est_valide,
                    })

                # Credits acquis par EMs Art. 12+13 (somme des EMs est_valide=True du module)
                # Si module valide entier (V) : tous EM non-eliminatoires acquis (Art. 13)
                # Si module non valide : seuls EM individuellement valides (Art. 12)
                credits_acquis_em = sum(
                    e['em_credits'] for e in elems_data if e['est_valide'] and e['em_credits']
                )

                # Statut MODULE consolidé (AFFICHAGE) : un module validé par
                # compensation semestrielle/cross-année (Art. 14-15) reste 'NV' sur
                # le ResultatModule brut de session. On l'affiche validé comme le
                # relevé. N'écrit RIEN ; corrige seulement l'affichage du PV.
                mod_code_statut = rm_used.code_statut or ''
                mod_est_valide  = rm_used.est_valide
                if _module_valide_consolide(ia.etudiant, rm_used.module, insc_ped.semestre, annee_univ):
                    mod_est_valide = True
                    if mod_code_statut not in ('V', 'VCI', 'VCS'):
                        mod_code_statut = 'V'

                # Alignement AFFICHAGE de la moyenne module sur le relevé consolidé
                # (Strategie A + compensation) : élimine l'écart PV↔relevé (redoublants
                # + arrondi). Fallback moyenne stockée si module absent de la conso.
                _present_moy, _moy_conso = _moyenne_module_consolide(
                    ia.etudiant, rm_used.module, insc_ped.semestre, annee_univ)
                moyenne_affichee = _moy_conso if _present_moy else rm_used.moyenne

                modules_par_etudiant.append({
                    'session_code':         src_session.code if src_session else '',
                    'parite':               parite,
                    'semestre_code':        insc_ped.semestre.code_semestre or '',  # S1, S2, S3, S4, S5, S6
                    'module':               rm_used.module,
                    'moyenne':              moyenne_affichee,
                    'credits':              credits_acquis_em,        # Art. 12 + Art. 13
                    'credits_module_total': rm_used.credits_valides,  # ancien : credits du module entier
                    'code_statut':          mod_code_statut,
                    'est_valide':           mod_est_valide,
                    'elements':             elems_data,
                })

        lignes_enrichies.append({
            'ligne':   ligne,
            'modules': modules_par_etudiant,
        })

    # Tri par matricule croissant (numerique si possible, alphabetique sinon)
    def _matricule_key(item):
        m = ((item['ligne'].inscription_admin.etudiant.matricule or '')
             if item['ligne'].inscription_admin and item['ligne'].inscription_admin.etudiant
             else '')
        try:
            return (0, int(m))
        except (ValueError, TypeError):
            return (1, m)
    lignes_enrichies.sort(key=_matricule_key)

    meta = {
        'has_sr_par_parite': has_sr_par_parite,
        'parites_incluses':  parites_incluses,
        # Pour le template : True si au moins une parite a une SR
        'has_sr_global':     any(has_sr_par_parite.values()),
    }
    return lignes_enrichies, meta
