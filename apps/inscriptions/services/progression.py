"""
Service de progression N → N+1.

Remplace l'ancien ProgressionService qui créait directement les InscriptionAdministrative.
Désormais en deux temps :
  1. generer_progressions(pv)   → crée la table Progression (buffer)
  2. ReinscriptionService        → crée les inscriptions effectives à la rentrée

Avantage : l'admin peut modifier filiere_cible / niveau_cible entre les deux étapes.

Couvre 5 décisions :
  - progression : passage au niveau supérieur (N+1 dans la même filière)
  - redoublement : reste au même niveau, consomme le droit Art. 22/28
  - annee_blanche : reste au même niveau, ne consomme pas le droit (Art. 23/29)
  - exclusion : sortie définitive du cycle
  - diplomation : fin de cycle (auto-détectée si niveau_source == filiere.niveau_fin
                  ET decision PV ∈ {passage_droit, passage_cond}). Couvre Licence Pro
                  (Art. 25 Arrêté 562) et Ingénieur (Art. 30 Décret 2018-070).
"""
import logging

from django.db import transaction

from apps.evaluations.models import PVDeliberation

logger = logging.getLogger('siga')


class ProgressionService:
    """
    Génère les lignes Progression à partir d'un PV annuel clos.
    Ne crée aucune InscriptionAdministrative directement.
    """

    _DECISION_MAP = {
        'passage_droit': 'progression',
        'passage_cond':  'progression',
        'redoublement':  'redoublement',
        'annee_blanche': 'annee_blanche',
        'exclusion':     'exclusion',
    }

    def __init__(self, pv: PVDeliberation):
        if not pv.est_clos:
            raise ValueError('Le PV doit être clos avant de générer les progressions.')
        if pv.type_pv != 'annuel':
            raise ValueError('Seuls les PV annuels génèrent des progressions.')
        self.pv = pv

    @transaction.atomic
    def generer_progressions(self) -> dict:
        """
        Crée ou met à jour une Progression par LigneDeliberation du PV.
        Idempotent : update_or_create sur (etudiant, annee_cible),
        SAUF si une Progression `executee` existe déjà pour cet étudiant/année
        (refus pour ne pas écraser l'historique).

        Retourne {'progression': n, 'redoublement': n, 'annee_blanche': n,
                  'exclusion': n, 'diplomation': n}.
        """
        from apps.inscriptions.models import Progression

        annee_cible = self._annee_suivante()
        # Bug #5 — refuser si l'année cible est clôturée
        if annee_cible.est_cloturee:
            raise ValueError(
                f"L'année cible {annee_cible.annee} est clôturée. "
                f"Impossible d'y générer des progressions."
            )

        stats = {
            'progression':   0,
            'redoublement':  0,
            'annee_blanche': 0,
            'exclusion':     0,
            'diplomation':   0,
        }

        lignes = self.pv.lignes.select_related(
            'inscription_admin__etudiant',
            'inscription_admin__filiere',
            'inscription_admin__annee_univ',
        )

        for ligne in lignes:
            decision_brute = self._mapper_decision(ligne.decision_annuelle)
            filiere_source = ligne.inscription_admin.filiere
            niveau_source  = ligne.inscription_admin.niveau

            # Auto-détection diplomation : fin de cycle + décision validante
            decision = self._resolve_decision_with_diplomation(
                decision_brute, niveau_source, filiere_source,
            )

            niveau_cible  = self._niveau_cible(decision, niveau_source, filiere_source)
            filiere_cible = self._filiere_cible(decision, niveau_cible, filiere_source)

            # Bug #3 — refuser l'écrasement d'une Progression déjà exécutée
            existante = Progression.objects.filter(
                etudiant=ligne.inscription_admin.etudiant,
                annee_cible=annee_cible,
            ).first()
            if existante and existante.statut == 'executee':
                raise ValueError(
                    f"Étudiant {ligne.inscription_admin.etudiant.matricule} a déjà "
                    f"une progression exécutée pour {annee_cible.annee} "
                    f"(InscriptionAdministrative #{existante.inscription_admin_creee_id}). "
                    f"Re-génération interdite — annulez d'abord la réinscription."
                )

            Progression.objects.update_or_create(
                etudiant=ligne.inscription_admin.etudiant,
                annee_cible=annee_cible,
                defaults={
                    'ligne_deliberation':          ligne,
                    'matricule':                   ligne.inscription_admin.etudiant.matricule,
                    'annee_source':                ligne.inscription_admin.annee_univ,
                    'filiere_source':              filiere_source,
                    'niveau_source':               niveau_source,
                    'filiere_cible':               filiere_cible,
                    'niveau_cible':                niveau_cible,
                    'institution':                 ligne.inscription_admin.institution,
                    'decision':                    decision,
                    'consomme_droit_redoublement': (decision == 'redoublement'),
                    'statut':                      'en_attente',
                },
            )
            stats[decision] += 1

        return stats

    @classmethod
    def _mapper_decision(cls, decision_annuelle: str) -> str:
        return cls._DECISION_MAP.get(decision_annuelle, 'redoublement')

    @staticmethod
    def _a_filles_continuation(filiere_source):
        """
        True si la filière a des filles qui PROLONGENT le même cycle de diplôme
        (même type_diplome) — cas tronc commun (ex. LPSTAT L1 → SDID/SEA L2-L3).
        Dans ce cas son `niveau_fin` ne marque PAS la fin du cursus : le diplôme
        se joue chez la fille. Le filtre type_diplome évite de confondre avec des
        filles d'un AUTRE cycle (ex. Licence → Master) qui, elles, démarrent un
        nouveau diplôme et ne prolongent pas le cursus de la mère.
        """
        return filiere_source.filieres_filles.filter(
            type_diplome=filiere_source.type_diplome,
        ).exists()

    @staticmethod
    def _resolve_decision_with_diplomation(decision_mappee, niveau_source, filiere_source):
        """
        Bascule en 'diplomation' si on est en fin de cycle :
          - decision PV mappée = 'progression' (passage_droit ou passage_cond)
          - niveau_source >= filiere_source.niveau_fin = palier de diplôme. Cet
            entier encode déjà le bon palier selon le diplôme : L3=3 pour LP,
            E3=3 pour ING, M2=2 pour Master, D3=3 pour Doctorat.

        Couvre Licence Pro (Art. 25 Arrêté 562) et Ingénieur (Art. 30 Décret 2018-070) :
        un étudiant qui valide son dernier niveau N'EST PAS progressé en niveau N+1
        fictif, il est diplômé.

        EXCEPTION tronc commun : une filière dont le cursus se PROLONGE chez des
        filles du même diplôme (ex. LPSTAT L1 → SDID/SEA L2-L3) n'est PAS un palier
        de diplôme — son niveau_fin ne couvre qu'une partie du cursus. Le diplôme se
        joue chez la fille (L3) ; ici on ORIENTE → on reste en 'progression'.
        """
        if decision_mappee != 'progression':
            return decision_mappee
        if ProgressionService._a_filles_continuation(filiere_source):
            return 'progression'
        if niveau_source >= filiere_source.niveau_fin:
            return 'diplomation'
        return 'progression'

    @staticmethod
    def _niveau_cible(decision, niveau_source, filiere_source=None):
        """
        Retourne le niveau cible pour la nouvelle inscription N+1.

        - progression  → niveau_source + 1
        - redoublement → niveau_source (même niveau)
        - annee_blanche → niveau_source (même niveau)
        - exclusion    → None
        - diplomation  → None (pas d'inscription N+1)

        Garde-fou Bug #4 : si decision='progression' et niveau_source+1 dépasse
        filiere.niveau_fin, c'est une incohérence (devrait être attrapé par
        _resolve_decision_with_diplomation en amont). On lève alors ValueError —
        SAUF pour un tronc commun (filles du même diplôme) : sa cible est une
        fille (niveau_debut = niveau_source+1), donc dépasser SON niveau_fin est
        légitime (ex. LPSTAT L1 → niveau_cible 2 chez SDID/SEA).
        """
        if decision == 'progression':
            niveau_cible = niveau_source + 1
            if (filiere_source and niveau_cible > filiere_source.niveau_fin
                    and not ProgressionService._a_filles_continuation(filiere_source)):
                raise ValueError(
                    f"niveau_cible {niveau_cible} dépasse niveau_fin "
                    f"{filiere_source.niveau_fin} de la filière {filiere_source.code}. "
                    f"Cas de fin de cycle non détecté — vérifier "
                    f"_resolve_decision_with_diplomation."
                )
            return niveau_cible
        if decision == 'exclusion':   return None
        if decision == 'diplomation': return None
        return niveau_source  # redoublement, annee_blanche

    @staticmethod
    def _filiere_cible(decision, niveau_cible, filiere_source):
        """
        Filière d'inscription N+1.
          - diplomation / exclusion         → None (pas d'inscription N+1)
          - progression d'un tronc commun   → None « à orienter » : niveau_cible
            dépasse le niveau_fin du parent (LPSTAT), donc la cible sera une fille
            (SDID/SEA) que l'administration choisit AVANT la réinscription.
          - sinon (même filière couvre le niveau cible) → on reste dans la filière.
        """
        if decision not in ('progression', 'redoublement', 'annee_blanche'):
            return None  # diplomation, exclusion
        if (decision == 'progression'
                and niveau_cible is not None
                and niveau_cible > filiere_source.niveau_fin):
            return None  # tronc commun → à orienter vers une fille
        return filiere_source

    def _annee_suivante(self):
        """
        Retourne l'objet Year correspondant à N+1.

        Stratégie :
          1. Récupère l'année courante depuis pv.annee_univ (ou pv.session.annee_univ).
          2. Calcule le label N+1 par incrément +1 sur chaque partie de "YYYY-YYYY".
          3. get_or_create de la Year cible (auto-création si absente).

        Bug #2 — refuse explicitement avec ValueError si :
          - le PV n'a pas d'annee_univ ni de session avec annee_univ
          - le format n'est pas "YYYY-YYYY"

        Le fallback historique sur "année la plus récente en base" est supprimé :
        il créait des progressions vers une année arbitraire si l'année source
        était indéterminée.
        """
        from apps.parametres.models import Year

        annee_courante = (
            self.pv.annee_univ
            or (self.pv.session.annee_univ if self.pv.session else None)
        )

        if not annee_courante or '-' not in (annee_courante.annee or ''):
            raise ValueError(
                "Le PV n'a pas d'année universitaire valide associée. "
                "Impossible de déterminer l'année cible de progression."
            )

        parts = annee_courante.annee.split('-')
        try:
            label_suivant = f'{int(parts[0]) + 1}-{int(parts[1]) + 1}'
        except (ValueError, IndexError):
            raise ValueError(
                f"Format d'année invalide : '{annee_courante.annee}' "
                f"(attendu : YYYY-YYYY)."
            )

        annee, created = Year.objects.get_or_create(
            annee=label_suivant,
            defaults={'est_active': False, 'est_cloturee': False},
        )
        if created:
            logger.info(
                "Year auto-créée pour progression : %s (depuis PV #%s sur %s)",
                label_suivant, self.pv.pk, annee_courante.annee,
            )
        return annee


