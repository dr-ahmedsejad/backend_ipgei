"""
Backfill des Filière/Departement/DepartementAcademique orphelins d'institution.

Pré-requis Section 0 du plan institution_V1 : corriger les FK institution=NULL
en les rattachant à l'institution principale (mono-institution).

Usage :
    python manage.py fix_orphelins_institution --dry-run         # voit ce qui serait fait
    python manage.py fix_orphelins_institution --apply           # applique
    python manage.py fix_orphelins_institution --institution 1 --apply
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Rattache les Filière/Departement orphelins à l'institution principale."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help="Affiche ce qui serait fait sans rien modifier (défaut).")
        parser.add_argument('--apply', action='store_true', default=False,
                            help="Applique les modifications.")
        parser.add_argument('--institution', type=int, default=None,
                            help="ID de l'institution cible (défaut : la principale).")

    def handle(self, *args, **opts):
        from apps.parametres.models import Institution
        from apps.scolarite.models import Filiere, DepartementAcademique
        from apps.departement.models import Departement

        if not opts['apply'] and not opts['dry_run']:
            opts['dry_run'] = True

        # Résoudre l'institution cible
        if opts['institution']:
            try:
                inst = Institution.objects.get(pk=opts['institution'])
            except Institution.DoesNotExist:
                raise CommandError(f"Institution #{opts['institution']} introuvable.")
        else:
            principales = list(Institution.objects.filter(est_principale=True))
            if len(principales) == 0:
                raise CommandError("Aucune institution avec est_principale=True. Bloquant.")
            if len(principales) > 1:
                raise CommandError(
                    f"{len(principales)} institutions principales — désambiguïser avec --institution <id>."
                )
            inst = principales[0]

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Institution cible : #{inst.id} {inst.acronyme}"
        ))

        # Compter les orphelins
        orph_filieres = Filiere.objects.filter(institution__isnull=True)
        orph_dept_acad = DepartementAcademique.objects.filter(institution__isnull=True)
        orph_departs = Departement.objects.filter(institution__isnull=True)

        nb_f = orph_filieres.count()
        nb_da = orph_dept_acad.count()
        nb_d = orph_departs.count()

        self.stdout.write(f"  Filieres orphelines             : {nb_f}")
        self.stdout.write(f"  DepartementsAcademiques orphelins: {nb_da}")
        self.stdout.write(f"  Departements orphelins          : {nb_d}")

        if nb_f + nb_da + nb_d == 0:
            self.stdout.write(self.style.SUCCESS("Rien a faire."))
            return

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING("\n[DRY-RUN] Aucune modification appliquee."))
            self.stdout.write("Relancer avec --apply pour executer.")
            return

        with transaction.atomic():
            updated_f = orph_filieres.update(institution=inst)
            updated_da = orph_dept_acad.update(institution=inst)
            updated_d = orph_departs.update(institution=inst)

        self.stdout.write(self.style.SUCCESS(
            f"\nOK : {updated_f} filieres, {updated_da} dept_acad, {updated_d} departements rattaches a #{inst.id}."
        ))
