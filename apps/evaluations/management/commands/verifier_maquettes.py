"""
Audit de cohérence des maquettes — coefficient(module) vs Σ coefficients(EM).

Convention Art. 14 Arrêté 562 / Art. 19 Décret 2018-070 : la MGS de SIGA
(calculée sur les EM) n'égale la « moyenne pondérée des modules » que si le
coefficient déclaré de chaque module vaut la somme des coefficients de ses EM.

Usage :
  python manage.py verifier_maquettes                  # audit complet (lecture seule)
  python manage.py verifier_maquettes --filiere SEA    # une filière
  python manage.py verifier_maquettes --fix            # aligne coefficient = somme EM
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Audite les maquettes : structure (3-5 modules/semestre, 3 éléments "
        "max/module — Art. 8 Arrêté 562 / Art. 13-14 Décret 2018-070) et "
        "coefficient(module) = somme des coefficients des EM "
        "(convention Art. 14 Arrêté 562 / Art. 19 Décret 2018-070). "
        "--fix aligne les coefficients déclarés sur les sommes effectives."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--filiere', type=str, default=None,
            help='Code filière à auditer (défaut : toutes).',
        )
        parser.add_argument(
            '--fix', action='store_true',
            help='Met à jour Module.coefficient = somme des coefficients EM pour chaque anomalie.',
        )

    def handle(self, *args, **options):
        from apps.scolarite.models import Filiere
        from apps.modules.models import Module
        from apps.evaluations.services.coherence_maquette import (
            verifier_coherence_coefficients, verifier_structure_maquette,
        )

        filiere = None
        if options['filiere']:
            try:
                filiere = Filiere.objects.get(code__iexact=options['filiere'])
            except Filiere.DoesNotExist:
                self.stderr.write(self.style.ERROR(
                    f"Filière «{options['filiere']}» introuvable."))
                return

        # ── Section 2 : structure (Art. 8 Arrêté 562 / Art. 13-14 Décret) ──
        # 3-5 modules par semestre (2 en S4 LP, 1 en S6), 3 éléments max/module.
        # Lecture seule (pas de --fix possible : décision de maquette).
        filieres = [filiere] if filiere else list(
            Filiere.objects.filter(est_active=True).order_by('code'))
        msgs_structure = []
        for f in filieres:
            msgs_structure.extend(verifier_structure_maquette(f))

        self.stdout.write(self.style.MIGRATE_HEADING(
            '== Structure des maquettes (Art. 8 / Art. 13-14) =='))
        if not msgs_structure:
            self.stdout.write(self.style.SUCCESS(
                'OK — structure conforme (modules par semestre, 3 elements max).'))
        else:
            self.stdout.write(self.style.WARNING(
                f'{len(msgs_structure)} anomalie(s) de structure :'))
            for msg in msgs_structure:
                self.stdout.write(f'  - {msg}')

        # ── Section 1 : coefficients (Art. 14 / Art. 19) ────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING(
            '\n== Coefficients (Art. 14 / Art. 19) =='))
        anomalies = verifier_coherence_coefficients(filiere=filiere)

        if not anomalies:
            self.stdout.write(self.style.SUCCESS(
                'OK — tous les modules ont coefficient = somme des coefficients EM.'))
            return

        self.stdout.write(self.style.WARNING(
            f'{len(anomalies)} module(s) incohérent(s) :\n'))
        self.stdout.write(
            f"{'Filière':<10} {'Sem.':<6} {'Module':<16} "
            f"{'Coeff déclaré':>14} {'somme EM':>11} {'Nb EM':>6}"
        )
        for a in anomalies:
            self.stdout.write(
                f"{a['filiere_code']:<10} {a['semestre_code']:<6} {a['module_code']:<16} "
                f"{a['coefficient_module']:>14} {a['somme_coefficients_em']:>11} {a['nb_em']:>6}"
            )

        if not options['fix']:
            self.stdout.write(self.style.NOTICE(
                '\nLecture seule. Relancer avec --fix pour aligner '
                'coefficient = somme des coefficients EM.'))
            return

        fixed = 0
        for a in anomalies:
            Module.objects.filter(pk=a['module_id']).update(
                coefficient=a['somme_coefficients_em'],
            )
            fixed += 1
        self.stdout.write(self.style.SUCCESS(
            f'\n{fixed} module(s) aligné(s) : coefficient = somme des coefficients EM.'))
