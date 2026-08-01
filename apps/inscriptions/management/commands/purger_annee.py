"""
Section 4 institution_V1 — Purge sélective d'une année universitaire.

Supprime toutes les données opérationnelles d'une année (étudiants, inscriptions,
notes, sessions, PV, progressions) tout en conservant l'objet `Year` lui-même
(qui est référencé par les modules emplois/suivi/vacation via CharField).

Filtres scopés sur (annee, institution) pour protection multi-institution future.
Ordre EXACT du bas de la chaîne FK PROTECT vers le haut.

Usage :
    python manage.py purger_annee --annee 2025-2026                   # dry-run par défaut
    python manage.py purger_annee --annee 2025-2026 --apply --confirm-token <token>

Génère le token avec --show-token (affiche puis quitte) :
    python manage.py purger_annee --annee 2025-2026 --show-token
"""
import hashlib

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q


class Command(BaseCommand):
    help = "Purge selective d'une annee universitaire (donnees operationnelles)."

    def add_arguments(self, parser):
        parser.add_argument('--annee', type=str, required=True)
        parser.add_argument('--institution', type=int, default=None)
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help="(défaut) N'applique pas les suppressions.")
        parser.add_argument('--apply', action='store_true', default=False,
                            help="Exécute la purge.")
        parser.add_argument('--confirm-token', type=str, default=None,
                            help="Token de confirmation (cf. --show-token).")
        parser.add_argument('--show-token', action='store_true', default=False,
                            help="Affiche le token attendu et quitte.")
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
                f"{len(principales)} institutions principales — désambiguïser avec --institution."
            )
        return principales[0]

    def _compute_token(self, annee_str, inst_id, nb_etudiants):
        # Token simple basé sur annee + institution + nb_etudiants — non secret mais anti-fat-finger.
        raw = f'PURGE-{annee_str}-INST{inst_id}-N{nb_etudiants}'
        return hashlib.sha256(raw.encode()).hexdigest()[:12].upper()

    def handle(self, *args, **opts):
        from apps.parametres.models import Year
        from apps.absence.models import Etudiant
        from apps.inscriptions.models import (
            InscriptionAdministrative, InscriptionPedagogique, InscriptionElement,
            Derogation, Progression,
        )
        from apps.evaluations.models import (
            SessionEvaluation, PVDeliberation, LigneDeliberation, Note,
            ResultatElement, ResultatSemestre, ResultatModule,
            RachatNote, ObligationRattrapage, AnonymatSession,
            JustificatifAnneeBlanche,
        )
        from apps.documents.models import DocumentOfficiel, RegistreDiplome

        try:
            annee = Year.objects.get(annee=opts['annee'])
        except Year.DoesNotExist:
            raise CommandError(f"Year '{opts['annee']}' introuvable.")
        inst = self._resolve_institution(opts)

        # Compter étudiants à purger pour token
        nb_etudiants_inst = InscriptionAdministrative.objects.filter(
            annee_univ=annee, institution=inst,
        ).values('etudiant_id').distinct().count()

        token = self._compute_token(annee.annee, inst.id, nb_etudiants_inst)

        if opts['show_token']:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"Token attendu pour purge {annee.annee} institution #{inst.id} "
                f"({nb_etudiants_inst} etudiants) :"
            ))
            self.stdout.write(self.style.SUCCESS(f"  {token}"))
            return

        # Pré-conditions
        if annee.est_active and opts['apply']:
            raise CommandError(
                f"Year '{annee.annee}' est encore active (est_active=True). "
                "Désactiver avant purge."
            )
        if RegistreDiplome.objects.filter(
            Q(annee_universitaire=annee.annee) | Q(etudiant__inscriptions_admin__annee_univ=annee),
            institution=inst,
        ).exists():
            raise CommandError(
                f"RegistreDiplome present pour {annee.annee} — modele immuable, purge impossible."
            )

        if not opts['dry_run'] and not opts['apply']:
            opts['dry_run'] = True

        if opts['apply']:
            if opts['confirm_token'] != token:
                raise CommandError(
                    f"Token invalide. Attendu : {token}\n"
                    f"Recuperez-le avec --show-token."
                )

        with transaction.atomic():
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n=== PURGE {annee.annee} INSTITUTION #{inst.id} ==="
            ))

            stats = {}

            # 1. RachatNote
            stats['RachatNote'] = RachatNote.objects.filter(
                Q(pv__annee_univ=annee) | Q(pv__session__annee_univ=annee),
                pv__institution=inst,
            ).delete()[0]

            # 2. ObligationRattrapage
            stats['ObligationRattrapage'] = ObligationRattrapage.objects.filter(
                ligne__pv__annee_univ=annee, ligne__pv__institution=inst,
            ).delete()[0]

            # 3. JustificatifAnneeBlanche
            stats['JustificatifAnneeBlanche'] = JustificatifAnneeBlanche.objects.filter(
                ligne_deliberation__pv__annee_univ=annee, ligne_deliberation__pv__institution=inst,
            ).delete()[0]

            # 4. AnonymatSession
            stats['AnonymatSession'] = AnonymatSession.objects.filter(
                Q(session__annee_univ=annee, session__institution=inst)
                | Q(inscription_admin__annee_univ=annee, inscription_admin__institution=inst),
            ).delete()[0]

            # 5. Note
            stats['Note'] = Note.objects.filter(
                session__annee_univ=annee, session__institution=inst,
            ).delete()[0]

            # 6. ResultatElement
            stats['ResultatElement'] = ResultatElement.objects.filter(
                session__annee_univ=annee, session__institution=inst,
            ).delete()[0]
            stats['ResultatModule'] = ResultatModule.objects.filter(
                session__annee_univ=annee, session__institution=inst,
            ).delete()[0]
            stats['ResultatSemestre'] = ResultatSemestre.objects.filter(
                session__annee_univ=annee, session__institution=inst,
            ).delete()[0]

            # 7. Progression
            stats['Progression'] = Progression.objects.filter(
                Q(annee_source=annee) | Q(annee_cible=annee), institution=inst,
            ).delete()[0]

            # 8. LigneDeliberation
            stats['LigneDeliberation'] = LigneDeliberation.objects.filter(
                pv__annee_univ=annee, pv__institution=inst,
            ).delete()[0]

            # 9. PVDeliberation
            stats['PVDeliberation'] = PVDeliberation.objects.filter(
                Q(annee_univ=annee) | Q(session__annee_univ=annee), institution=inst,
            ).delete()[0]

            # 10. InscriptionElement
            stats['InscriptionElement'] = InscriptionElement.objects.filter(
                inscription_ped__inscription_admin__annee_univ=annee,
                inscription_ped__inscription_admin__institution=inst,
            ).delete()[0]

            # 11. InscriptionPedagogique
            stats['InscriptionPedagogique'] = InscriptionPedagogique.objects.filter(
                inscription_admin__annee_univ=annee, inscription_admin__institution=inst,
            ).delete()[0]

            # 12. InscriptionAdministrative
            stats['InscriptionAdministrative'] = InscriptionAdministrative.objects.filter(
                annee_univ=annee, institution=inst,
            ).delete()[0]

            # 13. SessionEvaluation
            stats['SessionEvaluation'] = SessionEvaluation.objects.filter(
                annee_univ=annee, institution=inst,
            ).delete()[0]

            # 14. Derogation
            stats['Derogation'] = Derogation.objects.filter(
                annee_univ=annee, institution=inst,
            ).delete()[0]

            # 15. DocumentOfficiel
            stats['DocumentOfficiel'] = DocumentOfficiel.objects.filter(
                annee_universitaire=annee.annee, institution=inst,
            ).delete()[0]

            # 16. Etudiants orphelins (pour cette institution)
            if opts['purger_etudiants_orphelins']:
                # Etudiants de l'institution sans aucune InscriptionAdministrative restante
                orphelins = Etudiant.objects.filter(
                    departement__institution=inst,
                ).exclude(
                    inscriptions_admin__isnull=False,
                )
                stats['Etudiant'] = orphelins.count()
                if not opts['dry_run']:
                    orphelins.delete()

            # 17. CustomUser orphelins
            if opts['purger_comptes_orphelins']:
                from django.contrib.auth import get_user_model
                User = get_user_model()
                orph_users = User.objects.filter(
                    role='etudiant', etudiant_profile__isnull=True,
                )
                stats['CustomUser'] = orph_users.count()
                if not opts['dry_run']:
                    orph_users.delete()

            # Affichage stats
            for key, n in stats.items():
                self.stdout.write(f"  {key:30s} : {n}")

            if opts['dry_run']:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING(
                    "\n[DRY-RUN] Aucune modification appliquee."
                ))
                self.stdout.write(
                    f"Pour appliquer : --apply --confirm-token {token}"
                )
            else:
                self.stdout.write(self.style.SUCCESS(
                    f"\nPURGE EFFECTUEE pour {annee.annee} institution #{inst.id}."
                ))
