"""
Optimise les images DEJA stockees (une passe sur l'existant), en complement des
save() de modeles qui optimisent les NOUVEAUX uploads :
  - photos etudiants & preinscriptions        -> JPEG progressif q88, 1280px max
  - logos / sceaux / signatures institution    -> PNG optimise, 512px (avec --logos)

  python manage.py compresser_photos --dry-run   # simulation (n'ecrit rien)
  python manage.py compresser_photos             # applique (photos)
  python manage.py compresser_photos --logos     # + logos/signatures institution
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Optimise les images deja stockees (photos ; + logos avec --logos)."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Simulation : calcule les gains sans rien ecrire.')
        parser.add_argument('--logos', action='store_true',
                            help="Inclure les logos / sceaux / signatures de l'institution.")

    def handle(self, *args, **opts):
        from core.image_utils import optimize_image, PHOTO_MAX_DIM, LOGO_MAX_DIM

        dry = opts['dry_run']
        stats = {'count': 0, 'before': 0, 'after': 0, 'errors': 0, 'missing': 0}

        def process(qs, field_name, keep_transparency):
            md = LOGO_MAX_DIM if keep_transparency else PHOTO_MAX_DIM
            for obj in qs.iterator():
                field = getattr(obj, field_name, None)
                if not field:
                    continue
                try:
                    if not field.storage.exists(field.name):
                        stats['missing'] += 1
                        continue
                    before = field.size
                    field.open('rb')
                    try:
                        cf, name = optimize_image(
                            field, max_dim=md, keep_transparency=keep_transparency)
                    finally:
                        field.close()
                    if not cf:
                        continue   # aucun gain -> on ne touche pas
                    after = cf.size
                    stats['count'] += 1
                    stats['before'] += before
                    stats['after'] += after
                    pct = 100 * (before - after) // before if before else 0
                    self.stdout.write(f'  {field.name}: {before // 1024} Ko -> {after // 1024} Ko (-{pct}%)')
                    if not dry:
                        old = field.name
                        field.save(name, cf, save=True)     # ecrit + met a jour le modele
                        if field.name != old:
                            field.storage.delete(old)        # retire l'ancien fichier
                except Exception as e:
                    stats['errors'] += 1
                    self.stderr.write(f'  ERREUR {field_name} #{obj.pk}: {e}')

        from apps.absence.models import Etudiant
        from apps.inscriptions.models import Preinscription

        self.stdout.write(self.style.MIGRATE_HEADING('Photos etudiants'))
        process(Etudiant.objects.exclude(photo='').exclude(photo__isnull=True), 'photo', False)

        self.stdout.write(self.style.MIGRATE_HEADING('Photos preinscriptions'))
        process(Preinscription.objects.exclude(photo='').exclude(photo__isnull=True), 'photo', False)

        if opts['logos']:
            from apps.parametres.models import Institution
            self.stdout.write(self.style.MIGRATE_HEADING('Logos / signatures institution'))
            for f in ('logo', 'logo_republique', 'logo_groupe', 'favicon',
                      'directeur_signature', 'commandant_signature'):
                process(
                    Institution.objects.exclude(**{f: ''}).exclude(**{f + '__isnull': True}),
                    f, True)

        b, a = stats['before'], stats['after']
        saved = b - a
        prefix = '[SIMULATION] ' if dry else ''
        if stats['missing']:
            self.stdout.write(self.style.WARNING(
                f"{stats['missing']} fichier(s) introuvable(s) sur le disque (ignore(s))."))
        self.stdout.write(self.style.SUCCESS(
            f"\n{prefix}{stats['count']} image(s) optimisee(s) : "
            f"{b // 1024} Ko -> {a // 1024} Ko "
            f"(gain {saved // 1024} Ko, {100 * saved // b if b else 0}%). "
            f"{stats['errors']} erreur(s)."))
