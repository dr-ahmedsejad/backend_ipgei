"""
Test — le journal d'audit voit les événements d'AUTHENTIFICATION.

Le journal déclarait quatorze actions ; sept n'étaient jamais écrites. Les plus
gênantes : la connexion RÉUSSIE, la déconnexion, le changement de mot de passe
et le refus d'accès. Seuls les ÉCHECS de connexion y figuraient — de quoi lire
un journal où personne ne se connecte jamais et où tout le monde se trompe.

La cause n'était pas un oubli de récepteur : `core/signals.py` écoute bien
`user_logged_in` et `user_logged_out`. Mais ces signaux ne sont émis que par
`django.contrib.auth.login()` / `logout()`, qui ouvrent et ferment une SESSION.
L'authentification est ici par JWT : aucune session, donc aucun signal.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date

from django.test import TestCase
from rest_framework.test import APIClient

from apps.authentication.models import CustomUser
from apps.parametres.models import Year
from core.models import (
    ACTION_LOGIN_FAILED, ACTION_LOGIN_SUCCESS, ACTION_LOGOUT,
    ACTION_PASSWORD_CHANGED, ACTION_PERMISSION_DENIED, AuditLog,
)

MDP = 'Xk93!plqz72'


class AuditAuthentificationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Le serializer de connexion refuse tout si aucune annee n'existe : il
        # doit poser un contexte a l'utilisateur.
        Year.objects.create(annee='2024-2025', date_debut=date(2024, 9, 1),
                            date_fin=date(2025, 7, 31), est_active=True)
        cls.user = CustomUser.objects.create_user(
            username='journal', email='j@t.l', password=MDP, role='AA')

    def setUp(self):
        self.client = APIClient()
        AuditLog.objects.all().delete()

    def _lignes(self, action):
        return AuditLog.objects.filter(action=action)

    def _appel(self, methode, *args, **kwargs):
        """
        Un appel HTTP dont les ecritures de journal aboutissent vraiment.

        `write_audit` differe l'ecriture a `transaction.on_commit` — ce qui la
        rend inoffensive en production, mais invisible dans un `TestCase`, dont
        la transaction est toujours annulee. Sans ce filet, le test dirait
        « rien au journal » alors que le code fait ce qu'il faut.
        """
        with self.captureOnCommitCallbacks(execute=True):
            return getattr(self.client, methode)(*args, **kwargs)

    # ── Connexion ───────────────────────────────────────────────────────────
    def test_une_connexion_reussie_laisse_une_trace(self):
        reponse = self._appel('post', '/api/v1/auth/login/',
                              {'username': 'journal', 'password': MDP}, format='json')
        self.assertEqual(reponse.status_code, 200, reponse.data)

        trace = self._lignes(ACTION_LOGIN_SUCCESS).first()
        self.assertIsNotNone(trace, "la connexion réussie doit figurer au journal")
        self.assertEqual(trace.object_id, str(self.user.pk))
        self.assertIn('journal', trace.label)

    def test_un_echec_de_connexion_reste_trace(self):
        """Il l'était déjà — la correction ne doit pas l'avoir cassé."""
        self._appel('post', '/api/v1/auth/login/',
                    {'username': 'journal', 'password': 'faux'}, format='json')
        self.assertEqual(self._lignes(ACTION_LOGIN_FAILED).count(), 1)

    def test_l_echec_se_conserve_indefiniment(self):
        self._appel('post', '/api/v1/auth/login/',
                    {'username': 'journal', 'password': 'faux'}, format='json')
        self.assertTrue(self._lignes(ACTION_LOGIN_FAILED).first().keep_forever)

    # ── Déconnexion ─────────────────────────────────────────────────────────
    def test_une_deconnexion_laisse_une_trace(self):
        self.client.force_authenticate(user=self.user)
        reponse = self._appel('post', '/api/v1/auth/logout/')
        self.assertEqual(reponse.status_code, 200, reponse.data)

        trace = self._lignes(ACTION_LOGOUT).first()
        self.assertIsNotNone(trace, "la déconnexion doit figurer au journal")
        self.assertEqual(trace.object_id, str(self.user.pk))

    # ── Mot de passe ────────────────────────────────────────────────────────
    def test_un_changement_de_mot_de_passe_laisse_une_trace(self):
        self.client.force_authenticate(user=self.user)
        reponse = self._appel('post', '/api/v1/auth/change-password/', {
            'old_password': MDP,
            'new_password': 'Nouveau!2612mdp',
            'confirm_password': 'Nouveau!2612mdp',
        }, format='json')
        self.assertEqual(reponse.status_code, 200, reponse.data)

        trace = self._lignes(ACTION_PASSWORD_CHANGED).first()
        self.assertIsNotNone(trace, "le changement de mot de passe doit figurer")
        self.assertTrue(trace.keep_forever, 'une trace de sécurité ne se purge pas')
        # Le mot de passe lui-même n'a rien à faire dans un journal.
        self.assertNotIn('Nouveau!2612mdp', str(trace.changes) + trace.label)

    # ── Refus d'accès ───────────────────────────────────────────────────────
    def test_un_refus_d_acces_laisse_une_trace(self):
        # `journal` est un simple AA : le référentiel des matières lui est fermé.
        self.client.force_authenticate(user=self.user)
        reponse = self._appel('get', '/api/v1/ipgei/matieres/')
        self.assertEqual(reponse.status_code, 403, reponse.data)

        trace = self._lignes(ACTION_PERMISSION_DENIED).first()
        self.assertIsNotNone(trace, "un refus d'accès doit figurer au journal")
        self.assertEqual(trace.object_id, str(self.user.pk))
        self.assertIn('/api/v1/ipgei/matieres/', trace.label)

    def test_un_refus_anonyme_ne_pollue_pas_le_journal(self):
        """Sans utilisateur identifié, le refus n'apprend rien et noierait le reste."""
        self._appel('get', '/api/v1/ipgei/matieres/')
        self.assertEqual(self._lignes(ACTION_PERMISSION_DENIED).count(), 0)

    # ── Le journal ne doit jamais casser la réponse ──────────────────────────
    def test_une_ecriture_de_journal_qui_echoue_ne_casse_pas_le_403(self):
        from unittest.mock import patch

        self.client.force_authenticate(user=self.user)
        with patch('core.audit_helpers.write_audit', side_effect=RuntimeError('boum')):
            reponse = self._appel('get', '/api/v1/ipgei/matieres/')
        self.assertEqual(reponse.status_code, 403,
                         "le journal ne doit pas transformer un 403 en 500")
