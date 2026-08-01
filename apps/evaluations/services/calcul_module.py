"""
Service de calcul des résultats par module — Art. 13 de l'Arrêté 562.

Règles métier :
  moyenne_module = Σ(note_finale_élément × coefficient) / Σ(coefficient)
  est_valide     = moyenne ≥ 10 ET pas d'éliminatoire dans le module

  Compensation intra-module (Art. 13) :
    Si module validé ET un élément < 10 (mais ≥ 6) → élément = VCI
    Si module validé ET tous éléments ≥ 10 → chaque élément = V

  Codes provisoires (affinés par NoteCalculService.calculer_semestre) :
    module ≥ 10 + élément < 10  → élément = VCI
    module 8-10                  → élément = NV  (→ VCS si semestre valide)
    module < 8                   → élément = NVO
    élément < 6                  → élément = E

Usage :
  svc = ResultatModuleService(session)
  svc.calculer(inscription_ped, module)                  # un module
  svc.calculer_tous_modules_semestre(inscription_ped)    # tous les modules
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from apps.evaluations.models import ResultatElement, ResultatModule


class ResultatModuleService:
    def __init__(self, session):
        self.session = session

    # ── Calcul d'un module ───────────────────────────────────────────────────

    @transaction.atomic
    def calculer(self, inscription_ped, module) -> ResultatModule:
        """
        Calcule et persiste ResultatModule pour un étudiant / module / session.
        Doit être appelé APRÈS calculer_element pour tous les éléments du module.
        Met aussi à jour code_statut des ResultatElement concernés.
        """
        from apps.inscriptions.models import InscriptionElement

        # Cherche via em__module_lmd (lien planning→LMD) ET via element__module (lien académique)
        insc_elements = list(
            InscriptionElement.objects
            .filter(inscription_ped=inscription_ped, em__module_lmd=module)
            .select_related('element', 'em')
        )
        if not insc_elements:
            # Fallback : lien académique direct (si InscriptionElement.element est renseigné)
            insc_elements = list(
                InscriptionElement.objects
                .filter(inscription_ped=inscription_ped, element__module=module)
                .select_related('element', 'em')
            )

        # Charge les ResultatElement de cette session en une requête
        res_index: dict[int, ResultatElement] = {
            r.inscription_element_id: r
            for r in ResultatElement.objects.filter(
                session=self.session,
                inscription_element__in=[ie.pk for ie in insc_elements],
            )
        }

        total_pondere = Decimal('0')
        total_coeff   = Decimal('0')
        a_eliminatoire = False

        for ie in insc_elements:
            res = res_index.get(ie.pk)
            if res is None:
                continue
            # Coefficient : élément académique > EM planification > défaut 1
            coeff = (
                ie.element.coefficient if ie.element and ie.element.coefficient else None
            ) or (
                ie.em.coefficient if ie.em and ie.em.coefficient else None
            ) or Decimal('1')
            total_pondere += res.note_finale * coeff
            total_coeff   += coeff
            if res.est_eliminatoire:
                a_eliminatoire = True

        # Aucun element evalue (tous RE absents) → module non evaluable, pas de RM
        # Supprime le RM existant si on en a un (cas: notes effacees apres coup)
        if total_coeff == Decimal('0'):
            ResultatModule.objects.filter(
                inscription_ped=inscription_ped,
                module=module,
                session=self.session,
            ).delete()
            return None

        moyenne = (total_pondere / total_coeff).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP,
        )

        # Art. 13 : module validé si moyenne ≥ 10 ET pas d'éliminatoire
        est_valide      = moyenne >= Decimal('10') and not a_eliminatoire
        credits_valides = module.credits if est_valide else 0

        # Code statut module — uniquement V ou NV (NV pourra devenir V par
        # compensation semestrielle Art. 14 dans rafraichir_codes_apres_semestre)
        code_module = 'V' if est_valide else 'NV'

        resultat_module, _ = ResultatModule.objects.update_or_create(
            inscription_ped=inscription_ped,
            module=module,
            session=self.session,
            defaults={
                'moyenne':         moyenne,
                'credits_valides': credits_valides,
                'est_valide':      est_valide,
                'a_eliminatoire':  a_eliminatoire,
                'code_statut':     code_module,
            },
        )

        # Propager code_statut sur les éléments
        self._assigner_codes_elements(insc_elements, res_index, resultat_module)

        return resultat_module

    # ── Calcul en lot ────────────────────────────────────────────────────────

    def calculer_tous_modules_semestre(self, inscription_ped):
        """
        Calcule ResultatModule pour tous les modules du semestre de cet étudiant.
        À appeler après calculer_tous_elements_session().
        """
        from apps.modules.models import Module

        modules = Module.objects.filter(
            semestre=inscription_ped.semestre,
            filiere=inscription_ped.inscription_admin.filiere,
            actif=True,
        )

        resultats = []
        for module in modules:
            r = self.calculer(inscription_ped, module)
            if r is not None:
                resultats.append(r)
        return resultats

    def calculer_tous_modules_session(self):
        """
        Calcule tous les ResultatModule pour toute la session.
        À appeler après calculer_tous_elements_session().
        """
        from apps.inscriptions.models import InscriptionPedagogique
        from apps.modules.models import Module

        # Toutes les InscriptionPedagogique ayant des résultats éléments dans cette session
        TYPE_MAP = {'Impairs': 'I', 'Pairs': 'P'}
        sem_type = TYPE_MAP.get(self.session.type_semestre, 'I')

        insc_peds = InscriptionPedagogique.objects.filter(
            semestre__type_semestre=sem_type,
            inscription_admin__annee_univ=self.session.annee_univ,
        ).select_related('semestre', 'inscription_admin__filiere').distinct()

        resultats = []
        for insc_ped in insc_peds:
            resultats.extend(self.calculer_tous_modules_semestre(insc_ped))
        return resultats

    # ── Mise à jour VCS après calcul semestre ────────────────────────────────

    def rafraichir_codes_apres_semestre(self, inscription_ped, est_semestre_admis: bool):
        """
        Appelé par NoteCalculService.calculer_semestre() une fois la moyenne du
        semestre connue. Transforme les NV provisoires en VCS si le semestre est
        admis (Art. 14), sinon les laisse NV ou NVO.

        Art. 15 Arrêté 562 : « La validation du semestre permet la capitalisation
        de 30 crédits. » → Les modules VCS (compensés) acquièrent leurs crédits
        au même titre que les modules V/VCI.
        """
        from apps.inscriptions.models import InscriptionElement

        # Tous les modules du semestre (inclut V/VCI/VCS/NV/NVO/E)
        # — V/VCI : rattrapage est_valide pour leurs éléments VCI
        # — NV/VCS : compensation semestrielle si est_semestre_admis
        modules_a_traiter = ResultatModule.objects.filter(
            inscription_ped=inscription_ped,
            session=self.session,
        ).select_related('module')

        from django.db.models import Q

        for rm in modules_a_traiter:
            # Étape 1 : module NV peut devenir V par compensation semestrielle (Art. 14)
            # uniquement si le semestre est admis ET le module ≥ 8 (compensable)
            doit_compenser = (
                est_semestre_admis
                and rm.code_statut == 'NV'
                and rm.moyenne >= Decimal('8')
            )
            if doit_compenser:
                rm.code_statut     = 'V'                  # module validé par compensation
                rm.credits_valides = rm.module.credits    # Art. 15 — capitalisation
                rm.est_valide      = True
                rm.save(update_fields=['code_statut', 'credits_valides', 'est_valide'])

            # Étape 2 : propager le statut sur les éléments du module
            # — Module V validé directement (moyenne ≥ 10) → éléments < 10 deviennent VCI
            # — Module V validé par compensation (était NV avant)  → éléments NV deviennent VCS
            if rm.code_statut == 'V' and rm.est_valide:
                insc_elements = list(
                    InscriptionElement.objects
                    .filter(inscription_ped=inscription_ped)
                    .filter(Q(em__module_lmd=rm.module_id) | Q(element__module=rm.module))
                    .select_related('element', 'em')
                )
                res_index = {
                    r.inscription_element_id: r
                    for r in ResultatElement.objects.filter(
                        session=self.session,
                        inscription_element__in=[ie.pk for ie in insc_elements],
                    )
                }
                # Détermine si le module est validé directement (≥ 10) ou par compensation
                module_directement_valide = rm.moyenne >= Decimal('10')

                for ie in insc_elements:
                    res = res_index.get(ie.pk)
                    if res is None:
                        continue
                    # E (éliminatoire < 6) reste E quoi qu'il arrive
                    if res.code_statut == 'E':
                        continue
                    # V (≥ 10) reste V — l'élément est validé directement
                    if res.code_statut == 'V':
                        continue
                    # Élément < 10 dans module validé
                    code_cible = 'VCI' if module_directement_valide else 'VCS'
                    if res.code_statut != code_cible or not res.est_valide:
                        res.code_statut = code_cible
                        res.est_valide  = True   # capitalisation Art. 13/15
                        res.save(update_fields=['code_statut', 'est_valide'])

    # ── Privé ────────────────────────────────────────────────────────────────

    @staticmethod
    def _assigner_codes_elements(insc_elements, res_index, resultat_module: ResultatModule):
        """
        Assigne code_statut et est_valide sur chaque ResultatElement selon le
        résultat du module.
        Codes provisoires — VCS assigné plus tard par rafraichir_codes_apres_semestre().

        Art. 13 Arrêté 562 : « La validation du module emporte l'acquisition des
        crédits correspondants à l'ensemble des éléments du module. »
        → Élément V ou VCI (compensé intra-module) = est_valide=True
        """
        to_save = []
        for ie in insc_elements:
            res = res_index.get(ie.pk)
            if res is None:
                continue

            if res.est_eliminatoire:
                code, valide = 'E', False
            elif res.note_finale >= Decimal('10'):
                code, valide = 'V', True
            elif resultat_module.est_valide:
                # Élément < 10 compensé par d'autres éléments dans le module (Art. 13)
                code, valide = 'VCI', True
            else:
                # Module non validé : élément reste NV (provisoire → VCS si le
                # semestre est validé par la suite via rafraichir_codes_apres_semestre)
                code, valide = 'NV', False

            if res.code_statut != code or res.est_valide != valide:
                # Capture des anciennes valeurs AVANT modification, pour audit
                # detaille post-bulk_update (cf. write_audit_bulk plus bas).
                res._audit_old = {
                    'code_statut': res.code_statut,
                    'est_valide':  res.est_valide,
                }
                res.code_statut = code
                res.est_valide  = valide
                to_save.append(res)

        if to_save:
            ResultatElement.objects.bulk_update(to_save, ['code_statut', 'est_valide'])
            # Audit individuel par ResultatElement modifie. bulk_update() ne
            # declenche pas de signal post_save -> sans ce log, les changements
            # de code_statut (V/NV/VCI/E) seraient totalement silencieux et la
            # decision academique non tracable.
            try:
                from core.audit_helpers import write_audit_bulk
                audit_items = [
                    {
                        'object_id': res.pk,
                        'changes': {
                            'code_statut': {
                                'old': getattr(res, '_audit_old', {}).get('code_statut'),
                                'new': res.code_statut,
                            },
                            'est_valide': {
                                'old': getattr(res, '_audit_old', {}).get('est_valide'),
                                'new': res.est_valide,
                            },
                        },
                    }
                    for res in to_save
                ]
                write_audit_bulk(
                    'ResultatElement', 'UPDATE', audit_items,
                    label='Recalcul module',
                )
            except Exception:
                # Audit defensif : ne jamais casser le calcul si l'audit echoue.
                import logging as _logging
                _logging.getLogger('siga').warning(
                    'Audit bulk ResultatElement (calcul_module) failed', exc_info=True
                )
