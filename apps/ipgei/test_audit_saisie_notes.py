"""
Test — l'écran de saisie des notes laisse une trace exploitable.

`test_audit_notes_et_mdp.py` prouve que le MODÈLE `Evaluation` est tracé. Ici on
passe par l'ENDPOINT réellement utilisé par l'écran de saisie,
`POST /api/v1/ipgei/notes/saisie-collective/`, et par toute la pile HTTP —
middleware compris. Deux choses ne se vérifient qu'ainsi :

1. **L'effacement d'une note.** L'écran efface un DS en envoyant `valeur: null`,
   et la vue traduit ça en `Evaluation.objects.filter(...).delete()` — une
   suppression par QUERYSET, pas par instance. Django court-circuite les signaux
   sur ce chemin (« fast delete ») tant qu'aucun récepteur n'est branché sur le
   modèle. Tracer `Evaluation` suffit donc à faire émerger la trace, mais rien
   ne le garantit sans un test qui emprunte ce chemin-là.

2. **L'auteur.** `AuditMiddleware` tourne après `AuthenticationMiddleware`, ce
   qui suffit à l'authentification par SESSION — mais l'API est en JWT, et DRF
   n'authentifie qu'à l'entrée de la vue. Appeler la vue directement, comme le
   font les autres tests, ne dit donc rien sur ce qui arrive en vrai.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from apps.absence.models import Etudiant
from apps.authentication.models import CustomUser
from apps.ipgei.models import (
    Classe, Evaluation, InscriptionIPGEI, Matiere, NiveauIPGEI, Note,
    SemestreIPGEI,
)
from apps.parametres.models import Institution, Niveau
from core.models import (
    ACTION_CREATE, ACTION_DELETE, ACTION_UPDATE, AuditLog,
)

URL = '/api/v1/ipgei/notes/saisie-collective/'


class AuditSaisieCollectiveTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MP')
        cls.ref = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                             libelle='MP', rang=2)
        cls.annee = '2025-2026'
        cls.s3 = SemestreIPGEI.objects.create(institution=cls.inst, code='S3',
                                              annee_universitaire=cls.annee,
                                              date_debut=date(2025, 9, 1))
        cls.math = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Mathématiques',
            code_semestre='S3', niveau_ref=cls.ref, coefficient=Decimal('4'),
            has_tp=True, pct_ds=Decimal('20'), pct_tp=Decimal('10'),
            pct_exam=Decimal('70'), volume_cm=95)
        cls.classe = Classe.objects.create(nom='MP A', institution=cls.inst,
                                           annee_universitaire=cls.annee,
                                           niveau=cls.socle, groupe='A')
        etudiant = Etudiant.objects.create(matricule='242101', nom='SEJAD')
        cls.inscription = InscriptionIPGEI.objects.create(
            etudiant=etudiant, classe=cls.classe, annee_universitaire=cls.annee)
        cls.prof = CustomUser.objects.create_user(
            username='enseignant', email='p@t.l', password='Prof!2612mdp',
            role='admin')

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.prof)
        AuditLog.objects.all().delete()

    def _saisir(self, valeur, numero=1, type_evaluation='ds'):
        """Ce que l'écran envoie. `valeur=None` efface l'évaluation."""
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(URL, {
                'classe':   self.classe.pk,
                'matiere':  self.math.pk,
                'semestre': self.s3.pk,
                'lignes': [{
                    'inscription': self.inscription.pk,
                    'evaluations': [{'type_evaluation': type_evaluation,
                                     'numero': numero, 'valeur': valeur}],
                }],
            }, format='json')

    def _traces(self, action=None):
        q = AuditLog.objects.filter(model_name='Evaluation')
        return q.filter(action=action) if action else q

    # ── Saisie ──────────────────────────────────────────────────────────────
    def test_une_note_saisie_par_l_ecran_laisse_une_trace(self):
        reponse = self._saisir('12.50')
        self.assertEqual(reponse.status_code, 200, reponse.data)

        trace = self._traces(ACTION_CREATE).first()
        self.assertIsNotNone(trace, "la saisie d'une note doit figurer au journal")
        self.assertIn('242101', trace.label, 'le journal doit dire DE QUI')
        self.assertIn('MATH', trace.label, 'le journal doit dire DANS QUOI')
        self.assertIn('12.50', trace.label)

    def test_la_trace_porte_le_nom_de_qui_a_saisi(self):
        """
        Le point que seul le passage par la pile HTTP peut prouver : DRF
        n'authentifie qu'à l'entrée de la vue, bien après le middleware.
        """
        self._saisir('12.50')

        trace = self._traces(ACTION_CREATE).first()
        self.assertIsNotNone(trace)
        self.assertEqual(trace.user_id, self.prof.pk,
                         "le journal doit dire QUI a saisi, pas seulement quoi")

    # ── Modification ────────────────────────────────────────────────────────
    def test_une_note_corrigee_dit_l_ancienne_et_la_nouvelle(self):
        self._saisir('8')
        AuditLog.objects.all().delete()
        reponse = self._saisir('12')
        self.assertEqual(reponse.status_code, 200, reponse.data)

        trace = self._traces(ACTION_UPDATE).first()
        self.assertIsNotNone(trace, 'la correction doit figurer au journal')
        self.assertIn('valeur', trace.changes)
        self.assertEqual(Decimal(str(trace.changes['valeur']['old'])), Decimal('8'))
        self.assertEqual(Decimal(str(trace.changes['valeur']['new'])), Decimal('12'))
        self.assertEqual(trace.user_id, self.prof.pk)

    def test_une_note_inchangee_ne_produit_pas_de_trace(self):
        """Renvoyer la grille sans rien toucher ne doit pas gonfler le journal."""
        self._saisir('12')
        AuditLog.objects.all().delete()
        self._saisir('12')

        self.assertEqual(self._traces(ACTION_UPDATE).count(), 0)

    # ── Effacement ──────────────────────────────────────────────────────────
    def test_une_note_effacee_par_l_ecran_laisse_une_trace(self):
        """
        L'écran efface en envoyant `valeur: null`. La vue traduit en
        `filter(...).delete()` — une suppression par QUERYSET, celle que Django
        court-circuite tant qu'aucun récepteur n'écoute le modèle.
        """
        self._saisir('15')
        AuditLog.objects.all().delete()
        reponse = self._saisir(None)
        self.assertEqual(reponse.status_code, 200, reponse.data)

        self.assertFalse(
            Evaluation.objects.filter(note__inscription=self.inscription).exists(),
            "l'évaluation devait bien être effacée")
        trace = self._traces(ACTION_DELETE).first()
        self.assertIsNotNone(trace, "une note effacée ne doit pas s'évaporer")
        # La valeur perdue doit rester lisible : c'est tout l'intérêt de la trace.
        self.assertEqual(Decimal(str(trace.changes['valeur']['old'])), Decimal('15'))
        self.assertEqual(trace.user_id, self.prof.pk)

    # ── Note de TP : elle ne vit pas dans `Evaluation` ───────────────────────
    def test_le_tp_saisi_laisse_une_trace_sur_la_note(self):
        """
        Le TP n'est pas une `Evaluation` : il est porté par `Note.note_tp`.
        Il se trace donc ailleurs, et il fallait le vérifier.
        """
        with self.captureOnCommitCallbacks(execute=True):
            reponse = self.client.post(URL, {
                'classe':   self.classe.pk,
                'matiere':  self.math.pk,
                'semestre': self.s3.pk,
                'lignes': [{'inscription': self.inscription.pk, 'note_tp': '16'}],
            }, format='json')
        self.assertEqual(reponse.status_code, 200, reponse.data)

        trace = (AuditLog.objects
                 .filter(model_name='Note', action=ACTION_UPDATE,
                         changes__has_key='note_tp')
                 .first())
        self.assertIsNotNone(trace, 'la saisie du TP doit figurer au journal')
        self.assertEqual(Decimal(str(trace.changes['note_tp']['new'])), Decimal('16'))

    def test_la_note_recalculee_reste_distincte_de_la_note_saisie(self):
        """
        Le recalcul écrit sur `Note` ; la saisie humaine écrit sur `Evaluation`.
        Les confondre ferait passer un calcul automatique pour une saisie.
        """
        self._saisir('12.50')

        saisie = self._traces(ACTION_CREATE).first()
        self.assertIsNotNone(saisie)
        recalculs = AuditLog.objects.filter(model_name='Note', action=ACTION_UPDATE)
        self.assertGreater(recalculs.count(), 0,
                           'le recalcul de la moyenne doit rester visible')
        for ligne in recalculs:
            self.assertNotIn('valeur', ligne.changes,
                             "un recalcul ne doit pas ressembler à une saisie")
