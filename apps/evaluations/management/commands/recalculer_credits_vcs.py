"""
Commande de correction : recalcule tous les ResultatSemestre existants pour
appliquer la règle Art. 15 (capitalisation 30 crédits si semestre validé,
y compris pour les modules VCS compensés).

Bug corrigé : avant cette commande, les modules VCS donnaient 0 crédit,
ce qui sous-évaluait les crédits semestriels des étudiants.

Usage :
  python manage.py recalculer_credits_vcs --dry-run
  python manage.py recalculer_credits_vcs
"""
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Recalcule les ResultatSemestre pour corriger les crédits VCS (Art. 15)."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument(
            '--all-admis', action='store_true',
            help='Recalculer tous les semestres admis (pas seulement ceux <30 crédits) — '
                 'utile pour propager les codes VCS sur les éléments.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        from apps.evaluations.models import ResultatSemestre, ResultatModule, ResultatElement
        from apps.evaluations.services.calcul_notes import NoteCalculService

        dry = options['dry_run']
        if dry:
            self.stdout.write(self.style.WARNING('Mode dry-run : rollback final, aucune persistance.'))

        # ── Normalisation des codes module → uniquement V ou NV ─────────────
        # Anciens codes possibles : V, VCI, VCS, R, NV, NVO, E
        # Nouveaux codes module   : V (validé d'une façon ou d'une autre) ou NV
        n_mod_normalises = (
            ResultatModule.objects
            .filter(code_statut__in=['VCI', 'VCS', 'R'])
            .update(code_statut='V')
        )
        n_mod_normalises += (
            ResultatModule.objects
            .filter(code_statut__in=['NVO', 'E'])
            .update(code_statut='NV')
        )
        self.stdout.write(f'Modules normalisés : {n_mod_normalises} (V/NV uniquement)')

        # ── Normalisation des codes EM → V, VCI, VCS, NV, E ─────────────────
        # Suppression de NVO (devient NV) et R (devient V)
        n_em_normalises  = ResultatElement.objects.filter(code_statut='NVO').update(code_statut='NV')
        n_em_normalises += ResultatElement.objects.filter(code_statut='R').update(code_statut='V', est_valide=True)
        self.stdout.write(f'Éléments normalisés : {n_em_normalises} (NVO->NV, R->V)')

        if options['all_admis']:
            suspects = ResultatSemestre.objects.filter(est_admis=True)
            self.stdout.write('Mode --all-admis : recalcul de TOUS les semestres validés.')
        else:
            suspects = ResultatSemestre.objects.filter(
                est_admis=True, credits_valides__lt=30,
            )
        suspects = suspects.select_related('inscription_ped__semestre', 'session')

        total = suspects.count()
        self.stdout.write(f'{total} ResultatSemestre suspect(s) (admis & crédits < 30).')

        corriges = 0
        for rs in suspects:
            ancien = rs.credits_valides
            try:
                NoteCalculService(rs.session).calculer_semestre(rs.inscription_ped)
                rs.refresh_from_db()
                if rs.credits_valides != ancien:
                    corriges += 1
                    self.stdout.write(
                        f'  IP {rs.inscription_ped_id} sem {rs.inscription_ped.semestre.code_semestre} '
                        f'session {rs.session.code} : {ancien} -> {rs.credits_valides}'
                    )
            except Exception as exc:
                self.stdout.write(self.style.ERROR(
                    f'  Erreur IP {rs.inscription_ped_id} session {rs.session.code} : {exc}'
                ))

        # ── Nettoyage des ObligationRattrapage obsolètes ─────────────────────
        # Supprime les obligations dont l'élément est désormais validé
        # (V, VCI, VCS, R) ou dont l'étudiant est admis (semestre validé).
        from apps.evaluations.models import ObligationRattrapage, ResultatElement

        # Cas 1 : étudiant admis → aucune obligation de rattrapage nécessaire
        obs_admis = ObligationRattrapage.objects.filter(
            ligne__decision__in=['admis', 'rachat'],
        )
        n_admis = obs_admis.count()

        # Cas 2 : élément désormais validé (V/VCI/VCS/R) → obligation obsolète
        # Note : on ne peut pas filtrer côté ORM car ResultatElement et
        # InscriptionElement sont liés indirectement par session ; on itère.
        obs_valides_ids = []
        for ob in ObligationRattrapage.objects.exclude(
            ligne__decision__in=['admis', 'rachat'],
        ).select_related('ligne__pv__session'):
            session = ob.ligne.pv.session
            if not session:
                continue
            res = ResultatElement.objects.filter(
                session=session,
                inscription_element=ob.inscription_element,
            ).first()
            if res and res.code_statut in ('V', 'VCI', 'VCS', 'R'):
                obs_valides_ids.append(ob.pk)
        n_valides = len(obs_valides_ids)

        self.stdout.write(
            f'Obligations obsolètes : {n_admis} (étudiants admis) + '
            f'{n_valides} (éléments validés)'
        )
        if not dry:
            obs_admis.delete()
            ObligationRattrapage.objects.filter(pk__in=obs_valides_ids).delete()

        if dry:
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING('Dry-run : rollback effectué.'))
        self.stdout.write(self.style.SUCCESS(
            f'Terminé : {corriges} / {total} ResultatSemestre corrigés, '
            f'{n_admis + n_valides} obligations obsolètes supprimées.'
        ))
