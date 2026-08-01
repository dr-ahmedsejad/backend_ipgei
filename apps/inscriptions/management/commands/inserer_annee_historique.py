"""
Section 2 institution_V1 — Crée les structures pour une année historique.

Crée :
- Year (si absent)
- Departement clones (à partir d'une année source) avec institution préservée
- SessionEvaluation (SN-I + SN-P, optionnellement SR-I + SR-P)

Pas de création de Filiere/Module/EM/Semestre — ces entités sont stables.

Usage :
    python manage.py inserer_annee_historique --annee 2024-2025 --source-annee 2025-2026
    python manage.py inserer_annee_historique --annee 2023-2024 --source-annee 2025-2026 --avec-rattrapage
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Crée Year, Departements clonés et Sessions pour une année historique."

    def add_arguments(self, parser):
        parser.add_argument('--annee', type=str, required=True,
                            help="Année à créer (ex : '2024-2025').")
        parser.add_argument('--source-annee', type=str, required=True,
                            help="Année source pour cloner Departements (ex : '2025-2026').")
        parser.add_argument('--institution', type=int, default=None,
                            help="ID de l'institution cible (défaut : principale).")
        parser.add_argument('--avec-rattrapage', action='store_true',
                            help="Crée aussi les sessions SR-I et SR-P (par défaut : seulement SN-I et SN-P).")

    @transaction.atomic
    def handle(self, *args, **opts):
        from apps.parametres.models import Year, Institution
        from apps.departement.models import Departement
        from apps.evaluations.models import SessionEvaluation

        # 1. Résoudre institution
        if opts['institution']:
            try:
                inst = Institution.objects.get(pk=opts['institution'])
            except Institution.DoesNotExist:
                raise CommandError(f"Institution #{opts['institution']} introuvable.")
        else:
            principales = list(Institution.objects.filter(est_principale=True))
            if len(principales) != 1:
                raise CommandError(
                    f"{len(principales)} institutions principales — désambiguïser avec --institution."
                )
            inst = principales[0]

        # 2. Year cible
        try:
            an_debut = int(opts['annee'].split('-')[0])
        except (ValueError, IndexError):
            raise CommandError(f"Format année invalide : {opts['annee']} (attendu : 'YYYY-YYYY').")

        annee, year_created = Year.objects.get_or_create(
            annee=opts['annee'],
            defaults={
                'est_active':   False,
                'est_cloturee': True,
                'date_debut':   date(an_debut, 9, 1),
                'date_fin':     date(an_debut + 1, 7, 31),
            },
        )
        msg = "creee" if year_created else "deja existante"
        self.stdout.write(self.style.SUCCESS(f"Year {annee.annee} {msg}."))

        # 3. Year source
        try:
            source = Year.objects.get(annee=opts['source_annee'])
        except Year.DoesNotExist:
            raise CommandError(f"Year source '{opts['source_annee']}' introuvable.")

        # 4. Cloner Departements
        depts_source = Departement.objects.filter(
            annee_universitaire=source.annee, institution=inst,
        )
        if not depts_source.exists():
            self.stdout.write(self.style.WARNING(
                f"Aucun Departement source pour {source.annee} et institution #{inst.id}."
            ))
        nb_clones = 0
        nb_existants = 0
        for d_src in depts_source:
            new_nom = (d_src.nom or '').replace(source.annee, annee.annee) or f"{d_src.code}-{annee.annee}"
            obj, created = Departement.objects.get_or_create(
                code=d_src.code,
                filiere=d_src.filiere,
                niveau=d_src.niveau,
                groupe=d_src.groupe,
                annee_universitaire=annee.annee,
                institution=inst,
                defaults={
                    'nom':         new_nom,
                    'description': d_src.description,
                    'decalage':    d_src.decalage,
                },
            )
            if created:
                nb_clones += 1
            else:
                nb_existants += 1
        self.stdout.write(self.style.SUCCESS(
            f"Departements : {nb_clones} clones, {nb_existants} deja existants."
        ))

        # 5. SessionEvaluation
        sessions_specs = [
            ('normale', 'Impairs'),
            ('normale', 'Pairs'),
        ]
        if opts['avec_rattrapage']:
            sessions_specs += [
                ('rattrapage', 'Impairs'),
                ('rattrapage', 'Pairs'),
            ]
        nb_sess = 0
        nb_sess_existants = 0
        for type_session, type_semestre in sessions_specs:
            code = f"{'SN' if type_session == 'normale' else 'SR'}-{type_semestre[0]}-{annee.annee}"
            obj, created = SessionEvaluation.objects.get_or_create(
                institution=inst,
                annee_univ=annee,
                type_session=type_session,
                type_semestre=type_semestre,
                defaults={
                    'code':                code,
                    'intitule':            f"Session {type_session} {type_semestre} {annee.annee}",
                    'est_close':           True,
                    'est_ouverte':         False,
                },
            )
            if created:
                nb_sess += 1
            else:
                nb_sess_existants += 1
        self.stdout.write(self.style.SUCCESS(
            f"Sessions : {nb_sess} crees, {nb_sess_existants} deja existantes."
        ))

        self.stdout.write(self.style.SUCCESS(
            f"\nOK : annee {annee.annee} prete pour imports."
        ))
