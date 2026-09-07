"""
Test — un administrateur réinitialise le mot de passe de n'importe quel compte.

Il n'existait aucune réinitialisation : ni endpoint, ni écran. Un utilisateur
qui perdait son mot de passe n'avait de recours qu'une intervention en base, et
l'action `PASSWORD_RESET` du journal, faute d'écrivain, ne se déclenchait jamais
depuis l'application.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date

from django.test import TestCase
from rest_framework.test import APIClient

from apps.authentication.models import CustomUser
from apps.authentication.views import engendrer_mot_de_passe
from apps.parametres.models import Year
from core.models import ACTION_PASSWORD_RESET, AuditLog

URL = '/api/v1/auth/users/{pk}/reinitialiser-mot-de-passe/'
MDP = 'Ancien!2612mdp'


class ReinitialisationParAdminTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Le serializer de connexion refuse tout si aucune année n'existe.
        Year.objects.create(annee='2024-2025', date_debut=date(2024, 9, 1),
                            date_fin=date(2025, 7, 31), est_active=True)
        cls.admin = CustomUser.objects.create_user(
            username='patron', email='a@t.l', password=MDP, role='admin')
        cls.cible = CustomUser.objects.create_user(
            username='etudiant1', email='e@t.l', password=MDP, role='etudiant')
        cls.autre_admin = CustomUser.objects.create_user(
            username='patron2', email='a2@t.l', password=MDP, role='admin')
        cls.simple = CustomUser.objects.create_user(
            username='agent', email='s@t.l', password=MDP, role='AA')

    def setUp(self):
        self.client = APIClient()
        AuditLog.objects.all().delete()

    def _appel(self, cible, corps=None, *, par=None):
        self.client.force_authenticate(user=par or self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(URL.format(pk=cible.pk), corps or {},
                                    format='json')

    # ── Le geste ────────────────────────────────────────────────────────────
    def test_l_admin_reinitialise_et_recoit_le_mot_de_passe(self):
        reponse = self._appel(self.cible)
        self.assertEqual(reponse.status_code, 200, reponse.data)

        nouveau = reponse.data['mot_de_passe']
        self.assertTrue(nouveau, 'un mot de passe engendré doit être rendu')
        self.cible.refresh_from_db()
        self.assertTrue(self.cible.check_password(nouveau))
        self.assertFalse(self.cible.check_password(MDP), "l'ancien ne vaut plus")

    def test_le_compte_devra_en_choisir_un_autre(self):
        self._appel(self.cible)
        self.cible.refresh_from_db()
        self.assertTrue(self.cible.doit_changer_mdp,
                        'un mot de passe connu de deux personnes est provisoire')

    def test_l_admin_peut_imposer_un_mot_de_passe(self):
        reponse = self._appel(self.cible,
                              {'nouveau_mot_de_passe': 'Impose!2612mdp'})
        self.assertEqual(reponse.status_code, 200, reponse.data)
        self.cible.refresh_from_db()
        self.assertTrue(self.cible.check_password('Impose!2612mdp'))
        self.assertIsNone(reponse.data['mot_de_passe'],
                          "on ne renvoie pas ce que l'admin vient de fournir")

    def test_n_importe_quel_compte_y_compris_un_autre_admin(self):
        reponse = self._appel(self.autre_admin)
        self.assertEqual(reponse.status_code, 200, reponse.data)

    # ── Ce qui est refusé ───────────────────────────────────────────────────
    def test_un_admin_ne_reinitialise_pas_son_propre_compte(self):
        """
        `change-password` existe pour cela et exige l'ancien. Passer par ici
        permettrait d'en changer sans le connaître — sur un poste laissé
        déverrouillé, cela suffit à s'emparer du compte.
        """
        reponse = self._appel(self.admin)
        self.assertEqual(reponse.status_code, 400, reponse.data)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password(MDP), "rien n'a changé")

    def test_un_non_admin_ne_peut_pas(self):
        reponse = self._appel(self.cible, par=self.simple)
        self.assertEqual(reponse.status_code, 403, reponse.data)
        self.cible.refresh_from_db()
        self.assertTrue(self.cible.check_password(MDP))

    def test_un_mot_de_passe_trop_faible_est_refuse(self):
        reponse = self._appel(self.cible, {'nouveau_mot_de_passe': '1234'})
        self.assertEqual(reponse.status_code, 400, reponse.data)
        self.cible.refresh_from_db()
        self.assertTrue(self.cible.check_password(MDP))

    # ── La trace ────────────────────────────────────────────────────────────
    def test_la_reinitialisation_laisse_une_trace_nominative(self):
        self._appel(self.cible)

        trace = AuditLog.objects.filter(action=ACTION_PASSWORD_RESET).first()
        self.assertIsNotNone(trace, 'une remise à zéro doit figurer au journal')
        self.assertEqual(trace.object_id, str(self.cible.pk))
        self.assertIn('etudiant1', trace.label, 'de QUI')
        self.assertIn('patron', trace.label, 'par QUI')
        self.assertTrue(trace.keep_forever,
                        'une trace de sécurité ne se purge pas')

    def test_le_mot_de_passe_n_atterrit_jamais_au_journal(self):
        reponse = self._appel(self.cible)
        nouveau = reponse.data['mot_de_passe']

        for trace in AuditLog.objects.all():
            contenu = str(trace.changes) + (trace.label or '')
            self.assertNotIn(nouveau, contenu)
            self.assertNotIn('pbkdf2', contenu)

    # ── Le mot de passe engendré ────────────────────────────────────────────
    def test_le_mot_de_passe_engendre_passe_les_validateurs(self):
        from django.contrib.auth.password_validation import validate_password
        for _ in range(20):
            validate_password(engendrer_mot_de_passe())

    def test_il_ne_contient_aucun_caractere_ambigu(self):
        """Il se transmet à l'oral ou sur un papier : 0 et O ne s'y disputent pas."""
        for _ in range(20):
            self.assertFalse(set(engendrer_mot_de_passe()) & set('0O1lI'))

    def test_deux_appels_ne_donnent_pas_le_meme(self):
        tires = {engendrer_mot_de_passe() for _ in range(50)}
        self.assertEqual(len(tires), 50)

    # ── Le compte réinitialisé peut se connecter ────────────────────────────
    def test_le_compte_se_connecte_avec_le_nouveau_mot_de_passe(self):
        nouveau = self._appel(self.cible).data['mot_de_passe']

        client = APIClient()
        reponse = client.post('/api/v1/auth/login/',
                              {'username': 'etudiant1', 'password': nouveau},
                              format='json')
        self.assertEqual(reponse.status_code, 200, reponse.data)
