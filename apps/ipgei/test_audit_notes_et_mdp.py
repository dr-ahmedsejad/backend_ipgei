"""
Test — le journal d'audit voit la SAISIE DES NOTES et les REMISES A ZERO
de mot de passe.

Trois trous mesurés dans la base réelle avant correction :

1. `ipgei.Evaluation` n'était pas tracé : 3 432 évaluations, 0 ligne au
   journal. C'est pourtant là que vit la note réellement saisie — DS 1, DS 2,
   examen. `Note` n'en porte que la moyenne recalculée. On voyait donc une
   moyenne bouger sans jamais savoir quelle note l'avait fait bouger ni de
   combien ; une note effacée ne laissait, elle, aucune trace du tout.

2. La CRÉATION d'une note passait par `bulk_create`, qui ne déclenche aucun
   signal : 1 716 notes en base pour zéro `CREATE` au journal.

3. `password` est dans `BLACKLIST_FIELDS` — aucun hachage ne doit atterrir au
   journal. Effet de bord : le diff générique sur `CustomUser` ne voyait JAMAIS
   un changement de mot de passe. Une remise à zéro depuis l'admin Django, un
   shell ou un script ne laissait donc aucune trace.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.absence.models import Etudiant
from apps.authentication.models import CustomUser
from apps.ipgei.models import (
    Classe, Evaluation, InscriptionIPGEI, Matiere, NiveauIPGEI, Note,
    SemestreIPGEI,
)
from apps.parametres.models import Institution, Niveau
from core.audit_context import clear_request_context, set_request_context
from core.models import (
    ACTION_BULK_CREATE, ACTION_DELETE, ACTION_PASSWORD_CHANGED,
    ACTION_PASSWORD_RESET, ACTION_UPDATE, AuditLog,
)


class AuditNotesTest(TestCase):
    """La note saisie, modifiée ou effacée doit se lire au journal."""

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle_mp = Niveau.objects.create(niveau='MP')
        cls.ref_mp = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                                libelle='MP', rang=2)
        cls.annee = '2025-2026'
        cls.s3 = SemestreIPGEI.objects.create(institution=cls.inst, code='S3',
                                              annee_universitaire=cls.annee,
                                              date_debut=date(2025, 9, 1))
        cls.math = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Mathématiques',
            code_semestre='S3', niveau_ref=cls.ref_mp, coefficient=Decimal('4'),
            has_tp=True, pct_ds=Decimal('20'), pct_tp=Decimal('10'),
            pct_exam=Decimal('70'), volume_cm=95)
        cls.classe = Classe.objects.create(nom='MP A', institution=cls.inst,
                                           annee_universitaire=cls.annee,
                                           niveau=cls.socle_mp, groupe='A')

    def setUp(self):
        AuditLog.objects.all().delete()

    def _inscrit(self, matricule):
        etudiant = Etudiant.objects.create(matricule=matricule, nom='SEJAD')
        return InscriptionIPGEI.objects.create(etudiant=etudiant, classe=self.classe,
                                               annee_universitaire=self.annee)

    def _note(self, matricule='242101'):
        return Note.objects.create(inscription=self._inscrit(matricule),
                                   matiere=self.math, semestre=self.s3)

    def _lignes(self, modele, action=None):
        q = AuditLog.objects.filter(model_name=modele)
        return q.filter(action=action) if action else q

    def test_une_note_saisie_laisse_une_trace(self):
        note = self._note()
        AuditLog.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            Evaluation.objects.create(note=note, type_evaluation=Evaluation.TYPE_DS,
                                      numero=1, valeur=Decimal('12.50'))

        trace = self._lignes('Evaluation').first()
        self.assertIsNotNone(trace, 'la note saisie doit figurer au journal')
        # Le libellé doit dire DE QUI et DANS QUOI : au journal, contrairement
        # à l'écran de saisie, l'étudiant et la matière ne sont nulle part.
        self.assertIn('242101', trace.label)
        self.assertIn('MATH', trace.label)
        self.assertIn('12.50', trace.label)

    def test_une_note_modifiee_dit_l_ancienne_et_la_nouvelle(self):
        evaluation = Evaluation.objects.create(
            note=self._note('242102'), type_evaluation=Evaluation.TYPE_DS,
            numero=1, valeur=Decimal('8.00'))
        AuditLog.objects.all().delete()

        with self.captureOnCommitCallbacks(execute=True):
            evaluation.valeur = Decimal('12.00')
            evaluation.save()

        trace = self._lignes('Evaluation', ACTION_UPDATE).first()
        self.assertIsNotNone(trace, 'la modification doit figurer au journal')
        self.assertIn('valeur', trace.changes)
        self.assertEqual(Decimal(str(trace.changes['valeur']['old'])), Decimal('8'))
        self.assertEqual(Decimal(str(trace.changes['valeur']['new'])), Decimal('12'))

    def test_une_note_effacee_laisse_une_trace(self):
        evaluation = Evaluation.objects.create(
            note=self._note('242103'), type_evaluation=Evaluation.TYPE_DS,
            numero=1, valeur=Decimal('15.00'))
        AuditLog.objects.all().delete()

        with self.captureOnCommitCallbacks(execute=True):
            evaluation.delete()

        trace = self._lignes('Evaluation', ACTION_DELETE).first()
        self.assertIsNotNone(trace, "une note effacée ne doit pas s'évaporer")
        # La valeur perdue doit rester lisible : c'est tout l'intérêt.
        self.assertEqual(Decimal(str(trace.changes['valeur']['old'])), Decimal('15'))

    def test_l_initialisation_des_notes_laisse_une_trace(self):
        """`bulk_create` ne déclenche aucun signal : le service doit tracer."""
        from apps.ipgei.services import initialiser_notes

        inscription = self._inscrit('242104')
        AuditLog.objects.all().delete()

        with self.captureOnCommitCallbacks(execute=True):
            cree = initialiser_notes(inscription, self.s3)

        self.assertGreater(cree, 0, 'la maquette doit rattacher au moins MATH')
        trace = self._lignes('Note', ACTION_BULK_CREATE).first()
        self.assertIsNotNone(trace, 'la création de notes doit figurer')
        self.assertIn('MATH', trace.changes.get('matieres', []))
        self.assertIn('242104', trace.label)


class AuditMotDePasseTest(TestCase):
    """Qui change le mot de passe de qui, et depuis où."""

    @classmethod
    def setUpTestData(cls):
        cls.cible = CustomUser.objects.create_user(
            username='etudiant1', email='e@t.l', password='Ancien!2612mdp',
            role='etudiant')
        cls.admin = CustomUser.objects.create_user(
            username='patron', email='a@t.l', password='Patron!2612mdp',
            role='admin')

    def setUp(self):
        AuditLog.objects.all().delete()
        self.addCleanup(clear_request_context)

    def _lignes(self, action):
        return AuditLog.objects.filter(action=action)

    def test_un_tiers_qui_change_le_mot_de_passe_est_une_reinitialisation(self):
        set_request_context(user=self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            self.cible.set_password('Impose!2612mdp')
            self.cible.save()

        trace = self._lignes(ACTION_PASSWORD_RESET).first()
        self.assertIsNotNone(trace, 'une remise à zéro par un tiers doit figurer')
        self.assertEqual(trace.object_id, str(self.cible.pk))
        self.assertIn('patron', trace.label)
        self.assertTrue(trace.keep_forever, 'une trace de sécurité ne se purge pas')

    def test_sans_acteur_la_console_est_nommee(self):
        """Admin Django hors requête, shell, commande de gestion."""
        with self.captureOnCommitCallbacks(execute=True):
            self.cible.set_password('Console!2612mdp')
            self.cible.save()

        trace = self._lignes(ACTION_PASSWORD_RESET).first()
        self.assertIsNotNone(trace, 'un changement hors requête doit figurer')
        self.assertIn('console', trace.label)

    def test_le_titulaire_lui_meme_est_un_changement_pas_une_remise_a_zero(self):
        set_request_context(user=self.cible)
        with self.captureOnCommitCallbacks(execute=True):
            self.cible.set_password('Choisi!2612mdp')
            self.cible.save()

        self.assertEqual(self._lignes(ACTION_PASSWORD_RESET).count(), 0)
        self.assertEqual(self._lignes(ACTION_PASSWORD_CHANGED).count(), 1)

    def test_aucun_hachage_n_atterrit_au_journal(self):
        set_request_context(user=self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            self.cible.set_password('Secret!2612mdp')
            self.cible.save()

        for trace in AuditLog.objects.all():
            contenu = str(trace.changes) + (trace.label or '')
            self.assertNotIn('pbkdf2', contenu)
            self.assertNotIn('Secret!2612mdp', contenu)

    def test_un_enregistrement_sans_changement_de_mot_de_passe_ne_trace_rien(self):
        set_request_context(user=self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            self.cible.email = 'autre@t.l'
            self.cible.save()

        self.assertEqual(self._lignes(ACTION_PASSWORD_RESET).count(), 0)
        self.assertEqual(self._lignes(ACTION_PASSWORD_CHANGED).count(), 0)

    def test_une_creation_de_compte_n_est_pas_une_remise_a_zero(self):
        set_request_context(user=self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            CustomUser.objects.create_user(
                username='nouveau', email='n@t.l', password='Neuf!2612mdp',
                role='etudiant')

        self.assertEqual(self._lignes(ACTION_PASSWORD_RESET).count(), 0)
