"""
Service de délibération annuelle (progression) — Art. 20-21-22 de l'Arrêté 562
et Art. 24-25 du Décret 2018-070 (Ingénieur).

Workflow :
  1. peupler_lignes()     → agrège S_impair + S_pair pour chaque étudiant
  2. calculer_decisions() → applique Art. 20 (passage/redoublement/exclusion)

Factory :
  get_deliberation_annuelle_service(pv) retourne la bonne sous-classe selon
  pv.filiere.type_diplome ('LP' → Licence, 'ING' → Ingénieur).
"""
from decimal import Decimal

from django.db import transaction

from apps.evaluations.models import (
    PVDeliberation, LigneDeliberation, ParametreJury,
)
from apps.evaluations.services.calcul_notes import NoteCalculService

# Crédits standard par année LMD
CREDITS_PAR_ANNEE  = 60
CREDITS_DIPLOME    = 180              # 6 semestres × 30 — Art. 25 Arrêté 562 / Art. 8 Décret 2018-070
SEUIL_NOTE_DIPLOME = Decimal('12')   # note finale S6/PFE — Art. 25 Arrêté 562 / Art. 16 Décret 2018-070
SEUIL_EXCLUSION    = Decimal('6')    # moyenne annuelle < 6 → exclusion directe


def get_deliberation_annuelle_service(pv: PVDeliberation) -> 'DeliberationAnnuelleService':
    """Factory : retourne la bonne sous-classe selon le type de diplôme de la filière."""
    type_diplome = (pv.filiere.type_diplome if pv.filiere else 'LP') or 'LP'
    cls = {'ING': DeliberationAnnuelleIngenieur}.get(type_diplome, DeliberationAnnuelleLicence)
    return cls(pv)


