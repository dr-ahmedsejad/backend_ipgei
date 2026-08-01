"""
Commande de migration : crée rétroactivement les lignes Progression
à partir des PV annuels clos historiques.

Usage :
  python manage.py backfill_progressions --dry-run   # simulation sans écriture
  python manage.py backfill_progressions             # exécution réelle
"""
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Backfill de la table Progression depuis les PV annuels clos existants."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Simule sans écrire en base (rollback final).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        from apps.evaluations.models import PVDeliberation
        from apps.inscriptions.models import Progression, InscriptionAdministrative
        from apps.inscriptions.services.progression import ProgressionService

        dry = options['dry_run']
        if dry:
            self.stdout.write(self.style.WARNING('Mode dry-run : aucune modification ne sera persistée.'))

        pvs = PVDeliberation.objects.filter(est_clos=True, type_pv='annuel').select_related(
            'filiere', 'annee_univ', 'session',
        )

        total = 0
        for pv in pvs:
            try:
                svc   = ProgressionService(pv)
                stats = svc.generer_progressions()
                self.stdout.write(f'PV #{pv.pk} ({pv.filiere}) → {stats}')
                total += sum(stats.values())

                # Lier les InscriptionAdministrative N+1 déjà existantes
                for prog in Progression.objects.filter(ligne_deliberation__pv=pv):
                    insc = InscriptionAdministrative.objects.filter(
                        etudiant=prog.etudiant,
                        annee_univ=prog.annee_cible,
                    ).first()
                    if insc and prog.inscription_admin_creee_id is None:
                        prog.inscription_admin_creee = insc
                        prog.statut = 'executee'
                        # Détecter les changements de filière historiques
                        if insc.filiere_id != prog.filiere_cible_id:
                            prog.filiere_cible = insc.filiere
                            prog.motif_modification = '[Migration] Changement de filière historique détecté'
                            self.stdout.write(
                                self.style.WARNING(
                                    f'  ↳ Étudiant {prog.matricule} : filière changée '
                                    f'{prog.filiere_source.code} → {insc.filiere.code}'
                                )
                            )
                        prog.save(update_fields=[
                            'inscription_admin_creee', 'statut',
                            'filiere_cible', 'motif_modification',
                        ])

            except Exception as exc:
                self.stdout.write(self.style.ERROR(f'PV #{pv.pk} : erreur — {exc}'))

        if dry:
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING('Dry-run : rollback effectué, aucune donnée persistée.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Terminé : {total} progressions traitées.'))
