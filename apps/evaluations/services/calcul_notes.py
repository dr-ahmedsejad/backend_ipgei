"""
Service de calcul des notes LMD — conforme à l'Arrêté 562.

Règles métier :
  note_finale = CC * poids_cc + TP * poids_tp + EXAM * poids_exam
  est_valide  = note_finale >= 10 ET NOT est_eliminatoire
  est_eliminatoire = note_finale < element.seuil_eliminatoire (défaut 6/20)

  moy_module       = Σ(note_élément × coeff_EM) / Σ(coeff_EM)          [Art. 13]
  moyenne_semestre = Σ(moy_module × coeff_module) / Σ(coeff_module)    [Art. 15]
  credits_valides  = Σ(element.credits) pour les éléments validés
  est_admis        = moyenne_semestre >= 10 ET pas d'éliminatoire non rattrapé

Méthodes statiques (fonctions pures, sans effets de bord) :
  - appliquer_regle_maximum_rattrapage(ord, ratt)   Art. 18 + plafond 10 (conseil scientifique)
  - calculer_mention(moyenne)                        barème mention
  - calculer_progression_annuelle(...)               Art. 20 verrou S5
  - verifier_eligibilite_diplome(etudiant)           Art. 25

Usage :
  service = NoteCalculService(session)
  service.calculer_element(inscription_element)
  service.calculer_semestre(inscription_pedagogique)
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from apps.evaluations.models import Note, ResultatElement, ResultatSemestre
from apps.inscriptions.models import InscriptionElement, InscriptionPedagogique


def _calculer_me_em(cc, tp, exam, has_tp: bool, params) -> Decimal:
    """
    Calcule la note finale d'un EM a partir de ses composantes (CC, TP, EXAM)
    selon la ponderation institutionnelle.

    Strategie A : note absente (None) = 0/20 (= absent a l'epreuve).
    Retourne Decimal arrondi a 0.01 (ROUND_HALF_UP).

    Helper centralise reutilise par calculer_element() ET pv_enrichment.py
    pour garantir la coherence releve/PV/calcul officiel.
    """
    if has_tp:
        diviseur = params.coeff_cc + params.coeff_exam + params.coeff_tp
        note = (
            (cc   or Decimal('0')) * params.coeff_cc
            + (exam or Decimal('0')) * params.coeff_exam
            + (tp   or Decimal('0')) * params.coeff_tp
        ) / Decimal(str(diviseur))
    else:
        diviseur = params.coeff_cc + params.coeff_exam
        note = (
            (cc   or Decimal('0')) * params.coeff_cc
            + (exam or Decimal('0')) * params.coeff_exam
        ) / Decimal(str(diviseur))
    return note.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


class NoteCalculService:
    def __init__(self, session):
        self.session = session

    # ── Calcul élément ────────────────────────────────────────────────────────────

    @transaction.atomic
    def calculer_element(self, inscription_element: InscriptionElement) -> ResultatElement:
        """
        Calcule et persiste la note finale d'un element pour un etudiant / session.
        Idempotent : met a jour le ResultatElement s'il existe deja.

        Strategie A : si une note (CC/TP/EXAM) n'est pas saisie, elle est consideree
        comme 0/20 (= absent a l'examen). Conforme a la regle universitaire.

        Cas session de rattrapage (Art. 18) :
          - Charger les Notes de la session NORMALE correspondante (memo parite/annee/inst).
          - Recuperer CC et TP de la session normale (heritage : non re-saisis en SR).
          - Recuperer EXAM de la session de rattrapage.
          - Si pas d'EXAM en SR (etudiant n'a pas pris le rattrapage) : note_finale = note SN.
          - Sinon : me_rat = formule(CC_SN, TP_SN, EXAM_SR), note_finale = max(me_sn, me_rat).
        """
        from apps.evaluations.models import SessionEvaluation

        element   = inscription_element.element  # peut etre None
        em_planif = getattr(inscription_element, 'em', None)
        has_tp    = bool(getattr(em_planif, 'has_tp', False))

        # Ponderation institutionnelle (singleton)
        from apps.scolarite.models import ParametresPonderation
        params = ParametresPonderation.get()

        # 1. Localiser la session normale correspondante (utile pour SR ET pour SN)
        if self.session.type_session == 'rattrapage':
            session_normale = SessionEvaluation.objects.filter(
                annee_univ=self.session.annee_univ,
                type_semestre=self.session.type_semestre,
                type_session='normale',
                institution=self.session.institution,
            ).first()
        else:
            session_normale = self.session

        # 2. Charger les Notes SN
        notes_sn = {}
        if session_normale:
            for n in Note.objects.filter(
                inscription_element=inscription_element,
                session=session_normale,
            ):
                notes_sn[n.type_note] = n.valeur

        # 3. Note SN (toujours calculee car necessaire pour le max en SR)
        me_sn = _calculer_me_em(
            notes_sn.get('CC'), notes_sn.get('TP'), notes_sn.get('EXAM'),
            has_tp, params,
        )

        # 4. Note finale selon le type de session
        if self.session.type_session == 'rattrapage':
            # Charger les Notes SR de la session courante
            notes_sr = {}
            for n in Note.objects.filter(
                inscription_element=inscription_element,
                session=self.session,
            ):
                notes_sr[n.type_note] = n.valeur

            exam_sr = notes_sr.get('EXAM')
            if exam_sr is None:
                # Etudiant n'a pas pris le rattrapage → on retombe sur la note SN
                note_finale = me_sn
            else:
                # CC et TP herites de SN (cas standard universitaire)
                me_rat = _calculer_me_em(
                    notes_sn.get('CC'), notes_sn.get('TP'), exam_sr,
                    has_tp, params,
                )
                # Plafond rattrapage : valeur FIGEE sur la session courante
                # (None pour les sessions heritees -> aucun plafond).
                plafond = None
                if self.session.rattrapage_plafond_actif and self.session.rattrapage_plafond is not None:
                    plafond = self.session.rattrapage_plafond
                note_finale = self.appliquer_regle_maximum_rattrapage(me_sn, me_rat, plafond)
        else:
            note_finale = me_sn

        # 5. est_eliminatoire et est_valide
        seuil_elim = (
            element.seuil_eliminatoire if element is not None else None
        ) or (
            Decimal(str(em_planif.seuil_eliminatoire))
            if em_planif and em_planif.seuil_eliminatoire else Decimal('6')
        )
        est_eliminatoire = note_finale < seuil_elim
        est_valide       = note_finale >= Decimal('10') and not est_eliminatoire

        # 6. Persistance
        resultat, _ = ResultatElement.objects.update_or_create(
            inscription_element=inscription_element,
            session=self.session,
            defaults={
                'note_finale':      note_finale,
                'est_valide':       est_valide,
                'est_eliminatoire': est_eliminatoire,
            },
        )
        return resultat

    # ── Calcul semestre ───────────────────────────────────────────────────────────

    @transaction.atomic
    def calculer_semestre(self, inscription_ped: InscriptionPedagogique) -> ResultatSemestre:
        """
        Calcule et persiste la moyenne générale semestrielle (MGS) — Arrêté 562 Art. 14.

        Formule : MGS = Σ(ME × coeff_EM) / Σ(coeff_EM)  sur tous les éléments du semestre.
        Identique au relevé de notes officiel (documents/services.py).

        Art. 15 : admis si MGS ≥ 10 ET tous modules ≥ 8 ET pas d'éliminatoire.
        ResultatModule utilisé uniquement pour le contrôle Art. 15 (tous MM ≥ 8).
        """
        from apps.evaluations.models import ResultatModule as RM

        # ── Contrôle Art. 15 : tous modules ≥ 8 ────────────────────────────────
        modules_resultats = list(
            RM.objects.filter(
                inscription_ped=inscription_ped,
                session=self.session,
            ).select_related('module')
        )
        if modules_resultats:
            tous_modules_ok = all(rm.moyenne >= Decimal('8') for rm in modules_resultats)
            a_elim_modules  = any(rm.a_eliminatoire for rm in modules_resultats)
            credits_valides = sum(rm.credits_valides for rm in modules_resultats)
        else:
            tous_modules_ok = True
            a_elim_modules  = False
            credits_valides = 0

        # ── MGS = Σ(ME × coeff_EM) / Σ(coeff_EM) — Art. 14 ────────────────────
        elements_inscrits = InscriptionElement.objects.filter(
            inscription_ped=inscription_ped,
        ).select_related('element', 'em')

        # Index ResultatElement par inscription_element_id pour cette session
        re_index = {
            r.inscription_element_id: r
            for r in ResultatElement.objects.filter(
                session=self.session,
                inscription_element__inscription_ped=inscription_ped,
            )
        }

        total_pondere      = Decimal('0')
        total_coeff        = Decimal('0')
        a_eliminatoire     = False
        credits_from_elems = 0

        for insc_el in elements_inscrits:
            resultat = re_index.get(insc_el.pk)
            if resultat is None:
                continue
            coeff = (
                insc_el.element.coefficient if insc_el.element and insc_el.element.coefficient else None
            ) or (
                Decimal(str(insc_el.em.coefficient)) if insc_el.em and insc_el.em.coefficient else None
            ) or Decimal('1')
            total_pondere += resultat.note_finale * coeff
            total_coeff   += coeff
            if resultat.est_eliminatoire:
                a_eliminatoire = True
            if resultat.est_valide:
                credits_el = (
                    insc_el.element.credits if insc_el.element and insc_el.element.credits else None
                ) or (insc_el.em.credits if insc_el.em and insc_el.em.credits else 0)
                credits_from_elems += credits_el

        moyenne = (total_pondere / total_coeff).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP,
        ) if total_coeff else Decimal('0')

        a_eliminatoire = a_eliminatoire or a_elim_modules

        # Art. 15 : admis si MGS ≥ 10 ET tous modules ≥ 8 ET pas d'éliminatoire
        est_admis = (
            moyenne >= Decimal('10')
            and tous_modules_ok
            and not a_eliminatoire
        )

        # code_statut semestre — uniquement V ou NV (simplification)
        code_sem = 'V' if est_admis else 'NV'

        # Rafraîchir codes VCS sur modules/éléments AVANT calcul final des crédits
        # → permet aux VCS d'acquérir leurs crédits (Art. 15)
        from apps.evaluations.services.calcul_module import ResultatModuleService
        svc_mod = ResultatModuleService(self.session)
        svc_mod.rafraichir_codes_apres_semestre(inscription_ped, est_admis)

        # Calcul final des credits acquis (Art. 12 + Art. 13 + Art. 15) :
        #  - Module valide (V direct ou VCS apres compensation) → tous ses EM acquierent leurs credits
        #  - Module non valide → seuls les EM individuellement valides (note ≥ 10, Art. 12) acquierent
        # On parcourt les ResultatElement APRES rafraichir (codes V/VCI/VCS/NV poses).
        # est_valide=True sur un RE = EM acquis (Art. 12 ou Art. 13).
        re_index_refreshed = {
            r.inscription_element_id: r
            for r in ResultatElement.objects.filter(
                session=self.session,
                inscription_element__inscription_ped=inscription_ped,
            )
        }
        credits_valides_final = 0
        for insc_el in elements_inscrits:
            re_ = re_index_refreshed.get(insc_el.pk)
            if re_ and re_.est_valide:
                credits_el = (
                    insc_el.element.credits if insc_el.element and insc_el.element.credits else None
                ) or (insc_el.em.credits if insc_el.em and insc_el.em.credits else 0)
                credits_valides_final += credits_el

        resultat_sem, _ = ResultatSemestre.objects.update_or_create(
            inscription_ped=inscription_ped,
            session=self.session,
            defaults={
                'moyenne':         moyenne,
                'credits_valides': credits_valides_final,
                'est_admis':       est_admis,
                'code_statut':     code_sem,
            },
        )

        return resultat_sem

    # ── Calcul en lot ─────────────────────────────────────────────────────────────

    def calculer_tous_modules_session(self):
        """
        Calcule ResultatModule pour toute la session.
        Doit être appelé APRÈS calculer_tous_elements_session().
        """
        from apps.evaluations.services.calcul_module import ResultatModuleService
        svc = ResultatModuleService(self.session)
        return svc.calculer_tous_modules_session()

    def calculer_tous_semestres_session(self):
        """
        Pour chaque InscriptionPedagogique ayant des ResultatElement dans cette session,
        calcule la moyenne pondérée du semestre et crée/met à jour le ResultatSemestre.
        Doit être appelé APRÈS calculer_tous_modules_session().
        """
        from apps.inscriptions.models import InscriptionPedagogique

        insc_peds = InscriptionPedagogique.objects.filter(
            inscriptions_elements__resultats__session=self.session,
        ).distinct().select_related('inscription_admin', 'semestre')

        resultats = []
        for insc_ped in insc_peds:
            resultats.append(self.calculer_semestre(insc_ped))
        return resultats

    def calculer_tous_elements_session(self):
        """
        Calcule les résultats éléments pour toutes les inscriptions de la session.
        Filtre par parité de semestre (Impairs → I, Pairs → P) et par année universitaire.
        """
        # Mapping SessionEvaluation.type_semestre → Semestre.type_semestre
        TYPE_MAP = {'Impairs': 'I', 'Pairs': 'P'}
        sem_type = TYPE_MAP.get(self.session.type_semestre, 'I')

        elements = InscriptionElement.objects.filter(
            inscription_ped__semestre__type_semestre=sem_type,
            inscription_ped__inscription_admin__annee_univ=self.session.annee_univ,
        ).select_related('element', 'inscription_ped')

        resultats = []
        for insc_el in elements:
            r = self.calculer_element(insc_el)
            if r is not None:
                resultats.append(r)
        return resultats

    # ── Méthodes statiques (fonctions pures) ──────────────────────────────────────

    @staticmethod
    def appliquer_regle_maximum_rattrapage(
        moy_ordinaire: Decimal,
        moy_rattrapage: Decimal,
        plafond=None,
    ) -> Decimal:
        """
        Art. 18 — Session de rattrapage : l'étudiant garde la note la plus favorable.

        Si `plafond` est fourni (décision du conseil scientifique, paramétrable et
        FIGÉ par session), un élément validé GRÂCE au rattrapage est plafonné à
        cette valeur. `plafond=None` → aucun plafond (Art. 18 pur).

        - Déjà validé en session normale (moy_ordinaire >= 10) → note la plus
          favorable, sans plafond (acquis dès la 1re session).
        - Échoué en normale puis validé par le rattrapage (favorable >= 10)
          → min(favorable, plafond).
        - Toujours non validé après rattrapage (favorable < 10) → note la plus
          favorable, inchangée.

        Retourne la note retenue.
        """
        favorable = max(moy_ordinaire, moy_rattrapage)
        if plafond is not None and moy_ordinaire < Decimal('10') and favorable >= Decimal('10'):
            return min(favorable, plafond)
        return favorable

    @staticmethod
    def calculer_mention(moyenne: Decimal) -> str:
        """
        Barème de mentions conforme à l'arrêté.
        Retourne 'Très Bien', 'Bien', 'Assez Bien', 'Passable' ou 'Insuffisant'.
        """
        if moyenne >= Decimal('16'):
            return 'Très Bien'
        if moyenne >= Decimal('14'):
            return 'Bien'
        if moyenne >= Decimal('12'):
            return 'Assez Bien'
        if moyenne >= Decimal('10'):
            return 'Passable'
        return 'Insuffisant'

    @staticmethod
    def calculer_progression_annuelle(
        credits_filiere: int,
        credits_capitalises: int,
        s1_s2_valides: bool,
        demande_s5: bool = False,
    ) -> dict:
        """
        Art. 20 — Règle de progression :
          - L'étudiant peut passer à l'année suivante s'il a validé >= 65 % des crédits.
          - Verrou S5 : l'accès au semestre 5 est bloqué si S1 et S2 ne sont pas validés.

        Paramètres :
          credits_filiere      : total des crédits de l'année en cours
          credits_capitalises  : crédits effectivement capitalisés par l'étudiant
          s1_s2_valides        : True si S1 ET S2 sont validés (pour le verrou S5)
          demande_s5           : True si l'étudiant tente de s'inscrire en S5

        Retourne :
          {
            'peut_progresser'   : bool,
            'taux_capitalisation': float,   # en %
            'bloque_s5'         : bool,
            'motif'             : str,
          }
        """
        taux = (credits_capitalises / credits_filiere * 100) if credits_filiere else 0
        peut_progresser = taux >= 65
        bloque_s5 = demande_s5 and not s1_s2_valides

        if bloque_s5:
            motif = 'Accès au semestre 5 bloqué : S1 et S2 doivent être validés (Art. 20).'
        elif not peut_progresser:
            motif = (
                f'Progression bloquée : {taux:.1f} % des crédits capitalisés '
                f'(seuil requis : 65 %).'
            )
        else:
            motif = 'Progression autorisée.'

        return {
            'peut_progresser':    peut_progresser and not bloque_s5,
            'taux_capitalisation': round(taux, 2),
            'bloque_s5':          bloque_s5,
            'motif':              motif,
        }

    @staticmethod
    def _parite_semestre(semestre) -> str | None:
        """
        Retourne 'Impairs' (S1/S3/S5) ou 'Pairs' (S2/S4/S6) ou None si indeterminable.
        Utilise le code_semestre (ex 'S1', 'S3') pour deduire la parite.
        """
        code = (semestre.code_semestre or '').upper()
        digits = ''.join(c for c in code if c.isdigit())
        if not digits:
            return None
        return 'Impairs' if (int(digits) % 2 == 1) else 'Pairs'

    @staticmethod
    def _selectionner_rs_consolide(insc_ped, annee_univ, institution, type_semestre):
        """
        Regle metier de consolidation des sessions pour un semestre :
          1. ResultatSemestre de session RATTRAPAGE CLOTUREE si elle existe
             (Art. 18 propage : note finale = max(SN, SR))
          2. Sinon : ResultatSemestre de session NORMALE (la plus recente)

        Retourne None si aucun ResultatSemestre trouve.
        Filtrage explicite par institution et parite de semestre.
        """
        from apps.evaluations.models import ResultatSemestre

        qs = ResultatSemestre.objects.filter(
            inscription_ped=insc_ped,
            session__annee_univ=annee_univ,
            session__institution=institution,
        )
        if type_semestre:
            qs = qs.filter(session__type_semestre=type_semestre)

        # Priorite 1 : rattrapage cloturee
        rs = qs.filter(
            session__type_session='rattrapage',
            session__est_close=True,
        ).order_by('-session__id').first()
        if rs is not None:
            return rs

        # Priorite 2 : normale (cloturee ou non, la plus recente)
        return qs.filter(
            session__type_session='normale',
        ).order_by('-session__id').first()

    @staticmethod
    def calculer_moyenne_annuelle(etudiant, annee_univ, niveau) -> tuple:
        """
        Moyenne annuelle CONSOLIDÉE — MÊME moteur que le relevé et la consultation
        (documents.services.calculer_resultat_semestre_consolide).

        Pour chaque EM, la note retenue est max(SN, SR) (Art. 18) APPLIQUÉE MÊME SI
        LE RATTRAPAGE N'EST PAS ENCORE CLÔTURÉ, puis compensation Art. 12-15 et
        report des crédits capitalisés des années antérieures (via la consolidation
        multi-IP). Le résultat de la délibération reflète donc EXACTEMENT le relevé
        en toute situation — c'est un choix explicite (cf. demande métier).

        IMPORTANT : tant que les sessions ne sont pas clôturées, ce résultat est
        PROVISOIRE. L'avertissement correspondant est porté séparément
        (pv_diagnostic._diagnostic_sessions_attendues / DeliberationAnnuelleService).

        Retourne (moyenne_annuelle: Decimal, credits_valides: int, tous_valides: bool).
        """
        from apps.inscriptions.models import (
            InscriptionAdministrative, InscriptionPedagogique,
        )
        # Import paresseux : la consolidation vit dans documents.services (même
        # source unique que le relevé/consultation). Lazy pour éviter tout cycle.
        from apps.documents.services import calculer_resultat_semestre_consolide

        # Toutes les inscriptions de l'étudiant à ce niveau jusqu'à l'année délibérée
        # (un redoublant peut ne pas avoir réinscrit un semestre déjà acquis).
        ia_qs = InscriptionAdministrative.objects.filter(
            etudiant=etudiant, niveau=niveau,
            annee_univ__annee__lte=annee_univ.annee,
        )
        if not ia_qs.exists():
            return Decimal('0'), 0, False

        # Semestres DU NIVEAU uniquement (S{2n-1}, S{2n}). Un redoublant peut
        # porter, sous son inscription de niveau N, des semestres d'un niveau
        # INFÉRIEUR (ex. rattrapage de L1 — S1/S2 — sous l'inscription L2) : ces
        # semestres ne doivent PAS gonfler les crédits/moyenne ANNUELS du niveau
        # (sinon total > 60 crédits et passage_droit accordé à tort). On borne
        # donc aux codes du niveau délibéré.
        codes_niveau = {f'S{2 * niveau - 1}', f'S{2 * niveau}'}
        semestres = {}
        for ip in (
            InscriptionPedagogique.objects
            .filter(inscription_admin__in=ia_qs)
            .select_related('semestre')
            .order_by('semestre__code_semestre')
        ):
            sem = ip.semestre
            if (sem and ip.semestre_id not in semestres
                    and sem.code_semestre in codes_niveau):
                semestres[ip.semestre_id] = sem
        if not semestres:
            return Decimal('0'), 0, False

        total_pondere = Decimal('0')
        total_credits = Decimal('0')
        credits_valides = 0
        tous_valides = True
        found = False

        for sem in semestres.values():
            res = calculer_resultat_semestre_consolide(etudiant, sem, annee_univ)
            moy = res.get('moyenne_semestre')
            if moy is None:
                tous_valides = False
                continue
            found = True
            sem_credits = Decimal(str(sem.credits or 30))
            total_pondere += Decimal(str(moy)) * sem_credits
            total_credits += sem_credits
            credits_valides += res.get('credits_valides', 0)
            if not res.get('est_admis'):
                tous_valides = False

        if not found or total_credits == 0:
            return Decimal('0'), 0, False

        moyenne_annuelle = (total_pondere / total_credits).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP,
        )
        return moyenne_annuelle, credits_valides, tous_valides

    @staticmethod
    def verifier_eligibilite_diplome(etudiant, annee_univ=None) -> dict:
        """
        Art. 25 — Éligibilité au diplôme de Licence (LF/LP) :
          1. 180 crédits capitalisés.
          2. PFE (S6) noté >= 12/20.
          3. Tous les semestres (S1 à S6) ont un PV clos.

        Paramètre `annee_univ` : optionnel, accepté pour compatibilité avec les
        appelants (ex. DeliberationAnnuelleIngenieur). Le décompte des 180 crédits
        porte sur l'ensemble du cursus, donc il n'est pas borné à une seule année.

        Retourne :
          {
            'eligible'          : bool,
            'credits_valides'   : int,
            'pfe_valide'        : bool | None,
            'semestres_clos'    : list[str],
            'semestres_ouverts' : list[str],
            'motifs'            : list[str],
          }
        """
        from apps.inscriptions.models import InscriptionAdministrative, InscriptionPedagogique
        from apps.evaluations.models import ResultatSemestre, PVDeliberation

        motifs = []

        # 1 — Crédits capitalisés (Art. 25)
        # Un ResultatSemestre existe par session (normale ET rattrapage) :
        # additionner toutes les lignes est_admis double-compte chaque semestre
        # validé en SN puis re-validé en SR (180 → 360 crédits → diplôme indu).
        # On consolide donc à UN seul RS par semestre, via la même règle que
        # calculer_moyenne_annuelle : rattrapage clôturé prioritaire (max SN/SR
        # déjà propagé, Art. 18), sinon normale. credits_valides reflète la
        # capitalisation modulaire (Art. 13), comptée même hors semestre validé.
        inscriptions_ped = (
            InscriptionPedagogique.objects
            .filter(inscription_admin__etudiant=etudiant)
            .select_related('inscription_admin', 'semestre')
        )

        credits_total = 0
        for ip in inscriptions_ped:
            insc_admin = ip.inscription_admin
            rs = NoteCalculService._selectionner_rs_consolide(
                insc_ped=ip,
                annee_univ=insc_admin.annee_univ,
                institution=insc_admin.institution,
                type_semestre=NoteCalculService._parite_semestre(ip.semestre),
            )
            if rs is not None:
                credits_total += rs.credits_valides
        if credits_total < 180:
            motifs.append(f'Crédits insuffisants : {credits_total}/180.')

        # 2 — Note du semestre final (S6) >= 12/20
        # Art. 25 Arrêté 562 (LP : moyenne >= 12 au 6e semestre — stage de fin
        # de formation) / Art. 16 Décret 2018-070 (ING : note PFE >= 12).
        # Source primaire : le ResultatSemestre CONSOLIDÉ du S6 (rattrapage
        # clôturé prioritaire, sinon normale). La moyenne annuelle du PV
        # niveau 3 mélange S5+S6 : elle n'est utilisée qu'en dernier recours.
        from apps.evaluations.models import LigneDeliberation
        pfe_valide = None
        note_s6 = None
        try:
            ip_s6 = (
                inscriptions_ped
                .filter(semestre__code_semestre__iexact='S6')
                .order_by('-inscription_admin__annee_univ__annee')
                .first()
            )
            if ip_s6:
                rs_s6 = NoteCalculService._selectionner_rs_consolide(
                    insc_ped=ip_s6,
                    annee_univ=ip_s6.inscription_admin.annee_univ,
                    institution=ip_s6.inscription_admin.institution,
                    type_semestre=NoteCalculService._parite_semestre(ip_s6.semestre),
                )
                if rs_s6 is not None:
                    note_s6 = rs_s6.moyenne

            if note_s6 is None:
                # Dernier recours (aucun ResultatSemestre S6) : moyenne de la
                # ligne de délibération niveau 3 — approximation S5+S6.
                ligne_s6 = LigneDeliberation.objects.filter(
                    inscription_admin__etudiant=etudiant,
                    pv__niveau=3,
                ).order_by('-pv__date_deliberation').first()
                if ligne_s6 and ligne_s6.moyenne_annuelle is not None:
                    note_s6 = ligne_s6.moyenne_annuelle

            if note_s6 is not None:
                pfe_valide = note_s6 >= Decimal('12')
                if not pfe_valide:
                    motifs.append(
                        f'Note du semestre final (S6) insuffisante : {note_s6}/20 '
                        f'(seuil : 12/20 — Art. 25 Arrêté 562 / Art. 16 Décret 2018-070).'
                    )
        except Exception:
            pass

        # 3 — PVs clos pour tous les semestres
        pvs = PVDeliberation.objects.filter(
            lignes__inscription_admin__etudiant=etudiant,
        ).distinct()

        semestres_clos    = [str(pv) for pv in pvs if pv.est_clos]
        semestres_ouverts = [str(pv) for pv in pvs if not pv.est_clos]

        if semestres_ouverts:
            motifs.append(
                f'PV non clos : {", ".join(semestres_ouverts)}.'
            )

        eligible = not motifs

        return {
            'eligible':           eligible,
            'credits_valides':    credits_total,
            'pfe_valide':         pfe_valide,
            'note_s6':            note_s6,
            'semestres_clos':     semestres_clos,
            'semestres_ouverts':  semestres_ouverts,
            'motifs':             motifs,
        }
