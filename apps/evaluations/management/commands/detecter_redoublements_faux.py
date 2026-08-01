"""
Commande d'audit : liste les étudiants exclus à tort à cause d'années blanches
mal comptabilisées dans l'ancien système.

Usage :
  python manage.py detecter_redoublements_faux
  python manage.py detecter_redoublements_faux --corriger   # corrige les statuts
"""
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = (
        "Détecte les étudiants exclus à tort (année blanche comptée comme redoublement). "
        "Utilise la table Progression.consomme_droit_redoublement pour le comptage précis."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--corriger', action='store_true',
            help="Remet les étudiants détectés en statut 'actif' (à valider manuellement).",
        )

    def handle(self, *args, **options):
        from apps.absence.models import Etudiant
        from apps.inscriptions.models import Progression

        suspects = []
        for etu in Etudiant.objects.filter(statut='exclu'):
            vrais_redoublements = Progression.objects.filter(
                etudiant=etu,
                consomme_droit_redoublement=True,
            ).count()
            if vrais_redoublements < 2:
                suspects.append((etu, vrais_redoublements))

        if not suspects:
            self.stdout.write(self.style.SUCCESS('Aucun étudiant exclu à tort détecté.'))
            return

        self.stdout.write(
            self.style.WARNING(f'{len(suspects)} étudiant(s) potentiellement exclus à tort :')
        )
        for etu, n in suspects:
            self.stdout.write(
                f'  {etu.numero_dossier} — {etu} : {n} vrai(s) redoublement(s) (exclusion à réviser)'
            )

        if options['corriger']:
            with transaction.atomic():
                for etu, _ in suspects:
                    etu.statut = 'actif'
                    etu.save(update_fields=['statut'])
            self.stdout.write(
                self.style.SUCCESS(
                    f'{len(suspects)} étudiant(s) remis en statut actif. '
                    'Vérifiez chaque cas manuellement avant la prochaine délibération.'
                )
            )
        else:
            self.stdout.write(
                'Relancez avec --corriger pour remettre ces étudiants en statut actif.'
            )
