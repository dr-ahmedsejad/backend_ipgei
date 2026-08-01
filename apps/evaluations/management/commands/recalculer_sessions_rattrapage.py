"""
Recalcule ResultatElement / ResultatModule / ResultatSemestre + LigneDeliberation
pour les sessions de rattrapage cloturees, apres correction du bug CC/TP herites.

CONTEXTE :
  Avant la correction, calcul_notes.py recalculait les notes SR a partir des
  Notes saisies en session SR uniquement (donc CC=0 si non re-saisi). Cela
  donnait des notes SR artificiellement basses, contraires au releve PDF qui
  utilise CC/TP de SN + EXAM de SR.

USAGE :
  python manage.py recalculer_sessions_rattrapage                           # dry-run par defaut
  python manage.py recalculer_sessions_rattrapage --annee 2024-2025 --apply
  python manage.py recalculer_sessions_rattrapage --apply                   # toutes annees
  python manage.py recalculer_sessions_rattrapage --apply --force-clos      # autorise PV clos

EFFETS :
  - Recalcule chaque ResultatElement des sessions SR cloturees
  - Recalcule chaque ResultatModule + applique compensation Art. 14 (rafraichir_codes_apres_semestre)
  - Recalcule chaque ResultatSemestre
  - Re-peuple les LigneDeliberation des PV annuels (et semestriels SR) NON CLOS
  - Genere AuditLog pour chaque LigneDeliberation modifiee
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = (
        "Recalcule ResultatElement/Module/Semestre + lignes PV pour les "
        "sessions SR cloturees apres correction CC/TP herites de SN."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--annee', type=str, default=None,
            help="Annee universitaire (ex 2024-2025). Sinon : toutes annees.",
        )
        parser.add_argument(
            '--apply', action='store_true', default=False,
            help="Execute le recalcul. Par defaut : dry-run (lecture seule).",
        )
        parser.add_argument(
            '--force-clos', action='store_true', default=False,
            help="Autorise le re-peuplement des PV CLOS (admin only).",
        )

    def handle(self, *args, **opts):
        from apps.evaluations.models import (
            SessionEvaluation, ResultatElement, ResultatModule, ResultatSemestre,
            PVDeliberation, LigneDeliberation,
        )
        from apps.evaluations.services.calcul_notes import NoteCalculService
        from apps.evaluations.services.deliberation_annuelle import (
            get_deliberation_annuelle_service,
        )
        from apps.evaluations.services.deliberation_semestre import (
            DeliberationSemestreService,
        )
        from core.models import AuditLog

        annee_filter = opts['annee']
        apply_       = opts['apply']
        force_clos   = opts['force_clos']

        # 1. Identifier les sessions SR cloturees
        qs_sessions = SessionEvaluation.objects.filter(
            type_session='rattrapage', est_close=True,
        )
        if annee_filter:
            qs_sessions = qs_sessions.filter(annee_univ__annee=annee_filter)

        sessions = list(qs_sessions.select_related('annee_univ', 'institution'))
        if not sessions:
            self.stdout.write(self.style.WARNING(
                f"Aucune session de rattrapage cloturee trouvee"
                f"{f' pour annee {annee_filter}' if annee_filter else ''}."
            ))
            return

        self.stdout.write(self.style.WARNING(
            f"\n=== Recalcul {len(sessions)} session(s) de rattrapage cloturee(s) ===\n"
        ))
        for s in sessions:
            self.stdout.write(
                f"  - {s.code} (annee={s.annee_univ.annee} institution={s.institution_id})"
            )

        if not apply_:
            self.stdout.write(self.style.WARNING(
                "\n*** MODE DRY-RUN : aucune modification ***"
            ))
            self.stdout.write(
                "Affichage des diffs sans persister. Utilisez --apply pour executer.\n"
            )

        # 2. Snapshot avant recalcul (pour AuditLog)
        snapshot_re = {}  # (ie_id, session_id) -> note_finale_avant
        snapshot_rs = {}  # (ip_id, session_id) -> {moy, cred, est_admis}
        snapshot_ld = {}  # ligne_id -> {moy, cred, decision, decision_annuelle}

        for s in sessions:
            for re_ in ResultatElement.objects.filter(session=s):
                snapshot_re[(re_.inscription_element_id, s.id)] = re_.note_finale
            for rs in ResultatSemestre.objects.filter(session=s):
                snapshot_rs[(rs.inscription_ped_id, s.id)] = {
                    'moy': rs.moyenne, 'cred': rs.credits_valides,
                    'est_admis': rs.est_admis, 'code_statut': rs.code_statut,
                }

        # PVs annuels concernes (par annee_univ + institution)
        couples_year_inst = {(s.annee_univ_id, s.institution_id) for s in sessions}
        pvs_annuels = PVDeliberation.objects.filter(
            type_pv='annuel',
        )
        pvs_annuels_concernes = [
            pv for pv in pvs_annuels
            if (pv.annee_univ_id, pv.institution_id) in couples_year_inst
        ]
        # PVs semestriels SR (peuvent aussi etre impactes)
        pvs_semestriels_sr = list(
            PVDeliberation.objects.filter(
                type_pv='semestriel', session__in=sessions,
            )
        )

        for pv in pvs_annuels_concernes + pvs_semestriels_sr:
            for ld in pv.lignes.all():
                snapshot_ld[ld.id] = {
                    'matricule':         ld.inscription_admin.etudiant.matricule,
                    'pv_id':             pv.id,
                    'pv_type':           pv.type_pv,
                    'moy':               ld.moyenne_annuelle,
                    'cred':              ld.credits_annuels,
                    'decision':          ld.decision,
                    'decision_annuelle': ld.decision_annuelle,
                }

        # 3. Si dry-run : simuler en memoire (transaction rollback)
        if not apply_:
            self.stdout.write("\n--- Simulation (rollback final) ---")
            sid = transaction.savepoint()
            try:
                self._executer_recalcul(
                    sessions, pvs_annuels_concernes, pvs_semestriels_sr,
                    force_clos, NoteCalculService, get_deliberation_annuelle_service,
                    DeliberationSemestreService,
                )
                self._afficher_diffs(
                    sessions, pvs_annuels_concernes, pvs_semestriels_sr,
                    snapshot_re, snapshot_rs, snapshot_ld,
                    ResultatElement, ResultatSemestre,
                )
            finally:
                transaction.savepoint_rollback(sid)
                self.stdout.write(self.style.WARNING(
                    "\n*** Rollback effectue (dry-run). Aucune modification ***"
                ))
            return

        # 4. Mode apply : recalcul reel + AuditLog
        with transaction.atomic():
            self._executer_recalcul(
                sessions, pvs_annuels_concernes, pvs_semestriels_sr,
                force_clos, NoteCalculService, get_deliberation_annuelle_service,
                DeliberationSemestreService,
            )

            # AuditLog par LigneDeliberation modifiee
            nb_audit = 0
            for ligne_id, before in snapshot_ld.items():
                ld = LigneDeliberation.objects.filter(id=ligne_id).first()
                if not ld:
                    continue
                changes = {}
                for field, old in [
                    ('moyenne_annuelle',  before['moy']),
                    ('credits_annuels',   before['cred']),
                    ('decision',          before['decision']),
                    ('decision_annuelle', before['decision_annuelle']),
                ]:
                    new = getattr(ld, field)
                    if old != new:
                        changes[field] = {
                            'old': str(old) if isinstance(old, Decimal) else old,
                            'new': str(new) if isinstance(new, Decimal) else new,
                        }
                if changes:
                    AuditLog.objects.create(
                        action='UPDATE',   # core.models.ACTION_UPDATE (constante module, pas attribut de classe)
                        model_name='LigneDeliberation',
                        object_id=str(ligne_id),
                        changes={
                            'matricule': before['matricule'],
                            'pv_id':     before['pv_id'],
                            'pv_type':   before['pv_type'],
                            'motif':     'Recalcul SR avec heritage CC/TP de SN',
                            'fields':    changes,
                        },
                    )
                    nb_audit += 1

        self._afficher_diffs(
            sessions, pvs_annuels_concernes, pvs_semestriels_sr,
            snapshot_re, snapshot_rs, snapshot_ld,
            ResultatElement, ResultatSemestre,
        )
        self.stdout.write(self.style.SUCCESS(
            f"\n*** Recalcul applique : {nb_audit} AuditLog crees ***"
        ))

    def _executer_recalcul(
        self, sessions, pvs_annuels, pvs_semestriels_sr,
        force_clos, NoteCalculService, get_deliberation_annuelle_service,
        DeliberationSemestreService,
    ):
        """Execute le recalcul effectif (commun dry-run et apply)."""
        # 1. Recalculer chaque session SR : elements -> modules -> semestres
        for s in sessions:
            self.stdout.write(f"  Recalcul session {s.code} ...")
            svc = NoteCalculService(s)
            elems = svc.calculer_tous_elements_session()
            mods  = svc.calculer_tous_modules_session()
            sems  = svc.calculer_tous_semestres_session()
            self.stdout.write(
                f"    {len(elems)} elements, {len(mods)} modules, {len(sems)} semestres"
            )

        # 2. Re-peupler les PV annuels concernes (non clos sauf force-clos)
        for pv in pvs_annuels:
            if pv.est_clos and not force_clos:
                self.stdout.write(self.style.WARNING(
                    f"  PV annuel #{pv.id} CLOS — saute (utiliser --force-clos pour forcer)"
                ))
                continue
            svc = get_deliberation_annuelle_service(pv)
            n1 = svc.peupler_lignes()
            n2 = svc.calculer_decisions()
            self.stdout.write(f"  PV annuel #{pv.id} : {n1} lignes, {n2} decisions")

        # 3. Re-peupler les PV semestriels SR concernes
        for pv in pvs_semestriels_sr:
            if pv.est_clos and not force_clos:
                self.stdout.write(self.style.WARNING(
                    f"  PV semestriel SR #{pv.id} CLOS — saute"
                ))
                continue
            svc = DeliberationSemestreService(pv)
            n1 = svc.peupler_lignes()
            n2 = svc.calculer_decisions()
            self.stdout.write(f"  PV semestriel SR #{pv.id} : {n1} lignes, {n2} decisions")

    def _afficher_diffs(
        self, sessions, pvs_annuels, pvs_semestriels_sr,
        snapshot_re, snapshot_rs, snapshot_ld,
        ResultatElement, ResultatSemestre,
    ):
        """Affiche les diffs avant/apres pour ResultatElement/Semestre/LigneDeliberation."""
        # ResultatElement
        nb_re_modif = 0
        max_diff = Decimal('0')
        for s in sessions:
            for re_ in ResultatElement.objects.filter(session=s):
                old = snapshot_re.get((re_.inscription_element_id, s.id))
                if old is not None and old != re_.note_finale:
                    nb_re_modif += 1
                    diff = abs(re_.note_finale - old)
                    if diff > max_diff:
                        max_diff = diff

        self.stdout.write(self.style.WARNING(
            f"\n--- ResultatElement modifies : {nb_re_modif}  (max ecart {max_diff}) ---"
        ))

        # ResultatSemestre — montrer les changements de est_admis
        nb_rs_admis_change = 0
        for s in sessions:
            for rs in ResultatSemestre.objects.filter(session=s):
                old = snapshot_rs.get((rs.inscription_ped_id, s.id))
                if old and old['est_admis'] != rs.est_admis:
                    nb_rs_admis_change += 1
        self.stdout.write(self.style.WARNING(
            f"--- ResultatSemestre changement est_admis : {nb_rs_admis_change} ---"
        ))

        # LigneDeliberation
        nb_ld_modif = 0
        nb_ld_decision_change = 0
        for ligne_id, before in snapshot_ld.items():
            from apps.evaluations.models import LigneDeliberation
            ld = LigneDeliberation.objects.filter(id=ligne_id).first()
            if not ld:
                continue
            modified = (
                before['moy'] != ld.moyenne_annuelle
                or before['cred'] != ld.credits_annuels
                or before['decision'] != ld.decision
                or before['decision_annuelle'] != ld.decision_annuelle
            )
            if modified:
                nb_ld_modif += 1
                if (before['decision'] != ld.decision
                        or before['decision_annuelle'] != ld.decision_annuelle):
                    nb_ld_decision_change += 1
        self.stdout.write(self.style.WARNING(
            f"--- LigneDeliberation modifiees : {nb_ld_modif}  (dont {nb_ld_decision_change} avec changement de decision) ---"
        ))

        # Top 10 des plus gros impacts
        impacts = []
        for ligne_id, before in snapshot_ld.items():
            from apps.evaluations.models import LigneDeliberation
            ld = LigneDeliberation.objects.filter(id=ligne_id).first()
            if not ld:
                continue
            old_cred = before['cred'] or 0
            new_cred = ld.credits_annuels or 0
            diff_cred = new_cred - old_cred
            if diff_cred != 0 or before['decision'] != ld.decision or before['decision_annuelle'] != ld.decision_annuelle:
                impacts.append({
                    'matricule': before['matricule'],
                    'pv_id':     before['pv_id'],
                    'pv_type':   before['pv_type'],
                    'old_cred':  old_cred,
                    'new_cred':  new_cred,
                    'diff_cred': diff_cred,
                    'old_dec':   before['decision_annuelle'] or before['decision'],
                    'new_dec':   ld.decision_annuelle or ld.decision,
                })

        if impacts:
            impacts.sort(key=lambda x: -abs(x['diff_cred']))
            self.stdout.write("\n--- Top 15 etudiants les plus impactes ---")
            self.stdout.write(
                f"  {'Matr':<8} {'PV':<5} {'Type':<10} {'Cred':>10} {'Decision':>30}"
            )
            for imp in impacts[:15]:
                self.stdout.write(
                    f"  {imp['matricule']:<8} {imp['pv_id']:<5} {imp['pv_type']:<10} "
                    f"{imp['old_cred']:>4}->{imp['new_cred']:<4} "
                    f"{imp['old_dec']:>15} -> {imp['new_dec']:<15}"
                )
