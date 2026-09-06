"""
Test — décision de jury, seuil figé, verrous de clôture.

Ce que le test fige :
  1. `decision_automatique` : 1re année admis / réorienté ; 2e année autorisé
     à concourir / autorisé au rattrapage — jamais « redoublant » d'emblée ;
  2. le relevé juge au seuil FIGÉ par le jury validé, pas au paramétrage du
     jour (PV : admis à 7,50 ; relevé : « non validé » à 9,00 — corrigé) ;
  3. `controler_recalcul` refuse une année close et un semestre clos —
     chemins qui échappaient à `controler_saisie` ;
  4. `rattacher_inscrits` n'injecte plus de ligne de note dans un semestre
     clos (une matière créée après jury faisait chuter la moyenne recalculée) ;
  5. `_poser_rangs` — la version en deux lectures rend les mêmes rangs.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.absence.models import Etudiant
from apps.ipgei import pv, services
from apps.ipgei.models import (
    Classe, Deliberation, InscriptionIPGEI, LigneDeliberation, Matiere,
    NiveauIPGEI, Note, ParametresIPGEI, SemestreIPGEI,
)
from apps.parametres.models import Institution, Niveau, Year


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle_mpsi = Niveau.objects.create(niveau='MPSI')
        cls.socle_mp   = Niveau.objects.create(niveau='MP')
        cls.ref_mpsi = NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI',
                                                  libelle='MPSI', rang=1)
        cls.ref_mp   = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                                  libelle='MP', rang=2)
        cls.annee = '2025-2026'
        cls.s1 = SemestreIPGEI.objects.create(institution=cls.inst, code='S1',
                                              annee_universitaire=cls.annee,
                                              date_debut=date(2025, 9, 1))
        cls.s2 = SemestreIPGEI.objects.create(institution=cls.inst, code='S2',
                                              annee_universitaire=cls.annee,
                                              date_debut=date(2026, 1, 15))
        cls.math1 = Matiere.objects.create(
            institution=cls.inst, code='MATH1', intitule='MATH1', code_semestre='S1',
            niveau_ref=cls.ref_mpsi, coefficient=Decimal(1))
        cls.math2 = Matiere.objects.create(
            institution=cls.inst, code='MATH2', intitule='MATH2', code_semestre='S2',
            niveau_ref=cls.ref_mpsi, coefficient=Decimal(1))
        cls.mpsi_a = Classe.objects.create(nom='MPSI A', institution=cls.inst,
                                           annee_universitaire=cls.annee,
                                           niveau=cls.socle_mpsi, groupe='A')
        cls.mpsi_b = Classe.objects.create(nom='MPSI B', institution=cls.inst,
                                           annee_universitaire=cls.annee,
                                           niveau=cls.socle_mpsi, groupe='B')

    def _inscrit(self, matricule, classe=None):
        etudiant = Etudiant.objects.create(matricule=matricule, nom=f'Etudiant {matricule}')
        return InscriptionIPGEI.objects.create(etudiant=etudiant, classe=classe or self.mpsi_a,
                                               annee_universitaire=self.annee)

    def _note(self, inscription, matiere, semestre, valeur):
        Note.objects.update_or_create(
            inscription=inscription, matiere=matiere, semestre=semestre,
            defaults={'note_retenue': Decimal(valeur), 'coefficient': matiere.coefficient})


class DecisionAutomatiqueTest(_Base):
    def test_premiere_annee_admis_ou_reoriente(self):
        self.assertEqual(services.decision_automatique('MPSI', Decimal('10'), Decimal('10'), 0, 1, self.inst),
                         LigneDeliberation.DECISION_ADMIS)
        self.assertEqual(services.decision_automatique('MPSI', Decimal('9.99'), Decimal('10'), 0, 1, self.inst),
                         LigneDeliberation.DECISION_REORIENTE)

    def test_deuxieme_annee_concours_ou_rattrapage_jamais_redoublant_d_emblee(self):
        self.assertEqual(services.decision_automatique('MP', Decimal('12'), Decimal('9'), 0, 1, self.inst),
                         LigneDeliberation.DECISION_CNIM)
        self.assertEqual(services.decision_automatique('MP', Decimal('8'), Decimal('9'), 0, 1, self.inst),
                         LigneDeliberation.DECISION_RATTRAPAGE)
        self.assertEqual(services.decision_automatique('MP', Decimal('8'), Decimal('9'), 1, 1, self.inst),
                         LigneDeliberation.DECISION_RATTRAPAGE,
                         'même le droit épuisé, on ne va pas au concours sans le rattrapage')

    def test_sans_moyenne_aucune_proposition(self):
        self.assertEqual(services.decision_automatique('MPSI', None, Decimal('10'), 0, 1, self.inst), '')


class SeuilFigeTest(_Base):
    def _jury_valide(self, inscription, seuil, moyenne):
        delib = Deliberation.objects.create(
            institution=self.inst, niveau='MPSI', classe=inscription.classe,
            annee_universitaire=self.annee, portee=Deliberation.PORTEE_ANNUELLE,
            seuil_validation=Decimal(seuil), statut=Deliberation.STATUT_VALIDEE,
            date_validation=timezone.now())
        LigneDeliberation.objects.create(deliberation=delib, inscription=inscription,
                                         moyenne_generale=Decimal(moyenne),
                                         decision=LigneDeliberation.DECISION_ADMIS)
        return delib

    def test_sans_jury_le_seuil_est_celui_du_parametrage(self):
        params = ParametresIPGEI.pour(self.inst)
        params.seuil_validation = Decimal('9.00')
        params.save()
        i = self._inscrit('400001')
        self.assertEqual(services.seuil_applicable(i, self.annee), Decimal('9.00'))

    def test_le_seuil_du_jury_valide_prime_sur_le_parametrage_courant(self):
        params = ParametresIPGEI.pour(self.inst)
        params.seuil_validation = Decimal('9.00')      # le seuil a bougé DEPUIS le jury
        params.save()
        i = self._inscrit('400002')
        self._jury_valide(i, '7.50', '8.20')
        self.assertEqual(services.seuil_applicable(i, self.annee), Decimal('7.50'))

    def test_le_releve_annuel_dit_la_meme_chose_que_le_pv(self):
        params = ParametresIPGEI.pour(self.inst)
        params.seuil_validation = Decimal('9.00')
        params.save()
        i = self._inscrit('400003')
        self._note(i, self.math1, self.s1, '8.20')
        self._note(i, self.math2, self.s2, '8.20')
        self._jury_valide(i, '7.50', '8.20')

        releve = services.releve_annuel(i, self.annee)
        self.assertEqual(releve['moyenne'], Decimal('8.20'))
        self.assertEqual(releve['seuil'], Decimal('7.50'), 'le seuil du jury, pas 9,00')
        self.assertTrue(releve['valide'], 'admis au PV → validé sur le relevé')

    def test_un_jury_non_valide_ne_fait_pas_foi(self):
        params = ParametresIPGEI.pour(self.inst)
        params.seuil_validation = Decimal('9.00')
        params.save()
        i = self._inscrit('400004')
        delib = self._jury_valide(i, '7.50', '8.20')
        delib.statut = Deliberation.STATUT_CALCULEE
        delib.save(update_fields=['statut'])
        self.assertEqual(services.seuil_applicable(i, self.annee), Decimal('9.00'))


class VerrousDeClotureTest(_Base):
    def test_le_recalcul_est_refuse_sur_un_semestre_clos(self):
        self.s1.est_cloture = True
        self.s1.save(update_fields=['est_cloture'])
        with self.assertRaises(ValidationError):
            services.controler_recalcul(self.s1)

    def test_le_recalcul_est_refuse_sur_une_annee_close(self):
        Year.objects.create(annee=self.annee, date_debut=date(2025, 9, 1),
                            date_fin=date(2026, 7, 31), est_cloturee=True)
        with self.assertRaises(ValidationError):
            services.controler_recalcul(self.s2)

    def test_le_recalcul_passe_sur_un_semestre_ouvert(self):
        services.controler_recalcul(self.s2)          # ne lève pas

    def test_un_semestre_clos_ne_recoit_plus_de_ligne_de_note(self):
        i = self._inscrit('500001')
        Note.objects.filter(inscription=i, semestre=self.s1).delete()
        self.s1.est_cloture = True
        self.s1.save(update_fields=['est_cloture'])

        crees = services.rattacher_inscrits(self.s1)
        self.assertEqual(crees, 0)
        self.assertFalse(Note.objects.filter(inscription=i, semestre=self.s1).exists())

    def test_un_semestre_ouvert_recoit_ses_lignes(self):
        i = self._inscrit('500002')
        Note.objects.filter(inscription=i, semestre=self.s1).delete()
        services.rattacher_inscrits(self.s1)
        self.assertTrue(Note.objects.filter(inscription=i, semestre=self.s1,
                                            matiere=self.math1).exists())


class PoserRangsTest(_Base):
    def test_les_rangs_de_classe_et_de_promotion(self):
        # Trois inscrits sur deux classes : A porte le jury, B est calculée.
        a1 = self._inscrit('600001', self.mpsi_a)
        a2 = self._inscrit('600002', self.mpsi_a)
        b1 = self._inscrit('600003', self.mpsi_b)
        self._note(b1, self.math1, self.s1, 15)             # meilleur de la promotion

        delib = Deliberation.objects.create(
            institution=self.inst, niveau='MPSI', classe=self.mpsi_a,
            annee_universitaire=self.annee, portee=Deliberation.PORTEE_SEMESTRE,
            semestre=self.s1, seuil_validation=Decimal('10'))
        l1 = LigneDeliberation.objects.create(deliberation=delib, inscription=a1,
                                              moyenne_generale=Decimal('12'))
        l2 = LigneDeliberation.objects.create(deliberation=delib, inscription=a2,
                                              moyenne_generale=Decimal('10'))

        effectifs = pv._poser_rangs(delib, [l1, l2])
        self.assertEqual((l1.rang_classe, l1.rang_promotion), (1, 2))
        self.assertEqual((l2.rang_classe, l2.rang_promotion), (2, 3))
        self.assertEqual(effectifs, {'classes_du_niveau': 2, 'effectif_promotion': 3,
                                     'effectif_classe': 2})


class AuditVerrouillageTest(_Base):
    """
    Verrouiller les notes est une modification, et elle doit se lire.

    `notes_couvertes(deliberation).update(verrouillee=True)` est un
    `QuerySet.update()` : il n'émet aucun signal. La validation du jury était
    tracée, mais rien ne disait que N notes venaient de devenir non
    modifiables. Le jour où quelqu'un demande pourquoi une note refuse d'être
    corrigée, le journal doit pouvoir répondre.
    """

    def setUp(self):
        from apps.authentication.models import CustomUser
        self.jury = CustomUser.objects.create_user(
            username='jury', email='j@t.l', password='Jury!2612mdp', role='admin')

    def _delibere(self, portee):
        from core.models import AuditLog

        inscription = self._inscrit('750001')
        self._note(inscription, self.math1, self.s1, '12')
        delib = Deliberation.objects.create(
            institution=self.inst, niveau='MPSI', classe=self.mpsi_a,
            annee_universitaire=self.annee, portee=portee,
            semestre=self.s1 if portee == Deliberation.PORTEE_SEMESTRE else None,
            statut=Deliberation.STATUT_CALCULEE)
        LigneDeliberation.objects.create(
            deliberation=delib, inscription=inscription,
            moyenne_generale=Decimal('12'),
            decision=LigneDeliberation.DECISION_ADMIS,
            # Sans decision automatique identique, le jury annuel exige un
            # motif d'ajustement — hors sujet ici.
            decision_auto=LigneDeliberation.DECISION_ADMIS)
        AuditLog.objects.all().delete()
        return delib

    def _trace_verrouillage(self):
        from core.models import ACTION_BULK_UPDATE, AuditLog
        return (AuditLog.objects
                .filter(model_name='Note', action=ACTION_BULK_UPDATE)
                .first())

    def test_un_jury_de_semestre_trace_le_verrouillage(self):
        delib = self._delibere(Deliberation.PORTEE_SEMESTRE)
        with self.captureOnCommitCallbacks(execute=True):
            services.valider_deliberation(delib, self.jury)

        trace = self._trace_verrouillage()
        self.assertIsNotNone(trace, 'le verrouillage des notes doit figurer')
        self.assertEqual(trace.changes['notes'], 1)
        self.assertIn('verrouillée', trace.label)
        self.assertTrue(trace.keep_forever,
                        'figer des notes est une décision, pas un événement courant')

    def test_un_jury_annuel_trace_aussi_le_verrouillage(self):
        delib = self._delibere(Deliberation.PORTEE_ANNUELLE)
        with self.captureOnCommitCallbacks(execute=True):
            services.valider_deliberation(delib, self.jury)

        self.assertIsNotNone(self._trace_verrouillage(),
                             'les deux portées passent par le même verrou')
