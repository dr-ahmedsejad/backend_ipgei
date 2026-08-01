"""
Purge ciblée des étudiants d'un niveau pour une année donnée.

Plus prudent que `purger_annee` :
- Cible uniquement les InscriptionAdministrative (annee, niveau, institution)
- Cascade : Notes / Resultats / InscPed / InscEl / LigneDeliberation
- Supprime les Progressions où etudiant ∈ étudiants ciblés
- Supprime les PVDeliberation (niveau, annee, institution) vidés
- Supprime les étudiants orphelins (sans aucune autre InscAdm restante)
- Conserve : SessionEvaluation (mutualisée avec autres niveaux), Departements, Year

Usage :
    python manage.py purger_etudiants_niveau --annee 2025-2026 --niveau 1
    python manage.py purger_etudiants_niveau --annee 2025-2026 --niveau 1 --apply --confirm-token <token>
    python manage.py purger_etudiants_niveau --annee 2025-2026 --niveau 1 --show-token
"""
import hashlib

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q


class Command(BaseCommand):
    help = "Purge ciblee des etudiants d'un niveau pour une annee donnee."

    def add_arguments(self, parser):
        parser.add_argument('--annee', type=str, required=True)
        parser.add_argument('--niveau', type=int, required=True)
        parser.add_argument('--institution', type=int, default=None)
        parser.add_argument('--dry-run', action='store_true', default=False)
        parser.add_argument('--apply', action='store_true', default=False)
        parser.add_argument('--confirm-token', type=str, default=None)
        parser.add_argument('--show-token', action='store_true', default=False)
        parser.add_argument('--purger-etudiants-orphelins', action='store_true', default=True)
        parser.add_argument('--purger-comptes-orphelins', action='store_true', default=False)

    def _resolve_institution(self, opts):
        from apps.parametres.models import Institution
        if opts['institution']:
            try:
                return Institution.objects.get(pk=opts['institution'])
            except Institution.DoesNotExist:
                raise CommandError(f"Institution #{opts['institution']} introuvable.")
        principales = list(Institution.objects.filter(est_principale=True))
        if len(principales) != 1:
            raise CommandError(
                f"{len(principales)} institutions principales — desambiguiser avec --institution."
            )
        return principales[0]

    def _compute_token(self, annee_str, niveau, inst_id, nb_etudiants):
        raw = f'PURGE-{annee_str}-N{niveau}-INST{inst_id}-E{nb_etudiants}'
        return hashlib.sha256(raw.encode()).hexdigest()[:12].upper()

    def handle(self, *args, **opts):
        from apps.parametres.models import Year
        from apps.absence.models import Etudiant
        from apps.inscriptions.models import (
            InscriptionAdministrative, InscriptionPedagogique, InscriptionElement,
            Progression, Derogation,
        )
        from apps.evaluations.models import (
            PVDeliberation, LigneDeliberation, Note,
            ResultatElement, ResultatSemestre, ResultatModule,
            RachatNote, ObligationRattrapage, AnonymatSession,
            JustificatifAnneeBlanche,
        )
        from apps.documents.models import DocumentOfficiel

        try:
            annee = Year.objects.get(annee=opts['annee'])
        except Year.DoesNotExist:
            raise CommandError(f"Year '{opts['annee']}' introuvable.")
        niveau = opts['niveau']
        inst = self._resolve_institution(opts)

        # Cible
        ia_qs = InscriptionAdministrative.objects.filter(
            annee_univ=annee, niveau=niveau, institution=inst,
        )
        nb_etudiants = ia_qs.values('etudiant_id').distinct().count()
        token = self._compute_token(annee.annee, niveau, inst.id, nb_etudiants)

        if opts['show_token']:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"Token attendu pour purge L{niveau} {annee.annee} institution #{inst.id} "
                f"({nb_etudiants} etudiants) :"
            ))
            self.stdout.write(self.style.SUCCESS(f"  {token}"))
            return

        if not opts['dry_run'] and not opts['apply']:
            opts['dry_run'] = True

        if opts['apply']:
            if opts['confirm_token'] != token:
                raise CommandError(
                    f"Token invalide. Attendu : {token}\n"
                    f"Recuperer avec --show-token."
                )

        with transaction.atomic():
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n=== PURGE L{niveau} {annee.annee} INSTITUTION #{inst.id} "
                f"({nb_etudiants} etudiants) ==="
            ))

            etu_ids = list(ia_qs.values_list('etudiant_id', flat=True).distinct())
            ip_ids  = list(InscriptionPedagogique.objects.filter(inscription_admin__in=ia_qs).values_list('id', flat=True))
            ie_ids  = list(InscriptionElement.objects.filter(inscription_ped_id__in=ip_ids).values_list('id', flat=True))
            ligne_ids = list(LigneDeliberation.objects.filter(inscription_admin__in=ia_qs).values_list('id', flat=True))
            pv_ids = list(PVDeliberation.objects.filter(
                annee_univ=annee, niveau=niveau, institution=inst,
            ).values_list('id', flat=True))

            stats = {}

            # 1. RachatNote (lies aux PV ou lignes ciblees)
            stats['RachatNote'] = RachatNote.objects.filter(
                Q(pv_id__in=pv_ids) | Q(ligne_id__in=ligne_ids),
            ).delete()[0]

            # 2. ObligationRattrapage (lies a ligne ou inscription_element)
            stats['ObligationRattrapage'] = ObligationRattrapage.objects.filter(
                Q(ligne_id__in=ligne_ids) | Q(inscription_element_id__in=ie_ids),
            ).delete()[0]

            # 3. JustificatifAnneeBlanche (lies a ligne)
            stats['JustificatifAnneeBlanche'] = JustificatifAnneeBlanche.objects.filter(
                ligne_deliberation_id__in=ligne_ids,
            ).delete()[0]

            # 4. AnonymatSession (lies a inscription_admin)
            stats['AnonymatSession'] = AnonymatSession.objects.filter(
                inscription_admin__in=ia_qs,
            ).delete()[0]

            # 5. Note (lies a inscription_element)
            stats['Note'] = Note.objects.filter(
                inscription_element_id__in=ie_ids,
            ).delete()[0]

            # 6. Resultats
            stats['ResultatElement'] = ResultatElement.objects.filter(
                inscription_element_id__in=ie_ids,
            ).delete()[0]
            stats['ResultatSemestre'] = ResultatSemestre.objects.filter(
                inscription_ped_id__in=ip_ids,
            ).delete()[0]
            stats['ResultatModule'] = ResultatModule.objects.filter(
                inscription_ped_id__in=ip_ids,
            ).delete()[0]

            # 7. Progression (etudiant cible)
            stats['Progression'] = Progression.objects.filter(
                etudiant_id__in=etu_ids,
            ).delete()[0]

            # 8. LigneDeliberation
            stats['LigneDeliberation'] = LigneDeliberation.objects.filter(
                id__in=ligne_ids,
            ).delete()[0]

            # 9. PVDeliberation niveau cible
            stats['PVDeliberation'] = PVDeliberation.objects.filter(id__in=pv_ids).delete()[0]

            # 10. InscriptionElement
            stats['InscriptionElement'] = InscriptionElement.objects.filter(id__in=ie_ids).delete()[0]

            # 11. InscriptionPedagogique
            stats['InscriptionPedagogique'] = InscriptionPedagogique.objects.filter(id__in=ip_ids).delete()[0]

            # 12. InscriptionAdministrative
            stats['InscriptionAdministrative'] = ia_qs.delete()[0]

            # 13. DocumentOfficiel des etudiants concernes (sans filtre annee — purge totale)
            stats['DocumentOfficiel'] = DocumentOfficiel.objects.filter(
                etudiant_id__in=etu_ids,
            ).delete()[0]

            # 13bis. Derogation des etudiants concernes
            stats['Derogation'] = Derogation.objects.filter(
                etudiant_id__in=etu_ids,
            ).delete()[0]

            # 14. Etudiants orphelins
            if opts['purger_etudiants_orphelins']:
                orphelins = Etudiant.objects.filter(id__in=etu_ids).exclude(
                    inscriptions_admin__isnull=False,
                )
                stats['Etudiant'] = orphelins.count()
                if not opts['dry_run']:
                    orphelins.delete()

            # 15. CustomUser orphelins
            if opts['purger_comptes_orphelins']:
                from django.contrib.auth import get_user_model
                User = get_user_model()
                orph_users = User.objects.filter(
                    role='etudiant', etudiant_profile__isnull=True,
                )
                stats['CustomUser'] = orph_users.count()
                if not opts['dry_run']:
                    orph_users.delete()

            for key, n in stats.items():
                self.stdout.write(f"  {key:30s} : {n}")

            if opts['dry_run']:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING(
                    f"\n[DRY-RUN] Aucune modification appliquee."
                ))
                self.stdout.write(
                    f"Pour appliquer : --apply --confirm-token {token}"
                )
            else:
                self.stdout.write(self.style.SUCCESS(
                    f"\nPURGE EFFECTUEE : L{niveau} {annee.annee} institution #{inst.id}."
                ))
