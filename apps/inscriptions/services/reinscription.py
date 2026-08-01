"""
Service de réinscription effective N+1.

Lit la table Progression (statuts en_attente et modifiee) et crée les
InscriptionAdministrative + InscriptionPedagogique + InscriptionElement correspondantes.

À appeler à la rentrée, après que l'admin a éventuellement modifié des filiere_cible
via ModificationProgressionService.
"""
import uuid

from django.db import transaction


class ReinscriptionService:

    @staticmethod
    @transaction.atomic
    def executer(annee_cible) -> dict:
        """
        Exécute toutes les Progression en attente pour une année cible.
        Idempotent : get_or_create sur InscriptionAdministrative(etudiant, annee_univ).

        Retourne {'progression': n, 'redoublement': n, 'annee_blanche': n,
                  'exclusion': n, 'diplomation': n}.

        Pour les diplômés (decision='diplomation'), AUCUNE InscriptionAdministrative
        n'est créée — l'étudiant est simplement flaggé `statut='diplome'`. Couvre
        Licence Pro (Art. 25 Arrêté 562) et Ingénieur (Art. 30 Décret 2018-070).
        """
        from apps.inscriptions.models import (
            Progression, InscriptionAdministrative,
        )
        from apps.inscriptions.utils import creer_inscriptions_pedagogiques

        progressions = (
            Progression.objects
            .filter(annee_cible=annee_cible, statut__in=('en_attente', 'modifiee'))
            .select_related('etudiant', 'filiere_cible', 'annee_source')
        )

        stats = {
            'progression':   0,
            'redoublement':  0,
            'annee_blanche': 0,
            'exclusion':     0,
            'diplomation':   0,
            'a_orienter':    0,  # tronc commun sans filière cible choisie (SDID/SEA)
        }

        for prog in progressions:
            if prog.decision == 'diplomation':
                # Fin de cycle — pas d'inscription N+1, on flague l'étudiant diplômé
                prog.etudiant.statut = 'diplome'
                prog.etudiant.save(update_fields=['statut'])
                prog.statut = 'executee'
                prog.save(update_fields=['statut', 'date_modification'])
                stats['diplomation'] += 1
                continue

            if prog.decision == 'exclusion':
                prog.etudiant.statut = 'exclu'
                prog.etudiant.save(update_fields=['statut'])
                prog.statut = 'executee'
                prog.save(update_fields=['statut', 'date_modification'])
                stats['exclusion'] += 1
                continue

            if prog.filiere_cible is None:
                # Tronc commun pas encore orienté vers une fille (SDID/SEA à
                # choisir par l'administration) → on SAUTE pour ne pas créer une
                # InscriptionAdministrative sans filière. La progression reste
                # 'en_attente'/'modifiee' jusqu'à ce que filiere_cible soit définie.
                stats['a_orienter'] += 1
                continue

            num_insc = f'INS-{annee_cible.annee}-{uuid.uuid4().hex[:6].upper()}'
            insc, _ = InscriptionAdministrative.objects.get_or_create(
                etudiant=prog.etudiant,
                annee_univ=annee_cible,
                defaults={
                    'filiere':            prog.filiere_cible,
                    'niveau':             prog.niveau_cible,
                    'institution':        prog.institution,
                    'numero_inscription': num_insc,
                    'statut':             'en_cours',
                },
            )

            if prog.decision == 'progression':
                # Passage de droit OU passage conditionnel : creer les EMs standards
                # de la nouvelle annee/niveau (S3/S4 pour L2, etc.)
                creer_inscriptions_pedagogiques(insc, user=prog.modifiee_par)
                # Puis reporter les EMs non valides de l'annee source comme dettes
                # → cree des IP supplementaires (S1/S2 si non valides) avec leurs
                #   IE marques est_dette=True. Utile pour passage conditionnel.
                ReinscriptionService._reinscrire_dettes(insc, prog)
            else:
                # redoublement ou annee_blanche : réinscrire seulement les dettes
                ReinscriptionService._reinscrire_dettes(insc, prog)

            if prog.etudiant.statut != 'actif':
                prog.etudiant.statut = 'actif'
                prog.etudiant.save(update_fields=['statut'])

            prog.inscription_admin_creee = insc
            prog.statut = 'executee'
            prog.save(update_fields=['inscription_admin_creee', 'statut', 'date_modification'])
            stats[prog.decision] += 1

        return stats

    @staticmethod
    def _reinscrire_dettes(insc_nouvelle, prog):
        """
        Pour un redoublant ou une année blanche : réinscrire uniquement les
        semestres non validés et leurs EM non validés (dettes).
        """
        from apps.inscriptions.models import (
            InscriptionAdministrative, InscriptionPedagogique, InscriptionElement,
        )
        from apps.evaluations.models import ResultatSemestre

        try:
            insc_old = InscriptionAdministrative.objects.get(
                etudiant=prog.etudiant, annee_univ=prog.annee_source,
            )
        except InscriptionAdministrative.DoesNotExist:
            return

        for ip_old in InscriptionPedagogique.objects.filter(inscription_admin=insc_old):
            valide = ResultatSemestre.objects.filter(
                inscription_ped=ip_old, est_admis=True,
            ).exists()
            if valide:
                continue

            # est_redoublant UNIQUEMENT pour un vrai redoublement / année blanche.
            # En cas de 'progression' (passage conditionnel), l'étudiant AVANCE
            # de niveau en traînant des dettes : il n'est PAS redoublant.
            # est_dette reste True dans tous les cas (ce sont bien des dettes).
            est_redoublant = prog.decision in ('redoublement', 'annee_blanche')
            ip_new, _ = InscriptionPedagogique.objects.get_or_create(
                inscription_admin=insc_nouvelle,
                semestre=ip_old.semestre,
                defaults={'est_redoublant': est_redoublant, 'est_dette': True},
            )

            for ie_old in ip_old.inscriptions_elements.select_related('element', 'em'):
                # Triple check pour ne creer une dette QUE si l'element est
                # vraiment a rattraper :
                #  1. est_valide=True sur n'importe quel ResultatElement -> deja valide
                #  2. code_statut in (V, VCI, VCS) -> validation directe ou par compensation
                # Sans (2), un element devenu VCI APRES la deliberation du module
                # (qui peut tourner apres la Progression) reste a tort en dette
                # car a l'instant T `_reinscrire_dettes` voyait encore est_valide=False.
                try:
                    element_valide = ie_old.resultats.filter(
                        est_valide=True,
                    ).exists() or ie_old.resultats.filter(
                        code_statut__in=['V', 'VCI', 'VCS'],
                    ).exists()
                except Exception:
                    element_valide = False

                # Garde-fou CONSOLIDÉ : un élément < 10 acquis par compensation/
                # capitalisation cross-année (module validé via un autre élément
                # validé une année antérieure) reste 'NV' au ResultatElement brut.
                # On le reconnaît via le relevé consolidé pour ne PAS créer de dette
                # fantôme. Additif : ne peut que rendre un EM « valide » (= retirer
                # une dette), jamais l'inverse.
                if not element_valide:
                    from apps.evaluations.services.note_lecture import em_acquis_consolide
                    try:
                        element_valide = em_acquis_consolide(
                            prog.etudiant, ie_old.em, prog.annee_source,
                        )
                    except Exception:
                        pass

                if not element_valide:
                    # Cle d'unicite : em (jamais NULL). Utiliser element (souvent NULL)
                    # ferait que tous les EMs partagent la meme cle (inscription_ped, NULL)
                    # → seul le 1er EM serait cree, les autres ignores en silence.
                    InscriptionElement.objects.get_or_create(
                        inscription_ped=ip_new,
                        em=ie_old.em,
                        defaults={
                            'element':     ie_old.element,
                            'est_dette':   True,
                            'annee_dette': prog.annee_source,
                        },
                    )