class DeliberationAnnuelleService:
    """Classe de base — sous-classée par régime réglementaire."""

    SEUIL_PROGRESSION: Decimal = Decimal('65')  # % — Art. 20 Arrêté 562

    def __init__(self, pv: PVDeliberation):
        if pv.type_pv != 'annuel':
            raise ValueError('Ce service ne traite que les PV annuels (type_pv="annuel").')
        self.pv = pv

    # ── Verification pre-peuplement ───────────────────────────────────────────

    def verifier_sessions_pretes(self) -> dict:
        """
        Verifie que les 4 sessions (SN-I, SR-I, SN-P, SR-P) necessaires a la
        consolidation existent et sont coherentes pour cette annee + institution.

        Retourne :
          {
            'pretes':   bool,                # True si aucun warning
            'warnings': [str, ...],          # Liste des avertissements
            'sessions': {                    # Mapping des 4 slots
              'SN-I': SessionEvaluation|None,
              'SR-I': SessionEvaluation|None,
              'SN-P': SessionEvaluation|None,
              'SR-P': SessionEvaluation|None,
            },
          }

        Non bloquant : peupler_lignes peut etre appele meme avec warnings,
        l'utilisateur en est juste averti pour decider en connaissance de cause.
        """
        from apps.evaluations.models import SessionEvaluation

        sessions = SessionEvaluation.objects.filter(
            annee_univ=self.pv.annee_univ,
            institution=self.pv.institution,
        )
        by_key = {(s.type_session, s.type_semestre): s for s in sessions}
        warnings = []
        sessions_map = {}

        for parite in ('Impairs', 'Pairs'):
            label_p = parite[0]  # 'I' ou 'P'
            sn = by_key.get(('normale', parite))
            sr = by_key.get(('rattrapage', parite))
            sessions_map[f'SN-{label_p}'] = sn
            sessions_map[f'SR-{label_p}'] = sr

            if sn is None:
                warnings.append(
                    f"Session normale {parite} absente — moyenne annuelle incomplete."
                )
            elif not sn.est_close:
                warnings.append(
                    f"Session normale {parite} ({sn.code}) non cloturee — "
                    f"moyenne annuelle provisoire."
                )
            if sr and not sr.est_close:
                warnings.append(
                    f"Session rattrapage {parite} ({sr.code}) ouverte — "
                    f"Art. 18 (max SN/SR) non encore propage pour {parite}."
                )

        return {
            'pretes':   len(warnings) == 0,
            'warnings': warnings,
            'sessions': sessions_map,
        }

    # ── Peuplement ────────────────────────────────────────────────────────────

    @transaction.atomic
    def peupler_lignes(self) -> int:
        """
        Crée une LigneDeliberation par étudiant inscrit cette année.
        Calcule pour chaque étudiant :
          - moyenne_annuelle (pondérée par crédits des semestres)
          - credits_annuels (total crédits capitalisés)
          - taux_capitalisation
          - verrou_passage (si niveau=2 et L1 non entièrement validée)

        Idempotent : update_fields si la ligne existe déjà.
        """
        from apps.inscriptions.models import InscriptionAdministrative

        inscriptions = (
            InscriptionAdministrative.objects
            .filter(
                filiere=self.pv.filiere,
                annee_univ=self.pv.annee_univ,
                niveau=self.pv.niveau,
            )
            .select_related('etudiant', 'annee_univ')
        )

        count = 0
        for insc_admin in inscriptions:
            moyenne, credits, _ = NoteCalculService.calculer_moyenne_annuelle(
                etudiant=insc_admin.etudiant,
                annee_univ=insc_admin.annee_univ,
                niveau=insc_admin.niveau,
            )

            taux = (
                Decimal(str(credits)) / Decimal(str(CREDITS_PAR_ANNEE)) * 100
            ).quantize(Decimal('0.01')) if credits else Decimal('0')

            verrou_passage = self._calculer_verrou(insc_admin)

            ligne, created = LigneDeliberation.objects.get_or_create(
                pv=self.pv,
                inscription_admin=insc_admin,
                defaults={
                    'decision':            'ajourned',
                    'decision_annuelle':   '',
                    'moyenne_annuelle':    moyenne,
                    'credits_annuels':     credits,
                    'taux_capitalisation': taux,
                    'verrou_passage':      verrou_passage,
                },
            )
            if not created:
                ligne.moyenne_annuelle    = moyenne
                ligne.credits_annuels     = credits
                ligne.taux_capitalisation = taux
                ligne.verrou_passage      = verrou_passage
                ligne.save(update_fields=[
                    'moyenne_annuelle', 'credits_annuels',
                    'taux_capitalisation', 'verrou_passage',
                ])
            count += 1

        return count

    def _calculer_verrou(self, insc_admin) -> bool:
        """
        Calcule le verrou de passage (Art. 20 al. 2 Licence / Art. 25 Ingénieur).
        Sous-classes peuvent surcharger pour une logique différente.
        """
        if self.pv.niveau != 2:
            return False
        credits_l1 = self._credits_capitalises_niveau(
            insc_admin.etudiant, insc_admin.annee_univ, 1,
            institution=insc_admin.institution,
        )
        return credits_l1 < CREDITS_PAR_ANNEE

    # ── Calcul des décisions ─────────────────────────────────────────────────

    @transaction.atomic
    def calculer_decisions(self) -> int:
        """
        Applique Art. 20-22 (LP) / 24-28 (ING) sur chaque LigneDeliberation.
        N'écrase pas les décisions 'annee_blanche' posées manuellement (Art. 23).
        Lit le seuil_progression depuis ParametreJury si défini (P6 — paramétrable).

        Décisions annuelles automatiques :
          passage_droit  : 60 crédits capitalisés
          passage_cond   : taux ≥ SEUIL_PROGRESSION % et pas de verrou
          redoublement   : sinon (premier ajournement) — Art. 21 al. 1 (LP) / Art. 26 (ING)
          exclusion      : 2ᵉ vrai redoublement — Art. 22 (LP) / Art. 28 (ING)

        Nota : l'exclusion automatique n'est déclenchée QUE si l'étudiant a déjà
        consommé son droit de redoublement (consomme_droit_redoublement=True
        dans Progression). Le jury garde le pouvoir d'exclure manuellement
        un étudiant avec moyenne < 6 même au premier ajournement (Art. 21 al. 1).
        """
        try:
            params = self.pv.parametre_jury
            # Override réglementaire si le jury a explicitement fixé un seuil (P6)
            seuil_progress = (
                params.seuil_progression
                if params.seuil_progression is not None
                else self.SEUIL_PROGRESSION
            )
        except ParametreJury.DoesNotExist:
            seuil_progress = self.SEUIL_PROGRESSION

        # Pré-charger les dérogations « année blanche » actives pour cette année
        from apps.inscriptions.models import Derogation
        derogations_blanches = set(
            Derogation.objects
            .filter(
                annee_univ=self.pv.annee_univ,
                type_derogation='annee_blanche',
                statut='actif',
            )
            .values_list('etudiant_id', flat=True)
        )

        lignes = self.pv.lignes.select_related('inscription_admin__etudiant')
        count  = 0

        for ligne in lignes:
            # Préserver les années blanches saisies manuellement (Art. 23/29)
            if ligne.decision_annuelle == 'annee_blanche':
                count += 1
                continue

            # Forcer 'annee_blanche' s'il existe une dérogation administrative
            # active pour cet étudiant et cette année (Art. 23 / Art. 29)
            if ligne.inscription_admin.etudiant_id in derogations_blanches:
                if ligne.decision_annuelle != 'annee_blanche':
                    ligne.decision_annuelle = 'annee_blanche'
                    ligne.decision          = 'ajourned'
                    ligne.save(update_fields=['decision_annuelle', 'decision'])
                count += 1
                continue

            credits = ligne.credits_annuels or 0
            taux    = ligne.taux_capitalisation or Decimal('0')

            # P4 — Compteur de redoublement corrigé : ne compte pas les années blanches
            deja_redoublant = self._est_deja_redoublant(ligne.inscription_admin)

            if credits >= CREDITS_PAR_ANNEE:
                # Le verrou de passage (Art. 20 al. 2 Licence / Art. 25 Ingénieur)
                # prime même sur le passage de droit : niveau inférieur (L1 / S1+S2)
                # non entièrement validé → accès au niveau supérieur INTERDIT, même
                # avec 60 crédits au niveau courant → redoublement (comme la branche
                # passage_cond ci-dessous).
                decision_annuelle = 'redoublement' if ligne.verrou_passage else 'passage_droit'
            elif taux >= seuil_progress:
                decision_annuelle = 'redoublement' if ligne.verrou_passage else 'passage_cond'
            elif deja_redoublant:
                # 2ᵉ ajournement après un vrai redoublement → exclusion (Art. 22/28)
                decision_annuelle = 'exclusion'
            else:
                # 1er ajournement → redoublement autorisé (Art. 21 al. 1 / Art. 26)
                decision_annuelle = 'redoublement'

            decision_legacy = (
                'admis'    if decision_annuelle in ('passage_droit', 'passage_cond') else
                'ajourned' if decision_annuelle == 'redoublement' else
                'exclus'
            )

            if (ligne.decision_annuelle != decision_annuelle
                    or ligne.decision != decision_legacy):
                ligne.decision_annuelle = decision_annuelle
                ligne.decision          = decision_legacy
                ligne.save(update_fields=['decision_annuelle', 'decision'])

            count += 1

        return count

    # ── Utilitaires internes ──────────────────────────────────────────────────

    @staticmethod
    def _credits_capitalises_niveau(etudiant, annee_univ_actuelle, niveau_cible,
                                    institution=None) -> int:
        """
        Crédits capitalisés pour un niveau donné (ex. L1) — utilisé par le verrou
        de passage (Art. 20 al. 2 Licence / Art. 25 Ingénieur).

        Consolidation PAR SEMESTRE du niveau (S{2n-1}, S{2n}) via le MÊME moteur
        que le relevé et la délibération annuelle (calculer_resultat_semestre_
        consolide : max(SN, SR) Art. 18 + report des EM capitalisés des années
        antérieures). On ne filtre donc PAS par niveau d'INSCRIPTION : un
        redoublant peut valider les semestres d'un niveau sous l'inscription d'un
        AUTRE niveau (ex. rattrapage de S1/S2 porté par l'inscription L2 d'un
        étudiant en L2).

        L'ancienne version ne lisait que les inscriptions de niveau=N des années
        STRICTEMENT antérieures : elle ratait la validation faite l'année courante
        sous une inscription d'un autre niveau, et verrouillait à tort des
        étudiants ayant pourtant validé leur L1 (cf. crédits annuels qui, eux,
        comptaient déjà ces semestres).

        Bornage : institution de la délibération + années <= année délibérée
        (la validation peut intervenir l'année courante).
        """
        from apps.inscriptions.models import InscriptionPedagogique
        from apps.documents.services import calculer_resultat_semestre_consolide

        # Semestres du niveau cible : niveau n -> S{2n-1}, S{2n}.
        codes = {f'S{2 * niveau_cible - 1}', f'S{2 * niveau_cible}'}

        ip_qs = InscriptionPedagogique.objects.filter(
            inscription_admin__etudiant=etudiant,
            semestre__code_semestre__in=codes,
        ).select_related('semestre')
        if institution is not None:
            ip_qs = ip_qs.filter(inscription_admin__institution=institution)
        if annee_univ_actuelle is not None:
            ip_qs = ip_qs.filter(
                inscription_admin__annee_univ__annee__lte=annee_univ_actuelle.annee,
            )

        # Par CODE de semestre, on retient la meilleure consolidation (robuste aux
        # objets-semestre homonymes multi-maquette) ; la consolidation couvre déjà
        # toutes les tentatives, toutes inscriptions confondues.
        seen_sem_ids = set()
        credits_par_code = {}
        for ip in ip_qs:
            sem = ip.semestre
            if not sem or sem.id in seen_sem_ids:
                continue
            seen_sem_ids.add(sem.id)
            res = calculer_resultat_semestre_consolide(
                etudiant, sem, annee_univ_actuelle,
            )
            cv = res.get('credits_valides', 0) or 0
            code = sem.code_semestre
            if cv > credits_par_code.get(code, 0):
                credits_par_code[code] = cv

        return sum(credits_par_code.values())

    @staticmethod
    def _est_deja_redoublant(inscription_admin) -> bool:
        """
        Art. 22/28 : un seul vrai redoublement autorisé dans le cycle.

        Utilise la table Progression.consomme_droit_redoublement pour distinguer
        vrai redoublement vs année blanche (Art. 23/29 — ne consomme pas le droit).

        EXCEPTION : si une dérogation d'inscription active existe pour cet
        étudiant à l'année courante, le droit de redoublement est rétabli
        (le directeur a accordé une exception → l'étudiant peut redoubler
        à nouveau même s'il a déjà épuisé son droit).

        IMPORTANT : seules les Progressions dont l'annee_source est STRICTEMENT
        ANTERIEURE a l'annee_univ de inscription_admin sont comptees. Cela
        evite l'effet de bord du re-peuplement d'un PV deja execute :
        - 1er peuplement N → cree Progression(annee_source=N, decision=redoublement)
        - Re-peuplement du meme PV N → sans le filtre, cette Progression serait
          comptee comme "deja redoublant" → faux exclusion. Avec le filtre, elle
          est ignoree (annee_source = N, pas < N).
        """
        from apps.inscriptions.models import (
            Progression, InscriptionAdministrative, Derogation,
        )

        # Dérogation d'inscription active → droit de redoublement rétabli
        derog_inscription = Derogation.objects.filter(
            etudiant=inscription_admin.etudiant,
            annee_univ=inscription_admin.annee_univ,
            type_derogation='derogation_inscription',
            statut='actif',
        ).exists()
        if derog_inscription:
            return False

        annee_courante_label = inscription_admin.annee_univ.annee

        if Progression.objects.filter(etudiant=inscription_admin.etudiant).exists():
            # Table Progression disponible → comptage précis, filtré aux
            # Progressions ANTERIEURES (pour ignorer celles issues du PV en cours).
            return Progression.objects.filter(
                etudiant=inscription_admin.etudiant,
                consomme_droit_redoublement=True,
                annee_source__annee__lt=annee_courante_label,
            ).exists()

        # Fallback pré-migration : comptage brut (approximatif)
        # On compte les InscriptionAdministrative au meme niveau dont l'annee
        # est strictement anterieure (idem logique : ignorer l'annee courante).
        return (
            InscriptionAdministrative.objects.filter(
                etudiant=inscription_admin.etudiant,
                niveau=inscription_admin.niveau,
                annee_univ__annee__lt=annee_courante_label,
            ).count() >= 1
        )

    # ── Blocage diplôme au dernier niveau du cycle ────────────────────────────

    # Messages d'observation — surchargés par les sous-classes avec l'article
    # du régime concerné (Art. 25 Arrêté 562 / Art. 8 & 16 Décret 2018-070).
    MSG_BLOCAGE_NOTE_FINALE = 'Diplôme refusé : note du semestre final (S6) < 12/20.'
    MSG_BLOCAGE_CREDITS = (
        'Diplôme refusé : {cv}/180 crédits capitalisés '
        '(Art. 25 Arrêté 562 / Art. 8 Décret 2018-070).'
    )

    def _bloquer_admis_non_eligible_diplome(self, count: int) -> int:
        """
        Rebascule en redoublement les « admis » du dernier niveau du cycle qui ne
        remplissent PAS les DEUX conditions réglementaires d'obtention du diplôme :
          1. 180 crédits capitalisés des 6 semestres
             — Art. 25 Arrêté 562 (LP) / Art. 8 Décret 2018-070 (ING) ;
          2. note du semestre final (S6) >= 12/20
             — Art. 25 Arrêté 562 (stage) / Art. 16 Décret 2018-070 (PFE).

        Les DEUX conditions sont mesurées par le MÊME moteur que le relevé et la
        consultation (calculer_resultat_semestre_consolide : max SN/SR Art. 18 +
        compensation VCS/VCI + report inter-années), et JAMAIS par le décompte
        stocké (ResultatSemestre) qui peut être périmé — un étudiant peut avoir
        180 crédits au relevé mais un ResultatSemestre non re-propagé (< 180). Se
        fonder sur le stocké bloquerait à tort des diplômés pourtant valides.

        Nota (Art. 21 Arrêté 562) : le jury peut adapter ces règles ; ce blocage
        est le comportement AUTOMATIQUE par défaut — un override manuel du jury
        (saisie décision / dérogation année blanche) reste possible.
        """
        # Tronc commun (filière AYANT des filières filles) : son niveau_fin n'est PAS
        # une année de diplôme — les étudiants poursuivent dans les filières filles
        # (ex. LPSTAT L1 → SDID/SEA en L2). Aucun blocage diplôme ne s'applique ici ;
        # la progression reste régie par la règle des crédits (base calculer_decisions).
        if self.pv.filiere and self.pv.filiere.filieres_filles.exists():
            return count

        niveau_final = (
            self.pv.filiere.niveau_fin
            if self.pv.filiere and self.pv.filiere.niveau_fin else 3
        )
        if self.pv.niveau != niveau_final:
            return count

        for ligne in self.pv.lignes.select_related('inscription_admin').all():
            if ligne.decision != 'admis':
                continue
            insc = ligne.inscription_admin

            credits    = self._credits_capitalises_diplome(insc)
            credits_ok = credits >= CREDITS_DIPLOME
            note_s6    = self._moyenne_semestre_final(insc)
            note_ok    = note_s6 is not None and Decimal(str(note_s6)) >= SEUIL_NOTE_DIPLOME

            if credits_ok and note_ok:
                continue  # les DEUX conditions réglementaires sont remplies

            motifs = []
            if not credits_ok:
                motifs.append(self.MSG_BLOCAGE_CREDITS.format(cv=credits))
            if not note_ok:
                motifs.append(self.MSG_BLOCAGE_NOTE_FINALE)

            ligne.decision_annuelle = 'redoublement'
            ligne.decision          = 'ajourned'
            ligne.observations      = (
                (ligne.observations or '').rstrip()
                + ''.join('\n[Auto] ' + m for m in motifs)
            )
            ligne.save(update_fields=['decision_annuelle', 'decision', 'observations'])

        return count

    def _credits_capitalises_diplome(self, insc_admin) -> int:
        """
        Total des crédits capitalisés du cursus (tous les niveaux jusqu'au niveau
        final), via le MÊME moteur que le relevé (_credits_capitalises_niveau →
        calculer_resultat_semestre_consolide). NE PAS remplacer par le décompte
        stocké (ResultatSemestre) : il peut être périmé vs le relevé (max SN/SR +
        compensation + report inter-années non re-propagés au stockage).
        Art. 25 Arrêté 562 / Art. 8 Décret 2018-070.
        """
        niveau_final = (
            self.pv.filiere.niveau_fin
            if self.pv.filiere and self.pv.filiere.niveau_fin else 3
        )
        return sum(
            self._credits_capitalises_niveau(
                insc_admin.etudiant, insc_admin.annee_univ, n,
                institution=insc_admin.institution,
            )
            for n in range(1, niveau_final + 1)
        )

    def _moyenne_semestre_final(self, insc_admin):
        """
        Moyenne CONSOLIDÉE du dernier semestre du cycle (S{2·niveau_fin}) — note
        finale de fin de formation : stage S6 (Art. 25 LP) / PFE (Art. 16 ING).
        Retourne None si aucun semestre final trouvé. Même moteur que le relevé.
        """
        from apps.inscriptions.models import InscriptionPedagogique
        from apps.documents.services import calculer_resultat_semestre_consolide

        niveau_final = (
            self.pv.filiere.niveau_fin
            if self.pv.filiere and self.pv.filiere.niveau_fin else 3
        )
        code_final = f'S{2 * niveau_final}'
        ip = (
            InscriptionPedagogique.objects
            .filter(inscription_admin__etudiant=insc_admin.etudiant,
                    semestre__code_semestre=code_final)
            .select_related('semestre')
            .order_by('-inscription_admin__annee_univ__annee')
            .first()
        )
        if not ip or not ip.semestre:
            return None
        res = calculer_resultat_semestre_consolide(
            insc_admin.etudiant, ip.semestre, insc_admin.annee_univ,
        )
        return res.get('moyenne_semestre')


