"""
Test de l'endpoint de contrôle d'accès /media/ (Nginx auth_request).

Ce que l'endpoint garantit :
  1. un anonyme est refusé, un connecté sans chemin obtient 204 (réponse
     historique — la route est `internal`, elle ne livre rien) ;
  2. un étudiant lit SON document officiel et pas celui d'un camarade —
     c'était la faille : les numéros de série sont séquentiels, un 204
     inconditionnel laissait énumérer toute la promotion ;
  3. un porteur du module RBAC du type de fichier lit ceux de tout le monde ;
  4. l'admin lit tout ; un préfixe inconnu est refusé ; une remontée `..`
     est refusée ;
  5. les préfixes partagés (cours, avatars) restent ouverts aux connectés.

Base SQLite en mémoire (siga.settings.test).
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.absence.models import Etudiant
from apps.authentication.models import Action, Module, ModuleAction, UserPermission
from apps.documents.models import DocumentOfficiel
from apps.parametres.models import Institution
from core.media_auth import acces_autorise, chemin_media

User = get_user_model()

URL = '/internal/media-auth/'


def _document(inst, etudiant, serie, chemin):
    return DocumentOfficiel.objects.create(
        institution=inst, etudiant=etudiant, type_document='releve_semestre',
        numero_serie=serie, fichier_pdf=chemin,
    )


class MediaAuthTest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)

        def compte(username, role):
            return User.objects.create_user(
                username=username, email=f'{username}@t.l', password='Xk93!plqz72', role=role)

        cls.admin  = compte('adm', 'admin')
        cls.alice  = compte('alice', 'etudiant')
        cls.bob    = compte('bob', 'etudiant')
        cls.agent  = compte('agent', 'scolarite')     # sans droit RBAC au départ
        cls.doc_ok = compte('doc_ok', 'scolarite')    # porteur de doc_registre

        cls.et_alice = Etudiant.objects.create(matricule='900001', nom='Alice', user=cls.alice)
        cls.et_bob   = Etudiant.objects.create(matricule='900002', nom='Bob',   user=cls.bob)

        cls.chemin_alice = 'documents/officiels/AI-2026-00001.pdf'
        cls.chemin_bob   = 'documents/officiels/AI-2026-00002.pdf'
        _document(cls.inst, cls.et_alice, 'AI-2026-00001', cls.chemin_alice)
        _document(cls.inst, cls.et_bob,   'AI-2026-00002', cls.chemin_bob)

        module = Module.objects.create(code='doc_registre', nom='Registre')
        voir   = Action.objects.create(code='voir', nom='Voir')
        ma     = ModuleAction.objects.create(module=module, action=voir)
        UserPermission.objects.create(user=cls.doc_ok, module_action=ma, allowed=True)

    def _get(self, user, uri=None):
        self.client.force_authenticate(user)
        entetes = {'HTTP_X_ORIGINAL_URI': uri} if uri is not None else {}
        return self.client.get(URL, **entetes)

    # ── 1. comportement historique conservé ─────────────────────────────────
    def test_anonyme_refuse(self):
        resp = self.client.get(URL)
        self.assertIn(resp.status_code, (401, 403))

    def test_authentifie_sans_chemin_autorise(self):
        self.assertEqual(self._get(self.alice).status_code, 204)

    # ── 2. la propriété — la faille corrigée ────────────────────────────────
    def test_l_etudiant_lit_son_document(self):
        self.assertEqual(self._get(self.alice, '/media/' + self.chemin_alice).status_code, 204)

    def test_l_etudiant_ne_lit_pas_celui_d_un_camarade(self):
        self.assertEqual(self._get(self.alice, '/media/' + self.chemin_bob).status_code, 403)
        self.assertEqual(self._get(self.bob,   '/media/' + self.chemin_alice).status_code, 403)

    def test_un_numero_de_serie_devine_ne_donne_rien(self):
        # Le fichier n'existe pas en base : personne ne le « possède ».
        resp = self._get(self.alice, '/media/documents/officiels/AI-2026-00999.pdf')
        self.assertEqual(resp.status_code, 403)

    # ── 3. le module RBAC ouvre le type de fichier ──────────────────────────
    def test_le_porteur_du_module_lit_tout(self):
        self.assertEqual(self._get(self.doc_ok, '/media/' + self.chemin_alice).status_code, 204)
        self.assertEqual(self._get(self.doc_ok, '/media/' + self.chemin_bob).status_code, 204)

    def test_un_agent_sans_module_ne_lit_rien(self):
        self.assertEqual(self._get(self.agent, '/media/' + self.chemin_alice).status_code, 403)

    # ── 4. admin, préfixe inconnu, remontée ─────────────────────────────────
    def test_l_admin_lit_tout(self):
        self.assertEqual(self._get(self.admin, '/media/' + self.chemin_bob).status_code, 204)
        self.assertEqual(self._get(self.admin, '/media/quelque/chose.bin').status_code, 204)

    def test_un_prefixe_inconnu_est_refuse(self):
        self.assertEqual(self._get(self.alice, '/media/inconnu/x.pdf').status_code, 403)

    def test_une_remontee_est_refusee(self):
        self.assertEqual(self._get(self.admin, '/media/../siga/settings/base.py').status_code, 403)
        self.assertIsNone(chemin_media('/media/../x'))
        self.assertIsNone(chemin_media('/static/x.css'))

    # ── 5. préfixes partagés ────────────────────────────────────────────────
    def test_les_supports_de_cours_restent_partages(self):
        self.assertEqual(self._get(self.alice, '/media/cours/2026/09/td1.pdf').status_code, 204)
        self.assertEqual(self._get(self.alice, '/media/avatars/agent.png').status_code, 204)

    # ── la fonction nue ─────────────────────────────────────────────────────
    def test_chemin_media_decode_et_ignore_la_query(self):
        self.assertEqual(chemin_media('/media/documents/officiels/AI%202026.pdf?dl=1'),
                         'documents/officiels/AI 2026.pdf')

    def test_acces_autorise_est_ferme_par_defaut(self):
        self.assertFalse(acces_autorise(self.alice, 'preinscriptions/identite/x.jpg'))
        self.assertTrue(acces_autorise(self.admin, 'preinscriptions/identite/x.jpg'))
