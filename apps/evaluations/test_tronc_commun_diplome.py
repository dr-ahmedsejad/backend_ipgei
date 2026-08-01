"""
Test de non-régression — garde « tronc commun » sur le blocage diplôme annuel
(_bloquer_admis_non_eligible_diplome) + conditions d'obtention du diplôme
(180 crédits — Art. 25 Arrêté 562 / Art. 8 Décret 2018-070 ; note finale >= 12).

Contexte (bug PV 23, 2026-06-19) : une filière AYANT des filières filles est un
tronc commun (ex. LPSTAT L1 → SDID/SEA en L2). Son `niveau_fin` n'est PAS une
année de diplôme : les étudiants poursuivent dans les filières filles. Le blocage
Art. 25 (moyenne S6/stage >= 12) ne doit donc PAS s'y appliquer, sinon tous les
admis sont rebasculés en redoublement.

Contrôle : une filière DIPLÔMANTE (sans filles) à son niveau_fin doit, elle,
continuer d'appliquer le blocage quand l'étudiant n'a pas de note finale (S6).

Exécution (runner = pytest) :
  .venv/Scripts/python.exe -m pytest apps/evaluations/test_tronc_commun_diplome.py
"""
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from apps.parametres.models import Year, Institution
from apps.departement.models import Departement
from apps.scolarite.models import Filiere
from apps.absence.models import Etudiant
from apps.inscriptions.models import InscriptionAdministrative
from apps.evaluations.models import PVDeliberation, LigneDeliberation
from apps.evaluations.services.deliberation_annuelle import (
    get_deliberation_annuelle_service, DeliberationAnnuelleService,
)


class GardeTroncCommunDiplomeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(
            acronyme='TST', nom='Institut Test', est_principale=True,
        )
        cls.year = Year.objects.create(annee='2024-2025')
        cls.dept = Departement.objects.create(nom='Dept Test', institution=cls.inst)

    def _filiere(self, code, niveau_fin, parent=None):
        return Filiere.objects.create(
            code=code, intitule_fr=code, type_diplome='LP',
            institution=self.inst, niveau_debut=1, niveau_fin=niveau_fin,
            filiere_parent=parent,
        )

    def _pv_admis(self, filiere, niveau):
        """PV annuel + 1 étudiant ADMIS (passage_droit, 60 crédits) sans note S6."""
        etu = Etudiant.objects.create(
            matricule=f'ETU-{filiere.code}', nom='X',
            departement=self.dept, genre='M', filiere=filiere,
        )
        ia = InscriptionAdministrative.objects.create(
            etudiant=etu, annee_univ=self.year, filiere=filiere,
            institution=self.inst, niveau=niveau,
            numero_inscription=f'INSC-{filiere.code}',
        )
        pv = PVDeliberation.objects.create(
            filiere=filiere, institution=self.inst, niveau=niveau,
            type_pv='annuel', annee_univ=self.year,
        )
        ligne = LigneDeliberation.objects.create(
            pv=pv, inscription_admin=ia, decision='admis',
            decision_annuelle='passage_droit',
            moyenne_annuelle=Decimal('13.00'), credits_annuels=60,
        )
        return pv, ligne

    def test_tronc_commun_pas_de_blocage(self):
        """Filière AVEC filles → blocage diplôme ignoré → l'admis reste admis."""
        parent = self._filiere('LPTRONC', niveau_fin=1)
        self._filiere('SDIDTEST', niveau_fin=3, parent=parent)  # filière fille
        pv, ligne = self._pv_admis(parent, niveau=1)

        svc = get_deliberation_annuelle_service(pv)
        svc._bloquer_admis_non_eligible_diplome(1)

        ligne.refresh_from_db()
        self.assertEqual(
            ligne.decision, 'admis',
            "Tronc commun (a des filles) : le blocage diplôme ne doit PAS "
            "rebasculer l'admis en redoublement.",
        )
        self.assertEqual(ligne.decision_annuelle, 'passage_droit')

    # ── Conditions d'obtention (mesurées via le moteur du RELEVÉ, pas le stocké) ──
    #
    # Les crédits (_credits_capitalises_diplome) et la note finale
    # (_moyenne_semestre_final) sont calculés par calculer_resultat_semestre_
    # consolide (max SN/SR + compensation + report). On mocke ces DEUX helpers
    # pour tester la LOGIQUE DE DÉCISION du blocage de façon déterministe, sans
    # monter la chaîne de notes que ce moteur exige (le décompte stocké
    # ResultatSemestre est justement IGNORÉ par le moteur du relevé).

    @staticmethod
    def _patch_relevé(credits, note_s6):
        return (
            mock.patch.object(
                DeliberationAnnuelleService, '_credits_capitalises_diplome',
                return_value=credits),
            mock.patch.object(
                DeliberationAnnuelleService, '_moyenne_semestre_final',
                return_value=(None if note_s6 is None else Decimal(str(note_s6)))),
        )

    def test_filiere_diplomante_note_finale_insuffisante(self):
        """Filière diplômante L3 (sans filles), 180 crédits mais S6 < 12 → blocage Art. 25/16."""
        solo = self._filiere('LPSOLO', niveau_fin=3)   # pas de filles
        pv, ligne = self._pv_admis(solo, niveau=3)      # L3 = niveau_fin
        p_cred, p_note = self._patch_relevé(180, '10.00')

        svc = get_deliberation_annuelle_service(pv)
        with p_cred, p_note:
            svc._bloquer_admis_non_eligible_diplome(1)

        ligne.refresh_from_db()
        self.assertEqual(
            ligne.decision, 'ajourned',
            "Filière diplômante (sans filles) avec S6 < 12 : le blocage Art. 25 "
            "doit rebasculer l'admis (le garde tronc commun ne doit pas la couvrir).",
        )
        self.assertEqual(ligne.decision_annuelle, 'redoublement')

    def test_diplome_bloque_si_credits_insuffisants(self):
        """
        Admis L3, note S6 >= 12 mais TOTAL crédits < 180 (dette S3) → blocage
        (reproduit le cas 23634 : 176/180, note OK). L'ancien blocage — qui ne
        testait QUE la note finale — laissait passer.
        """
        solo = self._filiere('LPCRED', niveau_fin=3)
        pv, ligne = self._pv_admis(solo, niveau=3)
        p_cred, p_note = self._patch_relevé(176, '13.00')   # 176/180, S6 OK

        svc = get_deliberation_annuelle_service(pv)
        with p_cred, p_note:
            svc._bloquer_admis_non_eligible_diplome(1)

        ligne.refresh_from_db()
        self.assertEqual(
            ligne.decision, 'ajourned',
            'Diplôme < 180 crédits (176) doit être bloqué même si la note S6 >= 12.',
        )
        self.assertEqual(ligne.decision_annuelle, 'redoublement')
        self.assertIn('crédits', (ligne.observations or '').lower())

    def test_diplome_accorde_si_180_et_note_ok(self):
        """Admis L3 avec 180 crédits capitalisés (relevé) + S6 >= 12 → reste diplômé."""
        solo = self._filiere('LP180', niveau_fin=3)
        pv, ligne = self._pv_admis(solo, niveau=3)
        p_cred, p_note = self._patch_relevé(180, '13.00')   # 180/180, S6 OK

        svc = get_deliberation_annuelle_service(pv)
        with p_cred, p_note:
            svc._bloquer_admis_non_eligible_diplome(1)

        ligne.refresh_from_db()
        self.assertEqual(
            ligne.decision, 'admis',
            '180 crédits + note S6 >= 12 : le diplôme doit être accordé.',
        )
        self.assertEqual(ligne.decision_annuelle, 'passage_droit')
