"""
Test — qui peut générer le suivi d'une semaine ÉCOULÉE (rattrapage).

Le rattrapage était réservé à l'administrateur : tout autre compte devait lui
demander une autorisation, semaine par semaine. Le DIRECTEUR DES ÉTUDES tient
l'emploi du temps et le suivi — lui faire réclamer une autorisation pour
rattraper une semaine de retard arrêtait son travail sur un geste purement
administratif.

Ce que le test fige :
  1. le DE génère une semaine close sans autorisation préalable ;
  2. l'administrateur aussi, comme avant ;
  3. un autre compte (scolarité) est toujours refusé, et le message le dit ;
  4. ce compte-là passe s'il détient une autorisation, qui est alors consommée ;
  5. une semaine EN COURS reste ouverte à tous ;
  6. l'écran sait qui peut rattraper (`is_admin_user`) ;
  7. le DE ne figure plus parmi les comptes à qui proposer une autorisation.

Les dates sont posées EN RELATIF : figées, ces tests passeraient le jour de leur
écriture puis tomberaient d'eux-mêmes quelques semaines plus tard.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.authentication.models import Action, Module, ModuleAction, UserPermission
from apps.departement.models import Departement
from apps.ipgei.models import Classe, GrilleType, SeanceType
from apps.parametres.models import Creneau, Institution, Niveau, Seance
from apps.parametres.models import Jour, Semaine
from apps.suivi.models import SuiviGenerationAuthorization
from apps.suivi.views import peut_rattraper_suivi

User = get_user_model()

ANNEE = '2026-2027'
TS    = 'I'
GENERER = '/api/v1/suivi/suivies/ajouter/'
LISTE   = '/api/v1/suivi/suivies/semaines-generees/'


class RattrapageSuiviTest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.jour = Jour.objects.create(jour='Lundi')
        # Semaine 1 : close depuis un mois. Semaine 2 : en cours.
        cls.close = 1
        cls.en_cours = 2
        for numero, decalage in ((cls.close, -30), (cls.en_cours, 0)):
            Semaine.objects.create(
                numero_semaine=numero, jour_fk=cls.jour,
                date=date.today() + timedelta(days=decalage),
                annee_universitaire=ANNEE, type_semestre=TS)

        # Les droits RBAC viennent de migrations de données, absentes sous
        # `--no-migrations` : on pose ici le seul droit que ces écrans exigent.
        module, _ = Module.objects.get_or_create(code='suivi_saisie', defaults={'nom': 'Suivi'})
        droits = {}
        for code in ('voir', 'modifier', 'supprimer', 'exporter'):
            action, _ = Action.objects.get_or_create(code=code, defaults={'nom': code})
            droits[code], _ = ModuleAction.objects.get_or_create(module=module, action=action)

        # La génération est scopée aux groupes attribués : sans groupe, l'écran
        # refuse avant même d'arriver à la règle de rattrapage.
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test', est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MP')
        # Une classe IPGEI et sa grille type remplie : sans emploi du temps à
        # recopier, la génération s'arrête AVANT la règle qu'on teste.
        cls.groupe = Classe.objects.create(nom='MP A', annee_universitaire=ANNEE,
                                           institution=cls.inst, niveau=cls.socle,
                                           groupe='A')
        creneau = Creneau.objects.create(creneau='08h00-09h30', ordre=1, is_actif=True)
        grille = GrilleType.objects.create(classe=cls.groupe, type_semestre=TS)
        type_seance = Seance.objects.create(type_seance='CM')
        SeanceType.objects.create(grille=grille, jour=cls.jour, creneau=creneau,
                                  type_seance=type_seance)

        def compte(nom, role):
            u = User.objects.create_user(username=nom, email=f'{nom}@t.l',
                                         password='Xk93!plqz72', role=role)
            for ma in droits.values():
                UserPermission.objects.create(user=u, module_action=ma, allowed=True)
            u.managed_departements.add(cls.groupe)
            return u

        cls.de        = compte('de_suivi', 'DE')
        cls.admin     = compte('admin_suivi', 'admin')
        cls.scolarite = compte('scol_suivi', 'scolarite')

    def _generer(self, user, numero):
        self.client.force_authenticate(user=user)
        return self.client.post(GENERER, {
            'annee_universitaire': ANNEE, 'type_semestre': TS, 'numero_semaine': numero,
        }, format='json')

    # ── La règle elle-même ──────────────────────────────────────────────────
    def test_la_regle_nomme_le_de_et_l_admin(self):
        self.assertTrue(peut_rattraper_suivi(self.de))
        self.assertTrue(peut_rattraper_suivi(self.admin))
        self.assertFalse(peut_rattraper_suivi(self.scolarite))

    # ── Semaine close ───────────────────────────────────────────────────────
    def test_le_de_rattrape_sans_autorisation(self):
        reponse = self._generer(self.de, self.close)
        self.assertNotEqual(reponse.status_code, 403, reponse.data)

    def test_l_admin_rattrape_toujours(self):
        reponse = self._generer(self.admin, self.close)
        self.assertNotEqual(reponse.status_code, 403, reponse.data)

    def test_un_autre_compte_est_refuse_et_sait_a_qui_demander(self):
        reponse = self._generer(self.scolarite, self.close)
        self.assertEqual(reponse.status_code, 403, reponse.data)
        message = str(reponse.data.get('error', reponse.data)).lower()
        self.assertIn('rattrapage', message)
        self.assertIn('directeur des etudes', message)

    def test_une_autorisation_debloque_ce_compte(self):
        SuiviGenerationAuthorization.objects.create(
            user=self.scolarite, granted_by=self.admin, annee_universitaire=ANNEE,
            type_semestre=TS, numero_semaine=self.close)
        reponse = self._generer(self.scolarite, self.close)
        self.assertNotEqual(reponse.status_code, 403, reponse.data)

    # ── Semaine en cours ────────────────────────────────────────────────────
    def test_la_semaine_en_cours_reste_ouverte_a_tous(self):
        reponse = self._generer(self.scolarite, self.en_cours)
        self.assertNotEqual(reponse.status_code, 403, reponse.data)

    # ── Ce que l'écran lit ──────────────────────────────────────────────────
    def test_l_ecran_sait_qui_peut_rattraper(self):
        for user, attendu in ((self.de, True), (self.admin, True), (self.scolarite, False)):
            with self.subTest(user=user.username):
                self.client.force_authenticate(user=user)
                r = self.client.get(LISTE, {'annee_universitaire': ANNEE, 'type_semestre': TS})
                self.assertEqual(r.status_code, 200, r.data)
                self.assertEqual(r.data.get('is_admin_user'), attendu)

    def test_le_de_n_est_plus_propose_pour_une_autorisation(self):
        """Lui accorder une autorisation n'aurait plus d'objet."""
        self.client.force_authenticate(user=self.admin)
        r = self.client.get('/api/v1/suivi/rattrapages/pending/',
                            {'annee_universitaire': ANNEE, 'type_semestre': TS})
        self.assertEqual(r.status_code, 200, r.data)
        noms = [u.get('username') for u in r.data.get('users', [])]
        self.assertNotIn('de_suivi', noms)
        self.assertNotIn('admin_suivi', noms)
