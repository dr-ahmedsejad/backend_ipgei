"""
Commande de rattrapage : crée les comptes portail pour les étudiants actifs
qui n'en ont pas encore. Idempotente — sans effet si le compte existe déjà.

Usage :
    python manage.py create_student_accounts
    python manage.py create_student_accounts --dry-run
    python manage.py create_student_accounts --filiere-id 3
"""
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

User = get_user_model()


class Command(BaseCommand):
    help = "Crée les comptes portail (rôle 'etudiant') pour les étudiants actifs sans compte."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Affiche ce qui serait fait sans modifier la base.',
        )
        parser.add_argument(
            '--filiere-id', type=int, default=None,
            help='Limiter à une filière spécifique.',
        )

    def handle(self, *args, **options):
        from apps.absence.models import Etudiant

        dry_run    = options['dry_run']
        filiere_id = options['filiere_id']

        qs = Etudiant.objects.filter(statut='actif', user__isnull=True)
        if filiere_id:
            qs = qs.filter(filiere_id=filiere_id)

        total   = qs.count()
        created = 0
        skipped = 0

        self.stdout.write(f"Etudiants actifs sans compte : {total}")
        if dry_run:
            self.stdout.write(self.style.WARNING("Mode dry-run - aucune modification."))

        linked = 0
        for etudiant in qs.iterator():
            cni  = (etudiant.cni  or '').strip()
            nbac = (etudiant.nbac or '').strip()
            mat  = (etudiant.matricule or '').strip()

            if not cni or not nbac:
                self.stdout.write(
                    self.style.WARNING(f"  SKIP {etudiant.matricule} - CNI ou NBAC manquant")
                )
                skipped += 1
                continue

            nom_complet = f"{etudiant.prenom_fr or ''} {etudiant.nom_fr or etudiant.nom}".strip()
            email_cible = f"{mat}@isms.esp.mr"

            existing = User.objects.filter(username=cni).first() or User.objects.filter(username=mat).first()
            if existing:
                # Compte deja present : si role='etudiant' et non lie a un Etudiant -> rattacher.
                if existing.role != 'etudiant':
                    self.stdout.write(self.style.WARNING(
                        f"  SKIP {etudiant.matricule} - username '{existing.username}' deja pris (role={existing.role})"
                    ))
                    skipped += 1
                    continue
                if hasattr(existing, 'etudiant_profile') and existing.etudiant_profile_id and existing.etudiant_profile_id != etudiant.id:
                    self.stdout.write(self.style.WARNING(
                        f"  SKIP {etudiant.matricule} - User '{existing.username}' deja lie a un autre Etudiant"
                    ))
                    skipped += 1
                    continue
                if not dry_run:
                    existing.email = email_cible
                    existing.name  = nom_complet or existing.name
                    existing.save(update_fields=['email', 'name'])
                    etudiant.user = existing
                    etudiant.save(update_fields=['user'])
                self.stdout.write(self.style.SUCCESS(
                    f"  {'[DRY] ' if dry_run else ''}Compte deja existant -> rattache {etudiant.matricule} (login: {existing.username}, email: {email_cible})"
                ))
                linked += 1
                continue

            if not dry_run:
                new_user = User.objects.create_user(
                    username         = cni,
                    password         = nbac,
                    email            = email_cible,
                    name             = nom_complet,
                    role             = 'etudiant',
                    doit_changer_mdp = True,
                )
                etudiant.user = new_user
                etudiant.save(update_fields=['user'])

            self.stdout.write(
                self.style.SUCCESS(f"  {'[DRY] ' if dry_run else ''}Compte cree -> {etudiant.matricule} (login: {cni}, email: {email_cible})")
            )
            created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\nTermine : {created} crees, {linked} rattaches, {skipped} ignores"
                f" {'(simulation)' if dry_run else ''}."
            )
        )
