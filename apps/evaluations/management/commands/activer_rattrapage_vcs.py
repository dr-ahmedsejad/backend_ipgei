"""Active (ou désactive) l'exception « rattrapage des éléments VCS » sur les
sessions d'évaluation d'une année universitaire.

Exception TEMPORAIRE, FIGÉE PAR SESSION : autoriser le rattrapage FACULTATIF des
éléments validés par compensation semestrielle (VCS), note plafonnée comme d'habitude.

Usage :
  python manage.py activer_rattrapage_vcs --annee 2023-2024 --on
  python manage.py activer_rattrapage_vcs --annee 2024-2025 --on
  python manage.py activer_rattrapage_vcs --annee 2023-2024 --off   # revenir en arrière

Retrait définitif (ex. 2025-2026) : NE RIEN FAIRE — les nouvelles sessions naissent
avec le drapeau à False par défaut, donc sans l'exception. Les années activées
restent figées (aucun recalcul rétroactif).
"""
from django.core.management.base import BaseCommand, CommandError

from apps.evaluations.models import SessionEvaluation


class Command(BaseCommand):
    help = "Active/désactive l'exception rattrapage VCS sur les sessions d'une année."

    def add_arguments(self, parser):
        parser.add_argument('--annee', required=True,
                            help="Année universitaire, ex. 2023-2024")
        g = parser.add_mutually_exclusive_group()
        g.add_argument('--on', action='store_true', help="Activer (défaut)")
        g.add_argument('--off', action='store_true', help="Désactiver")

    def handle(self, *args, **opts):
        annee = opts['annee']
        actif = not opts['off']   # --on (ou rien) → True ; --off → False

        # SESSIONS NORMALES UNIQUEMENT : le drapeau est lu lors de la génération des
        # obligations, qui tourne sur le PV de la session NORMALE. Le poser sur une
        # session de rattrapage n'aurait aucun effet.
        qs = SessionEvaluation.objects.filter(
            annee_univ__annee=annee, type_session='normale',
        )
        if not qs.exists():
            raise CommandError(f"Aucune session normale pour l'année {annee}.")

        n = qs.update(rattrapage_vcs_actif=actif)
        etat = 'ACTIVÉE' if actif else 'désactivée'
        self.stdout.write(self.style.SUCCESS(
            f"Exception rattrapage VCS {etat} sur {n} session(s) NORMALE(s) de {annee}."
        ))

        if not actif:
            # Retrait immédiat : purge les obligations VCS déjà créées sous l'exception
            # (sinon l'EM resterait rattrapable en saisie jusqu'à la prochaine régénération).
            from apps.evaluations.models import ObligationRattrapage
            deleted, _ = ObligationRattrapage.objects.filter(
                ligne__pv__session__in=qs, code_statut_initial='VCS',
            ).delete()
            self.stdout.write(f"  → {deleted} obligation(s) VCS résiduelle(s) supprimée(s).")

        self.stdout.write(
            "Pense à régénérer les obligations (peupler / generer-obligations sur le PV) "
            "puis recalculer pour que l'effet soit pris en compte."
        )