class DeliberationAnnuelleLicence(DeliberationAnnuelleService):
    """
    Délibération pour filières Licence Professionnelle — Arrêté 562.
    Seuil de progression : 65 % (Art. 20).
    Verrou L3 : L1 entièrement validée (Art. 20 al. 2).
    Moyenne S6 (stage) ≥ 12/20 bloquante pour le diplôme (Art. 25).
    """
    SEUIL_PROGRESSION = Decimal('65')
    MSG_BLOCAGE_NOTE_FINALE = (
        'Diplôme refusé : moyenne du 6e semestre (stage de fin de formation) '
        '< 12/20 (Art. 25 Arrêté 562).'
    )
    MSG_BLOCAGE_CREDITS = (
        'Diplôme refusé : {cv}/180 crédits capitalisés — les 6 semestres ne '
        'sont pas tous validés (Art. 25 Arrêté 562).'
    )

    @transaction.atomic
    def calculer_decisions(self) -> int:
        """
        Surcharge : blocage diplôme Art. 25 Arrêté 562 — un admis en fin de cycle
        sans 180 crédits capitalisés OU sans moyenne >= 12/20 au S6 (stage de fin
        de formation) ne peut pas être diplômé.
        """
        count = super().calculer_decisions()
        return self._bloquer_admis_non_eligible_diplome(count)


