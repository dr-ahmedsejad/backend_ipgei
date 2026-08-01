"""
Nettoie les InscriptionElement (est_dette=True) devenues obsoletes parce que
l'element source a ete valide entre-temps (par exemple une note brute < 10 mais
validee par compensation au niveau du module → code VCI, est_valide=True).

Cas typique : la Progression d'un etudiant a ete executee AVANT que la
deliberation du module ne transforme NV → VCI. La dette a ete creee a tort.

Usage :
  python manage.py nettoyer_dettes_obsoletes               # dry-run (par defaut)
  python manage.py nettoyer_dettes_obsoletes --apply       # supprime reellement
  python manage.py nettoyer_dettes_obsoletes --etudiant 24618  # filtre un etudiant
"""
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Supprime les dettes (InscriptionElement.est_dette=True) dont l'element source est desormais valide."

    def add_arguments(self, parser):
        parser.add_argument('--apply',    action='store_true', default=False,
                            help='Effectue la suppression. Par defaut : dry-run.')
        parser.add_argument('--etudiant', type=str, default=None,
                            help='Filtre par matricule etudiant (optionnel).')
        parser.add_argument('--annee', type=str, default=None,
                            help="Annee universitaire a regulariser (ex 2025-2026). "
                                 "Defaut : annee ACTIVE. GARDE-FOU : la commande "
                                 "ne touche QUE cette annee, jamais les precedentes.")

    def handle(self, *args, **opts):
        from apps.inscriptions.models import InscriptionElement
        from apps.parametres.models import Year
        from apps.evaluations.services.note_lecture import em_acquis_consolide

        apply_changes = opts['apply']
        matr          = opts['etudiant']

        # ── GARDE-FOU ANNÉE : on ne régularise QUE l'année active (ou --annee).
        # Les inscriptions des années PRÉCÉDENTES ne sont JAMAIS touchées.
        annee_str = opts.get('annee')
        if not annee_str:
            ya = Year.objects.filter(est_active=True).order_by('-annee').first()
            annee_str = ya.annee if ya else None
        if not annee_str:
            self.stdout.write(self.style.ERROR('Aucune annee active ; precisez --annee.'))
            return
        self.stdout.write(self.style.NOTICE(
            f'GARDE-FOU : périmètre limité à l\'année {annee_str} '
            f'(les années précédentes ne seront PAS touchées).'
        ))

        qs = InscriptionElement.objects.filter(
            est_dette=True,
            inscription_ped__inscription_admin__annee_univ__annee=annee_str,
        ).select_related(
            'em',
            'inscription_ped__inscription_admin__etudiant',
            'inscription_ped__inscription_admin__annee_univ',
            'annee_dette',
        )
        if matr:
            qs = qs.filter(inscription_ped__inscription_admin__etudiant__matricule=matr)

        total = qs.count()
        self.stdout.write(f'IE en dette analysees ({annee_str}) : {total}'
                          + (f' (filtre matricule={matr})' if matr else ''))

        obsoletes = []
        for ie in qs:
            etu = ie.inscription_ped.inscription_admin.etudiant
            if not ie.em_id:
                continue
            annee_ie = ie.inscription_ped.inscription_admin.annee_univ

            # Acquis selon le relevé CONSOLIDÉ (compensation/capitalisation
            # cross-année) → la dette est obsolète. Repli sur l'IE source brute.
            valide = em_acquis_consolide(etu, ie.em, annee_ie)
            if not valide and ie.annee_dette_id:
                ie_source = InscriptionElement.objects.filter(
                    inscription_ped__inscription_admin__etudiant=etu,
                    inscription_ped__inscription_admin__annee_univ=ie.annee_dette,
                    em=ie.em,
                ).first()
                if ie_source:
                    valide = (
                        ie_source.resultats.filter(est_valide=True).exists()
                        or ie_source.resultats.filter(code_statut__in=['V', 'VCI', 'VCS']).exists()
                    )
            if valide:
                obsoletes.append(ie)

        self.stdout.write(self.style.WARNING(
            f'\nDettes obsoletes detectees : {len(obsoletes)}'
        ))
        for ie in obsoletes:
            etu = ie.inscription_ped.inscription_admin.etudiant
            self.stdout.write(
                f'  matricule={etu.matricule} '
                f'em={ie.em.code_em if ie.em else "?"} '
                f'ie_id={ie.id} '
                f'annee_dette={ie.annee_dette.annee if ie.annee_dette else "?"}'
            )

        if not obsoletes:
            self.stdout.write(self.style.SUCCESS('\nRien a nettoyer.'))
            return

        if not apply_changes:
            self.stdout.write(self.style.NOTICE(
                f'\nDry-run : aucune suppression. Relancer avec --apply pour supprimer.'
            ))
            return

        # Assertion de sécurité : AUCUNE IE hors de l'année cible ne doit être
        # supprimée. Si une seule l'est, on refuse de continuer.
        hors_perimetre = [
            ie.id for ie in obsoletes
            if ie.inscription_ped.inscription_admin.annee_univ.annee != annee_str
        ]
        if hors_perimetre:
            self.stdout.write(self.style.ERROR(
                f'ABANDON : {len(hors_perimetre)} IE hors année {annee_str} '
                f'dans les candidats ({hors_perimetre[:10]}…). Aucune suppression.'
            ))
            return

        with transaction.atomic():
            ids = [ie.id for ie in obsoletes]
            n = InscriptionElement.objects.filter(id__in=ids).delete()[0]
            self.stdout.write(self.style.SUCCESS(
                f'\n{n} dette(s) supprimee(s) (annee {annee_str}) en transaction atomique.'
            ))
