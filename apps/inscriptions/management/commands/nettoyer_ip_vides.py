"""
Supprime les InscriptionPedagogique « coquilles » : AUCUN element
(InscriptionElement), AUCUN resultat stocke (ResultatSemestre / ResultatModule).

Ces coquilles subsistent apres suppression de dettes (l'IE en dette est supprimee
mais l'IP qui la contenait reste a vide) et font apparaitre des semestres VIDES
sur l'attestation d'inscription (cas 23641 : S1/S2 vides en 2025-2026).

GARDE-FOUS (« fait attention ») :
  - perimetre limite a UNE annee (--annee, defaut = annee active). Les autres
    annees ne sont JAMAIS touchees ;
  - ne supprime QUE les IP a 0 EM ET 0 ResultatSemestre ET 0 ResultatModule.
    ResultatSemestre/ResultatModule ont un on_delete=CASCADE sur l'IP : une IP
    portant un resultat stocke est CONSERVEE (sinon suppression en cascade) ;
  - dry-run par defaut ; re-verification ligne par ligne juste avant suppression ;
  - suppression en transaction atomique.

Usage :
  python manage.py nettoyer_ip_vides                  # dry-run (annee active)
  python manage.py nettoyer_ip_vides --annee 2025-2026
  python manage.py nettoyer_ip_vides --annee 2025-2026 --apply
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count


class Command(BaseCommand):
    help = "Supprime les InscriptionPedagogique vides (0 EM, 0 resultat) d'une annee."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', default=False,
                            help='Effectue la suppression. Par defaut : dry-run.')
        parser.add_argument('--annee', type=str, default=None,
                            help="Annee a nettoyer (ex 2025-2026). Defaut : annee ACTIVE. "
                                 "GARDE-FOU : ne touche QUE cette annee.")

    def handle(self, *args, **opts):
        from apps.inscriptions.models import InscriptionPedagogique
        from apps.parametres.models import Year

        annee_str = opts.get('annee')
        if not annee_str:
            ya = Year.objects.filter(est_active=True).order_by('-annee').first()
            annee_str = ya.annee if ya else None
        if not annee_str:
            self.stdout.write(self.style.ERROR('Aucune annee active ; precisez --annee.'))
            return
        self.stdout.write(self.style.NOTICE(
            f"GARDE-FOU : perimetre limite a l'annee {annee_str} "
            f"(les autres annees ne seront PAS touchees)."))

        candidats = list(
            InscriptionPedagogique.objects
            .filter(inscription_admin__annee_univ__annee=annee_str)
            .annotate(n_ie=Count('inscriptions_elements', distinct=True),
                      n_rs=Count('resultats_semestre',     distinct=True),
                      n_rm=Count('resultats_module',       distinct=True))
            .filter(n_ie=0, n_rs=0, n_rm=0)
            .select_related('semestre', 'inscription_admin__etudiant',
                            'inscription_admin__annee_univ')
        )

        self.stdout.write(f'IP vides detectees (0 EM, 0 ResultatSemestre, 0 ResultatModule) : {len(candidats)}')
        for ip in candidats:
            ia = ip.inscription_admin
            self.stdout.write(
                f'  IP#{ip.id} matricule={ia.etudiant.matricule if ia and ia.etudiant_id else "?"} '
                f'annee={ia.annee_univ.annee if ia and ia.annee_univ else "?"} '
                f'sem={ip.semestre.code_semestre if ip.semestre else "?"}')

        if not candidats:
            self.stdout.write(self.style.SUCCESS('Rien a nettoyer.'))
            return
        if not opts['apply']:
            self.stdout.write(self.style.NOTICE(
                'Dry-run : aucune suppression. Relancer avec --apply pour supprimer.'))
            return

        # Re-verification ligne par ligne JUSTE AVANT la suppression : aucune IP hors
        # annee cible, aucune devenue non-vide entre-temps. Au moindre doute -> ABANDON.
        ids = []
        for ip in candidats:
            ia = ip.inscription_admin
            if not ia or not ia.annee_univ or ia.annee_univ.annee != annee_str:
                self.stdout.write(self.style.ERROR(
                    f'ABANDON : IP#{ip.id} hors annee {annee_str}. Aucune suppression.'))
                return
            if (ip.inscriptions_elements.exists()
                    or ip.resultats_semestre.exists()
                    or ip.resultats_module.exists()):
                self.stdout.write(self.style.ERROR(
                    f'ABANDON : IP#{ip.id} n\'est plus vide (EM/resultat apparu). Aucune suppression.'))
                return
            ids.append(ip.id)

        with transaction.atomic():
            deleted = InscriptionPedagogique.objects.filter(id__in=ids).delete()
            n = deleted[0]
            # Securite : avec 0 enfant, le total supprime == nombre d'IP visees.
            if n != len(ids):
                raise RuntimeError(
                    f'Suppression inattendue : {n} objets pour {len(ids)} IP visees '
                    f'({deleted[1]}). Transaction annulee.')
            self.stdout.write(self.style.SUCCESS(
                f'{n} IP vide(s) supprimee(s) (annee {annee_str}) en transaction atomique.'))