class ModificationProgressionService:
    """
    Permet à l'administration de modifier la filière et/ou le niveau cible
    d'une Progression avant son exécution.

    La modification est refusée si la progression est déjà exécutée ou annulée.
    La compatibilité filière est vérifiée via filiere_parent si défini.
    """

    @staticmethod
    @transaction.atomic
    def modifier(
        progression_id: int,
        nouvelle_filiere_id,
        nouveau_niveau,
        motif: str,
        user,
    ):
        from apps.inscriptions.models import Progression
        from apps.scolarite.models import Filiere

        if not motif or not motif.strip():
            raise ValueError('Le motif de modification est obligatoire.')

        prog = Progression.objects.select_for_update().get(pk=progression_id)

        if prog.statut in ('executee', 'annulee'):
            raise ValueError(
                f'La progression #{prog.pk} ne peut plus être modifiée '
                f'(statut actuel : {prog.get_statut_display()}).'
            )

        if nouvelle_filiere_id:
            nouvelle_filiere = Filiere.objects.get(pk=nouvelle_filiere_id)
            ModificationProgressionService._verifier_compatibilite(
                prog.filiere_source, nouvelle_filiere,
            )
            # Vérifier que la nouvelle filière couvre bien le niveau cible
            niveau_a_verifier = nouveau_niveau if nouveau_niveau is not None else prog.niveau_cible
            ModificationProgressionService._verifier_niveau(
                nouvelle_filiere, niveau_a_verifier,
            )
            prog.filiere_cible = nouvelle_filiere

        if nouveau_niveau is not None:
            if nouveau_niveau < 1:
                raise ValueError('Le niveau cible doit être ≥ 1.')
            prog.niveau_cible = nouveau_niveau

        prog.motif_modification = motif.strip()
        prog.modifiee_par       = user
        prog.statut             = 'modifiee'
        prog.save()
        return prog

    @staticmethod
    def _verifier_compatibilite(source, cible):
        """
        Refuse le changement si les filières n'ont pas de filiere_parent commune.
        Compatible si :
          - même filière (no-op)
          - même filiere_parent
          - l'une est parente de l'autre
          - aucune filiere_parent définie sur aucune des deux (pas de restriction)
        """
        if source.pk == cible.pk:
            return
        if source.filiere_parent_id is None and cible.filiere_parent_id is None:
            return  # Pas de hiérarchie configurée → pas de restriction
        if (source.filiere_parent_id
                and source.filiere_parent_id == cible.filiere_parent_id):
            return
        if source.filiere_parent_id == cible.pk:
            return
        if cible.filiere_parent_id == source.pk:
            return
        raise ValueError(
            f"Filière cible «{cible.code}» incompatible avec «{source.code}» : "
            f"pas de filiere_parent commune."
        )

    @staticmethod
    def _verifier_niveau(filiere, niveau):
        """
        Refuse si le niveau cible n'est pas couvert par la filière.
        (ex : envoyer un L1 vers SDID qui démarre en L2 → refus)
        """
        if niveau is None:
            return  # exclusion ou cas spécial — pas de niveau à valider
        if niveau < filiere.niveau_debut or niveau > filiere.niveau_fin:
            raise ValueError(
                f"La filière «{filiere.code}» couvre les niveaux "
                f"L{filiere.niveau_debut} → L{filiere.niveau_fin} ; "
                f"le niveau cible L{niveau} n'est pas couvert."
            )