class DeliberationAnnuelleIngenieur(DeliberationAnnuelleService):
    """
    Délibération pour filières Ingénieur — Décret 2018-070.
    Seuil de progression : 75 % (Art. 24).
    Verrou S5 : 60 crédits S1+S2 validés (Art. 25).
    Note PFE ≥ 12/20 bloquante pour l'obtention du diplôme au niveau 3 (Art. 16).
    """
    SEUIL_PROGRESSION = Decimal('75')
    MSG_BLOCAGE_NOTE_FINALE = 'Diplôme refusé : PFE < 12/20 (Art. 16 Décret 2018-070).'
    MSG_BLOCAGE_CREDITS = (
        'Diplôme refusé : {cv}/180 crédits capitalisés — les 6 semestres ne '
        'sont pas tous validés (Art. 8 Décret 2018-070).'
    )

    def _calculer_verrou(self, insc_admin) -> bool:
        """Verrou S5 Ingénieur : 60 crédits S1+S2 requis (Art. 25)."""
        if self.pv.niveau != 2:
            return False
        return self._credits_s1_s2(
            insc_admin.etudiant, insc_admin.annee_univ, insc_admin.institution,
        ) < CREDITS_PAR_ANNEE

    @transaction.atomic
    def calculer_decisions(self) -> int:
        """
        Surcharge : blocage diplôme Art. 8 & 16 Décret 2018-070 — un admis en fin
        de cycle sans 180 crédits (les 6 semestres validés) OU sans PFE >= 12/20
        ne peut pas accéder au titre d'ingénieur.
        """
        count = super().calculer_decisions()
        return self._bloquer_admis_non_eligible_diplome(count)

    @staticmethod
    def _credits_s1_s2(etudiant, annee_univ_actuelle=None, institution=None) -> int:
        """
        Credits capitalises en S1+S2 (niveau 1) — verrou S5 Ingenieur Art. 25.

        Delegue a _credits_capitalises_niveau pour beneficier du meme correctif
        que le verrou L3 Licence : scoping par institution, bornage aux annees
        anterieures, et selection de la MEILLEURE tentative (et non "la plus
        recente"). Sans ca, un redoublant ou un etudiant multi-institution
        voyait le verrou S5 fausse (cf. _credits_capitalises_niveau).
        """
        return DeliberationAnnuelleService._credits_capitalises_niveau(
            etudiant, annee_univ_actuelle, 1, institution=institution,
        )
