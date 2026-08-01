"""
Recalcul des moyennes (éléments → modules → semestres) pour appliquer
le nouvel arrondi ROUND_HALF_UP (15.125 -> 15.13) ou tout autre changement
de logique de calcul.

Usage :
    python manage.py recalculer_moyennes --dry-run                          # défaut, voit ce qui serait fait
    python manage.py recalculer_moyennes --apply                            # toutes sessions, tous niveaux
    python manage.py recalculer_moyennes --session 1 --apply                # une seule session
    python manage.py recalculer_moyennes --annee 2023-2024 --apply          # toutes sessions d'une année
    python manage.py recalculer_moyennes --apply --skip-modules             # ne refait que éléments + semestres

Par défaut on recalcule TOUT : éléments, modules, semestres.
Les sessions clôturées sont ignorées sauf si --inclure-cloturees.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Recalcule les moyennes (elements -> modules -> semestres) pour les sessions choisies."

    def add_arguments(self, parser):
        parser.add_argument('--session', type=int, default=None,
                            help="ID d'une session specifique (sinon : toutes).")
        parser.add_argument('--annee', type=str, default=None,
                            help="Annee universitaire (ex: '2023-2024'). Filtre les sessions.")
        parser.add_argument('--institution', type=int, default=None)
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help="(defaut) N'applique pas les recalculs.")
        parser.add_argument('--apply', action='store_true', default=False,
                            help="Execute les recalculs.")
        parser.add_argument('--skip-elements', action='store_true', default=False)
        parser.add_argument('--skip-modules', action='store_true', default=False)
        parser.add_argument('--skip-semestres', action='store_true', default=False)
        parser.add_argument('--inclure-cloturees', action='store_true', default=False,
                            help="Inclut aussi les sessions cloturees (par defaut elles sont ignorees).")

    def handle(self, *args, **opts):
        from apps.parametres.models import Year, Institution
        from apps.evaluations.models import SessionEvaluation
        from apps.evaluations.services.calcul_notes import NoteCalculService

        if not opts['apply'] and not opts['dry_run']:
            opts['dry_run'] = True

        # Resoudre le scope
        qs = SessionEvaluation.objects.all()
        if opts['session']:
            qs = qs.filter(pk=opts['session'])
        if opts['annee']:
            try:
                annee = Year.objects.get(annee=opts['annee'])
                qs = qs.filter(annee_univ=annee)
            except Year.DoesNotExist:
                raise CommandError(f"Year '{opts['annee']}' introuvable.")
        if opts['institution']:
            try:
                inst = Institution.objects.get(pk=opts['institution'])
                qs = qs.filter(institution=inst)
            except Institution.DoesNotExist:
                raise CommandError(f"Institution #{opts['institution']} introuvable.")
        if not opts['inclure_cloturees']:
            qs = qs.filter(est_close=False)

        sessions = list(qs.select_related('annee_univ', 'institution').order_by('annee_univ__annee', 'type_semestre', 'type_session'))

        if not sessions:
            self.stdout.write(self.style.WARNING(
                "Aucune session a recalculer. "
                "(Astuce : utiliser --inclure-cloturees si les sessions sont fermees.)"
            ))
            return

        mode = "DRY-RUN" if opts['dry_run'] else "APPLY"
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== RECALCUL MOYENNES [{mode}] : {len(sessions)} session(s) ==="
        ))

        total_elem = total_mod = total_sem = 0
        cloturees_temporairement = []

        for sess in sessions:
            label = f"#{sess.id} {sess.code or sess.intitule or '?'} ({sess.annee_univ.annee if sess.annee_univ else '?'})"
            was_close = sess.est_close

            try:
                with transaction.atomic():
                    # Re-ouvrir temporairement si cloturee (necessaire pour bypasser les checks)
                    if was_close and not opts['dry_run']:
                        sess.est_close = False
                        sess.save(update_fields=['est_close'])
                        cloturees_temporairement.append(sess.id)

                    svc = NoteCalculService(sess)
                    e = m = s = 0

                    if not opts['skip_elements']:
                        if opts['dry_run']:
                            from apps.evaluations.models import ResultatElement
                            e = ResultatElement.objects.filter(session=sess).count()
                        else:
                            e = len(svc.calculer_tous_elements_session())

                    if not opts['skip_modules']:
                        if opts['dry_run']:
                            from apps.evaluations.models import ResultatModule
                            m = ResultatModule.objects.filter(session=sess).count()
                        else:
                            m = len(svc.calculer_tous_modules_session())

                    if not opts['skip_semestres']:
                        if opts['dry_run']:
                            from apps.evaluations.models import ResultatSemestre
                            s = ResultatSemestre.objects.filter(session=sess).count()
                        else:
                            s = len(svc.calculer_tous_semestres_session())

                    total_elem += e
                    total_mod += m
                    total_sem += s

                    # Re-cloturer si on l'avait reouverte
                    if was_close and not opts['dry_run']:
                        sess.est_close = True
                        sess.save(update_fields=['est_close'])

                    self.stdout.write(
                        f"  {label} : {e} elements, {m} modules, {s} semestres"
                        + (" (etait close)" if was_close else "")
                    )

            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"  {label} : ERREUR {exc}"))
                # Si on a re-ouvert mais erreur : la transaction rollback restaure est_close=True
                # mais notre cloturees_temporairement reste, donc on retire
                if sess.id in cloturees_temporairement:
                    cloturees_temporairement.remove(sess.id)

        # Resume
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== TOTAL : {total_elem} elements, {total_mod} modules, {total_sem} semestres ==="
        ))

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING(
                "\n[DRY-RUN] Aucun recalcul effectue. Relancer avec --apply."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "\nRECALCUL TERMINE."
            ))
            if cloturees_temporairement:
                self.stdout.write(
                    f"  Sessions reouvertes puis recloturees : {cloturees_temporairement}"
                )
