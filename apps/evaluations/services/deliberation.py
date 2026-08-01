"""
Service de délibération — peuplement et calcul des décisions d'un PV.

Utilisation :
    svc = DeliberationService(pv)
    svc.peupler_lignes()        # crée LigneDeliberation pour chaque étudiant
    svc.calculer_decisions()    # applique les règles Art. 562
"""
from decimal import Decimal

from django.db import transaction

from apps.evaluations.models import (
    PVDeliberation, LigneDeliberation, ParametreJury,
)
from apps.evaluations.services.calcul_notes import NoteCalculService


class DeliberationService:
    def __init__(self, pv: PVDeliberation):
        self.pv = pv

    # ── Peuplement ────────────────────────────────────────────────────────────

    @transaction.atomic
    def peupler_lignes(self) -> int:
        """
        Pour chaque étudiant inscrit (InscriptionAdministrative) dans la
        filière/année/niveau du PV, crée une LigneDeliberation avec sa
        moyenne annuelle calculée.

        Idempotent : utilise get_or_create sur (pv, inscription_admin).
        Retourne le nombre de lignes créées ou mises à jour.
        """
        from apps.inscriptions.models import InscriptionAdministrative

        inscriptions = InscriptionAdministrative.objects.filter(
            filiere=self.pv.filiere,
            annee_univ=self.pv.session.annee_univ,
            niveau=self.pv.niveau,
        ).select_related('etudiant', 'annee_univ')

        count = 0
        for insc_admin in inscriptions:
            moyenne, credits, _ = NoteCalculService.calculer_moyenne_annuelle(
                etudiant=insc_admin.etudiant,
                annee_univ=insc_admin.annee_univ,
                niveau=insc_admin.niveau,
            )

            ligne, created = LigneDeliberation.objects.get_or_create(
                pv=self.pv,
                inscription_admin=insc_admin,
                defaults={
                    'decision':          'ajourned',
                    'moyenne_annuelle':  moyenne,
                    'credits_annuels':   credits,
                },
            )
            if not created:
                # Mise à jour des valeurs calculées sans écraser la décision jury
                ligne.moyenne_annuelle = moyenne
                ligne.credits_annuels  = credits
                ligne.save(update_fields=['moyenne_annuelle', 'credits_annuels'])

            count += 1

        return count

    # ── Calcul des décisions ──────────────────────────────────────────────────

    @transaction.atomic
    def calculer_decisions(self) -> int:
        """
        Applique les règles de délibération (Art. 562) sur chaque
        LigneDeliberation du PV.

        Règles (seuils depuis ParametreJury ou valeurs par défaut) :
          admis    : moyenne >= seuil_validation ET credits >= crédits_niveau
          rachat   : moyenne >= seuil_compensation ET < seuil_validation
          ajourné  : moyenne >= seuil_eliminatoire ET < seuil_compensation
          exclus   : moyenne < seuil_eliminatoire

        Met à jour LigneDeliberation.decision.
        Retourne le nombre de lignes traitées.
        """
        # Récupère les paramètres du jury (ou valeurs par défaut)
        try:
            params = self.pv.parametre_jury
            seuil_validation  = params.seuil_validation_semestre
            seuil_compensation = params.seuil_compensation
            seuil_eliminatoire = params.seuil_eliminatoire
        except ParametreJury.DoesNotExist:
            seuil_validation   = Decimal('10')
            seuil_compensation = Decimal('8')
            seuil_eliminatoire = Decimal('6')

        # Crédits requis pour le niveau (somme des crédits des semestres du niveau)
        credits_requis = self._credits_requis_niveau()

        lignes = self.pv.lignes.all()
        count = 0

        for ligne in lignes:
            moy = ligne.moyenne_annuelle or Decimal('0')
            credits = ligne.credits_annuels or 0

            if moy >= seuil_validation and credits >= credits_requis:
                decision = 'admis'
            elif moy >= seuil_compensation:
                decision = 'rachat'
            elif moy >= seuil_eliminatoire:
                decision = 'ajourned'
            else:
                decision = 'exclus'

            if ligne.decision != decision:
                ligne.decision = decision
                ligne.save(update_fields=['decision'])

            count += 1

        return count

    # ── Utilitaire interne ────────────────────────────────────────────────────

    def _credits_requis_niveau(self) -> int:
        """
        Retourne le total des crédits des semestres du niveau du PV.
        Fallback : 60 (standard LMD par niveau).

        Depuis la Section 1 du plan institution_V1, Semestre est générique
        (sans filiere ni annee_univ) — un seul Semestre par (niveau × parité).
        """
        from apps.parametres.models import Semestre

        semestres = Semestre.objects.filter(
            niveau_semestre__niveau__icontains=f'L{self.pv.niveau}',
        )
        total = sum(s.credits for s in semestres)
        return total if total > 0 else 60
