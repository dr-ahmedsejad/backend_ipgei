"""
Service de délibération semestrielle — Art. 15-17 de l'Arrêté 562.

Workflow :
  1. peupler_lignes()           → crée LigneDeliberation par étudiant
  2. calculer_decisions()       → applique Art. 15 (admis/ajourné/rachat)
  3. generer_obligations()      → génère ObligationRattrapage (Art. 17)

Règles appliquées :
  Admis (direct/compensation) :  ResultatSemestre.est_admis = True
  Validé par rachat jury       :  décision = 'rachat' posée manuellement
  Ajourné                      :  sinon
  Invalidé pour absentéisme    :  décision posée manuellement + motif
"""
from decimal import Decimal

from django.db import transaction

from apps.evaluations.models import (
    PVDeliberation, LigneDeliberation, ParametreJury,
    ObligationRattrapage,
)
from apps.evaluations.services.calcul_notes import NoteCalculService


class DeliberationSemestreService:
    def __init__(self, pv: PVDeliberation):
        if pv.type_pv != 'semestriel':
            raise ValueError('Ce service ne traite que les PV semestriels (type_pv="semestriel").')
        self.pv = pv

    # ── Peuplement ────────────────────────────────────────────────────────────

    @transaction.atomic
    def peupler_lignes(self) -> int:
        """
        Crée une LigneDeliberation par étudiant inscrit au semestre ciblé par ce PV.
        Idempotent : get_or_create sur (pv, inscription_admin).
        Retourne le nombre de lignes créées ou mises à jour.
        """
        from apps.inscriptions.models import InscriptionAdministrative, InscriptionPedagogique
        from apps.evaluations.models import ResultatSemestre

        # Identifie les InscriptionPedagogique correspondant à ce semestre/filière/niveau
        insc_peds = InscriptionPedagogique.objects.filter(
            semestre__code_semestre=self.pv.semestre_code,
            inscription_admin__filiere=self.pv.filiere,
            inscription_admin__annee_univ=self.pv.session.annee_univ,
            inscription_admin__niveau=self.pv.niveau,
        ).select_related('inscription_admin__etudiant', 'semestre')

        # Session de RATTRAPAGE : ne lister QUE les étudiants ayant une obligation
        # (issue de la session NORMALE, même année + parité). La normale concerne
        # tout le monde ; le rattrapage seulement ceux qui ont qqch à rattraper.
        if self.pv.session and self.pv.session.type_session == 'rattrapage':
            from apps.evaluations.models import ObligationRattrapage
            etu_oblig = set(
                ObligationRattrapage.objects.filter(
                    ligne__pv__session__annee_univ=self.pv.session.annee_univ,
                    ligne__pv__session__type_semestre=self.pv.session.type_semestre,
                    ligne__pv__session__type_session='normale',
                ).values_list('ligne__inscription_admin_id', flat=True)
            )
            insc_peds = insc_peds.filter(inscription_admin_id__in=etu_oblig)
            # Purge des lignes déjà créées pour des étudiants sans rattrapage.
            self.pv.lignes.exclude(inscription_admin_id__in=etu_oblig).delete()

        count = 0
        for insc_ped in insc_peds:
            # Recupere le ResultatSemestre de LA session du PV (pas le plus
            # recent globalement). Sans ce filtre, un PV de session normale
            # se voit attribuer la moyenne/credits/code_statut d'une session
            # de rattrapage si elle existe (id plus grand) -> incoherence
            # 30 credits + NV affichee dans le PV normale.
            res_sem = (
                ResultatSemestre.objects
                .filter(inscription_ped=insc_ped, session=self.pv.session)
                .order_by('-id')
                .first()
            )

            moyenne = res_sem.moyenne if res_sem else Decimal('0')
            credits = res_sem.credits_valides if res_sem else 0
            code_statut = res_sem.code_statut if res_sem else ''

            ligne, created = LigneDeliberation.objects.get_or_create(
                pv=self.pv,
                inscription_admin=insc_ped.inscription_admin,
                defaults={
                    'decision':     'ajourned',
                    'moyenne_annuelle': moyenne,
                    'credits_annuels':  credits,
                    'code_statut':      code_statut,
                },
            )
            if not created:
                ligne.moyenne_annuelle = moyenne
                ligne.credits_annuels  = credits
                ligne.code_statut      = code_statut
                ligne.save(update_fields=['moyenne_annuelle', 'credits_annuels', 'code_statut'])
            count += 1

        return count

    # ── Calcul des décisions ─────────────────────────────────────────────────

    @transaction.atomic
    def calculer_decisions(self) -> int:
        """
        Applique Art. 15 sur chaque LigneDeliberation.
        N'écrase pas les décisions 'rachat' posées manuellement.

        Décisions :
          admis   : ResultatSemestre.est_admis = True
          ajourned: sinon
        """
        from apps.inscriptions.models import InscriptionPedagogique
        from apps.evaluations.models import ResultatSemestre

        lignes = self.pv.lignes.all().select_related('inscription_admin')
        count = 0

        for ligne in lignes:
            # Ne pas écraser une décision de rachat saisie manuellement
            if ligne.decision == 'rachat':
                count += 1
                continue

            insc_ped = (
                InscriptionPedagogique.objects
                .filter(
                    inscription_admin=ligne.inscription_admin,
                    semestre__code_semestre=self.pv.semestre_code,
                )
                .first()
            )

            if insc_ped is None:
                count += 1
                continue

            # Filtrer par la session du PV : sinon, si un ResultatSemestre de
            # session de rattrapage existe (cree en arriere-plan par un
            # recalculer-tout precedent), .order_by('-id').first() le retourne
            # a tort meme pour un PV de session normale, ce qui marque admis
            # un etudiant qui ne valide pas en normale -> pas d'obligation
            # rattrapage generee.
            res_sem = (
                ResultatSemestre.objects
                .filter(inscription_ped=insc_ped, session=self.pv.session)
                .order_by('-id')
                .first()
            )

            if res_sem and res_sem.est_admis:
                decision = 'admis'
            else:
                decision = 'ajourned'

            if ligne.decision != decision:
                ligne.decision = decision
                ligne.save(update_fields=['decision'])

            count += 1

        return count

    # ── Obligations de rattrapage ─────────────────────────────────────────────

    @transaction.atomic
    def generer_obligations(self) -> int:
        """
        Génère ObligationRattrapage pour chaque étudiant ajourné.
        Les obligations existantes pour cette ligne sont réécrites.

        Codes EM possibles : E, NV, V, VCI, VCS.
        Le type d'obligation dépend du RÉGIME de la filière du PV :

        Licence Pro — Art. 17 Arrêté 562 (3 alinéas) :
          E                     → obligatoire (al. 1 — éliminatoire)
          NV ET module < 8      → obligatoire (al. 2 — module non compensable)
          NV ET module ≥ 8      → facultatif  (al. 3 — module compensable)

        Ingénieur — Art. 21 Décret 2018-070 (2 alinéas seulement) :
          E                     → obligatoire (« doit obligatoirement »)
          NV                    → facultatif  (« peut se présenter »), quelle
                                  que soit la moyenne du module

        V, VCI, VCS → pas de rattrapage (élément validé) dans les deux régimes.

        Retourne le nombre d'obligations créées.
        """
        # Les obligations de rattrapage se génèrent UNIQUEMENT sur la session NORMALE :
        # une session de rattrapage est terminale (pas de « rattrapage du rattrapage »).
        # Backstop pour tout appelant (le bouton, clore, recalculer-tout, peupler).
        if not (self.pv.session and self.pv.session.type_session == 'normale'):
            return 0

        from apps.inscriptions.models import InscriptionPedagogique, InscriptionElement
        from apps.evaluations.models import ResultatElement, ResultatModule
        from decimal import Decimal

        # Régime réglementaire : Décret 2018-070 (ING) ou Arrêté 562 (LP, défaut)
        est_ing = (
            self.pv.filiere is not None
            and self.pv.filiere.type_diplome == 'ING'
        )

        # Exceptions (figées par session) : si VCS et/ou VCI actif, on traite AUSSI les
        # étudiants admis (un EM VCS/VCI peut appartenir à un admis) pour leur ouvrir le
        # rattrapage FACULTATIF. Sinon : comportement standard (ajournés seuls).
        vcs_actif = bool(self.pv.session and getattr(self.pv.session, 'rattrapage_vcs_actif', False))
        vci_actif = bool(self.pv.session and getattr(self.pv.session, 'rattrapage_vci_actif', False))
        # Hygiène : exception désactivée → purger les obligations résiduelles de ce code
        # (créées quand elle était active), sinon l'EM resterait rattrapable en saisie.
        if not vcs_actif:
            ObligationRattrapage.objects.filter(ligne__pv=self.pv, code_statut_initial='VCS').delete()
        if not vci_actif:
            ObligationRattrapage.objects.filter(ligne__pv=self.pv, code_statut_initial='VCI').delete()
        lignes_a_traiter = (
            self.pv.lignes.all() if (vcs_actif or vci_actif)
            else self.pv.lignes.filter(decision='ajourned')
        )
        total = 0

        for ligne in lignes_a_traiter:
            insc_ped = (
                InscriptionPedagogique.objects
                .filter(
                    inscription_admin=ligne.inscription_admin,
                    semestre__code_semestre=self.pv.semestre_code,
                )
                .first()
            )
            if insc_ped is None:
                continue

            # Supprime les anciennes obligations de cette ligne
            ObligationRattrapage.objects.filter(ligne=ligne).delete()

            # Récupère tous les éléments du semestre avec leur code_statut
            insc_elements = InscriptionElement.objects.filter(
                inscription_ped=insc_ped,
            ).select_related('element', 'em__module_lmd')

            res_index = {
                r.inscription_element_id: r
                for r in ResultatElement.objects.filter(
                    session=self.pv.session,
                    inscription_element__in=[ie.pk for ie in insc_elements],
                )
            }

            # Index moyennes des modules pour décider du type d'obligation
            modules_moy = {
                rm.module_id: rm.moyenne or Decimal('0')
                for rm in ResultatModule.objects.filter(
                    inscription_ped=insc_ped, session=self.pv.session,
                )
            }

            for ie in insc_elements:
                res = res_index.get(ie.pk)
                if res is None:
                    continue

                code = res.code_statut
                if code == 'E':
                    type_obl = 'obligatoire'
                    motif = (
                        'Moyenne éliminatoire (Art. 21 Décret 2018-070)'
                        if est_ing else
                        'Moyenne éliminatoire (Art. 17 al. 1)'
                    )
                elif code == 'NV':
                    if est_ing:
                        # Art. 21 Décret 2018-070 : seul l'éliminatoire est
                        # obligatoire ; un EM non validé d'un module non validé
                        # est à rattrapage facultatif (« peut se présenter »),
                        # quelle que soit la moyenne du module.
                        type_obl = 'facultatif'
                        motif = ('Élément non validé — rattrapage facultatif '
                                 '(Art. 21 Décret 2018-070)')
                    else:
                        # Art. 17 Arrêté 562 : le type dépend de la moyenne
                        # du module auquel l'élément appartient.
                        module_id = (
                            ie.em.module_lmd_id if ie.em and ie.em.module_lmd_id else
                            (ie.element.module_id if ie.element else None)
                        )
                        moy_mod = modules_moy.get(module_id, Decimal('0'))
                        if moy_mod < Decimal('8'):
                            type_obl = 'obligatoire'
                            motif = 'Élément non validé — module < 8 (Art. 17 al. 2)'
                        else:
                            type_obl = 'facultatif'
                            motif = 'Élément non validé — module entre 8 et 10 (Art. 17 al. 3)'
                elif code == 'VCS' and vcs_actif:
                    # Exception (figée par session) : élément validé par compensation
                    # semestrielle, rendu rattrapable (facultatif) pour améliorer la note
                    # — qui reste plafonnée par la règle de rattrapage habituelle.
                    type_obl = 'facultatif'
                    motif = ('Validé par compensation semestrielle — rattrapage '
                             'facultatif (exception activée pour cette session)')
                elif code == 'VCI' and vci_actif:
                    # Exception (figée par session) : élément validé par compensation
                    # intra-module, rendu rattrapable (facultatif) pour améliorer la note
                    # — qui reste plafonnée par la règle de rattrapage habituelle.
                    type_obl = 'facultatif'
                    motif = ('Validé par compensation intra-module — rattrapage '
                             'facultatif (exception activée pour cette session)')
                else:
                    continue  # V (et VCS/VCI hors exception) → pas de rattrapage

                ObligationRattrapage.objects.create(
                    ligne=ligne,
                    inscription_element=ie,
                    type_obligation=type_obl,
                    code_statut_initial=code,
                    motif=motif,
                )
                total += 1

        return total
