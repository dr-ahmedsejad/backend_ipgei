"""
Recalcule un PV annuel existant avec la nouvelle logique de consolidation
des 4 sessions (SN-I, SR-I, SN-P, SR-P) — voir Sections 2-4 du plan correctif
deliberation annuelle (institution_V1).

La nouvelle logique :
  - Pour chaque parite (Impairs/Pairs), prend le ResultatSemestre de la session
    de rattrapage CLOTUREE si elle existe, sinon celui de la session normale
  - Filtre explicitement par institution
  - Elimine le double-comptage des credits SN+SR
  - Independant de l'ordre d'execution

Usage :
    python manage.py recalculer_pv_annuel <pv_id>                  # dry-run par defaut
    python manage.py recalculer_pv_annuel <pv_id> --apply
    python manage.py recalculer_pv_annuel <pv_id> --apply --force-clos
                                                                    # autorise le recalcul d'un PV clos

Pour chaque LigneDeliberation modifiee, un AuditLog est cree avec l'ancien et
le nouveau (moyenne_annuelle, credits_annuels, decision, decision_annuelle).
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = (
        "Recalcule un PV annuel existant avec la nouvelle logique de "
        "consolidation des 4 sessions (Art. 18 strict)."
    )

    def add_arguments(self, parser):
        parser.add_argument('pv_id', type=int, help="ID du PVDeliberation a recalculer.")
        parser.add_argument('--apply', action='store_true', default=False,
                            help="Execute le recalcul. Par defaut : dry-run (lecture seule).")
        parser.add_argument('--force-clos', action='store_true', default=False,
                            help="Autorise le recalcul d'un PV clos (admin only).")

    def handle(self, *args, **opts):
        from apps.evaluations.models import PVDeliberation, LigneDeliberation
        from apps.evaluations.services.deliberation_annuelle import (
            get_deliberation_annuelle_service,
        )
        from core.models import AuditLog

        pv_id      = opts['pv_id']
        apply_     = opts['apply']
        force_clos = opts['force_clos']

        try:
            pv = PVDeliberation.objects.get(id=pv_id)
        except PVDeliberation.DoesNotExist:
            raise CommandError(f"PVDeliberation #{pv_id} introuvable.")

        if pv.type_pv != 'annuel':
            raise CommandError(
                f"PV #{pv_id} est de type '{pv.type_pv}' — ce command ne traite "
                "que les PV annuels."
            )

        if pv.est_clos and not force_clos:
            raise CommandError(
                f"PV #{pv_id} est CLOS. Utilisez --force-clos pour autoriser "
                "le recalcul (admin only). L'AuditLog tracera l'operation."
            )

        self.stdout.write(self.style.WARNING(
            f"\n=== Recalcul PV {pv.id} — {pv.filiere} L{pv.niveau} {pv.annee_univ} ==="
        ))
        self.stdout.write(f"  est_clos={pv.est_clos}  type_pv={pv.type_pv}")

        svc = get_deliberation_annuelle_service(pv)

        # 1. Verifier l'etat des sessions
        verif = svc.verifier_sessions_pretes()
        self.stdout.write("\n--- Sessions sources ---")
        for slot, s in verif['sessions'].items():
            label = f"{s.code} (close={s.est_close})" if s else "ABSENTE"
            self.stdout.write(f"  {slot} : {label}")
        if verif['warnings']:
            self.stdout.write(self.style.WARNING("\n--- Warnings ---"))
            for w in verif['warnings']:
                self.stdout.write(self.style.WARNING(f"  ! {w}"))

        # 2. Snapshot avant
        lignes_avant = {
            l.id: {
                'inscription_admin_id': l.inscription_admin_id,
                'matricule':            l.inscription_admin.etudiant.matricule,
                'moyenne_annuelle':     l.moyenne_annuelle,
                'credits_annuels':      l.credits_annuels,
                'decision':             l.decision,
                'decision_annuelle':    l.decision_annuelle,
            }
            for l in pv.lignes.select_related('inscription_admin__etudiant').all()
        }

        # 3. DRY-RUN ou APPLY
        if not apply_:
            self.stdout.write(self.style.WARNING(
                "\n*** MODE DRY-RUN : aucune modification appliquee ***"
            ))
            self.stdout.write("Utilisez --apply pour executer le recalcul.\n")
            self._afficher_diff_dry_run(svc, lignes_avant, pv)
            return

        # 4. Recalcul reel
        with transaction.atomic():
            nb_lignes    = svc.peupler_lignes()
            nb_decisions = svc.calculer_decisions()

            # Snapshot apres
            lignes_apres = {
                l.id: {
                    'moyenne_annuelle':  l.moyenne_annuelle,
                    'credits_annuels':   l.credits_annuels,
                    'decision':          l.decision,
                    'decision_annuelle': l.decision_annuelle,
                }
                for l in pv.lignes.all()
            }

            # AuditLog par ligne modifiee
            nb_audit = 0
            for ligne_id, before in lignes_avant.items():
                after = lignes_apres.get(ligne_id)
                if not after:
                    continue
                changes = {}
                for field in ('moyenne_annuelle', 'credits_annuels', 'decision', 'decision_annuelle'):
                    old_val = before.get(field)
                    new_val = after.get(field)
                    if old_val != new_val:
                        # Decimal → str pour JSON
                        changes[field] = {
                            'old': str(old_val) if isinstance(old_val, Decimal) else old_val,
                            'new': str(new_val) if isinstance(new_val, Decimal) else new_val,
                        }
                if changes:
                    AuditLog.objects.create(
                        action='UPDATE',   # core.models.ACTION_UPDATE (constante module, pas attribut de classe)
                        model_name='LigneDeliberation',
                        object_id=str(ligne_id),
                        changes={
                            'matricule': before['matricule'],
                            'pv_id':     pv.id,
                            'motif':     'Recalcul avec consolidation 4 sessions (Art. 18 strict)',
                            'fields':    changes,
                        },
                    )
                    nb_audit += 1

        self.stdout.write(self.style.SUCCESS(
            f"\n*** Recalcul applique : {nb_lignes} lignes, {nb_decisions} decisions, "
            f"{nb_audit} AuditLog crees ***"
        ))

    def _afficher_diff_dry_run(self, svc, lignes_avant, pv):
        """Calcule la nouvelle moyenne sans modifier la BD pour montrer les diffs."""
        from apps.evaluations.services.calcul_notes import NoteCalculService
        from apps.evaluations.services.deliberation_annuelle import (
            CREDITS_PAR_ANNEE,
        )

        nb_diff = 0
        nb_total = len(lignes_avant)

        self.stdout.write("\n--- Comparaison ancien vs nouveau (sans modification) ---")
        self.stdout.write(
            f"  {'Matricule':<12} {'Moy actu':>9} {'Moy nouv':>9} {'Diff':>7}  "
            f"{'Cred actu':>10} {'Cred nouv':>10} {'Diff':>5}"
        )

        for ligne_id, before in lignes_avant.items():
            from apps.inscriptions.models import InscriptionAdministrative
            insc = InscriptionAdministrative.objects.get(id=before['inscription_admin_id'])
            moy_new, credits_new, _ = NoteCalculService.calculer_moyenne_annuelle(
                etudiant=insc.etudiant,
                annee_univ=pv.annee_univ,
                niveau=pv.niveau,
            )
            moy_old = before['moyenne_annuelle'] or Decimal('0')
            cred_old = before['credits_annuels'] or 0
            diff_moy = moy_new - moy_old
            diff_cred = credits_new - cred_old

            if diff_moy != 0 or diff_cred != 0:
                nb_diff += 1
                marker = self.style.WARNING('  DIFF')
                self.stdout.write(
                    f"  {before['matricule']:<12} {str(moy_old):>9} {str(moy_new):>9} "
                    f"{str(diff_moy):>7}  {cred_old:>10} {credits_new:>10} {diff_cred:+5}{marker}"
                )

        if nb_diff == 0:
            self.stdout.write(self.style.SUCCESS(
                f"\n  Aucun ecart : la nouvelle logique donne EXACTEMENT les memes "
                f"resultats que l'ancienne pour les {nb_total} etudiants de ce PV."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f"\n  {nb_diff}/{nb_total} etudiants auront des valeurs differentes "
                f"apres recalcul. Verifier l'impact metier avant --apply."
            ))
