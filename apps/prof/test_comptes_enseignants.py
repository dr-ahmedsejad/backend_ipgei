"""
Test — le compte enseignant créé avec la fiche du professeur.

Ce que ces tests figent vient d'une panne réelle. Le 6 octobre 2026,
vingt-sept enseignants ont été saisis un par un à l'écran, avec leur
téléphone ; aucun n'a obtenu de compte. La cause tenait en une ligne :
`CustomUser.email` est UNIQUE, la fonction écrivait `prof.email or ''`, et
une chaîne vide n'est pas un NULL — PostgreSQL l'indexe comme une valeur
ordinaire. Un compte de test à courriel vide, créé le 28 juillet, occupait
la place. Les vingt-sept échecs ont été avalés par un `except Exception` qui
n'écrivait qu'un avertissement dans un journal que personne ne lisait.

Trois règles en découlent, et ce sont elles qu'on protège ici :

  1. le courriel est obligatoire sur la fiche, sans domaine imposé ;
  2. deux fiches ne peuvent pas porter le même ;
  3. un refus est NOMMÉ — la raison dit quel champ corriger.

Base SQLite en mémoire (siga.settings.test).
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from rest_framework.test import APITestCase

from apps.parametres.models import Year
from apps.prof.models import Prof, ProfTypeHistory
from apps.prof.serializers import ProfSerializer
from apps.prof.services import (
    creer_compte_pour_prof, creer_compte_pour_prof_detaille,
)

User = get_user_model()


class TableHistoriqueMixin:
    """`prof_type_history` est `managed = False` : rien ne la crée en test.

    Or le même `post_save` qui pose le compte y écrit le statut initial. Sans
    la table, toute création de Prof échoue avant d'arriver au compte — et ce
    n'est pas ce qu'on mesure ici. Même parade que les tests d'avancement :
    on la crée si elle manque, jamais si elle est déjà là (la recréer
    condamnerait la transaction côté PostgreSQL).
    """

    @classmethod
    def setUpClass(cls):
        if ProfTypeHistory._meta.db_table not in connection.introspection.table_names():
            with connection.schema_editor() as se:
                se.create_model(ProfTypeHistory)
        super().setUpClass()


def fiche(nni, nom, tel, email=''):
    """Une fiche prof. Le signal post_save tente le compte au passage."""
    return Prof.objects.create(NNI=nni, nom=nom, telephone=tel, email=email)


class CompteALaCreationTest(TableHistoriqueMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        Year.objects.create(annee='2026-2027', est_active=True)

    def test_un_prof_complet_obtient_son_compte(self):
        p = fiche(9001, 'Cheikh KHAIRY', 36656361, 'cheikh@gmail.com')
        p.refresh_from_db()
        self.assertIsNotNone(p.user, "le signal doit créer le compte")
        self.assertEqual(p.user.username, '36656361')
        self.assertEqual(p.user.role, 'enseignant')
        self.assertTrue(p.user.doit_changer_mdp)
        self.assertTrue(p.user.check_password('9001'), 'mot de passe = NNI')

    def test_aucun_domaine_n_est_impose(self):
        """Une adresse personnelle convient : rien n'oblige à @ipgei.mr."""
        p = fiche(9002, 'Mamadou BA', 46875041, 'mamadou.ba@gmail.com')
        p.refresh_from_db()
        self.assertEqual(p.user.email, 'mamadou.ba@gmail.com')

    def test_le_contexte_de_session_est_pose(self):
        p = fiche(9003, 'Tah EL HABIB', 37174785, 'tah@ipgei.mr')
        p.refresh_from_db()
        self.assertEqual(p.user.contexte.annee_universitaire, '2026-2027')


class CourrielVideTest(TableHistoriqueMixin, TestCase):
    """LA régression : un courriel vide ne doit plus contaminer les autres."""

    @classmethod
    def setUpTestData(cls):
        Year.objects.create(annee='2026-2027', est_active=True)

    def test_sans_courriel_pas_de_compte_mais_la_fiche_existe(self):
        p = fiche(9101, 'Sans adresse', 22000001)
        p.refresh_from_db()
        self.assertIsNone(p.user, "aucun compte sans courriel")
        self.assertTrue(Prof.objects.filter(pk=p.pk).exists(),
                        "la fiche, elle, doit survivre au refus")

    def test_la_raison_nomme_le_courriel(self):
        p = fiche(9102, 'Sans adresse 2', 22000002)
        cree, raison = creer_compte_pour_prof_detaille(p)
        self.assertFalse(cree)
        self.assertIn('ourriel', raison)

    def test_un_compte_a_courriel_vide_ne_bloque_plus_les_suivants(self):
        """Le cœur du défaut : un seul '' en base privait tout le monde.

        On reproduit l'état exact de la production — un compte dont le
        courriel est la chaîne vide — puis on crée un professeur en règle.
        Avant correction, `create_user` levait une IntegrityError sur
        `authentication_customuser_email_key` et le compte n'était jamais
        créé. Vingt-sept fois.
        """
        User.objects.create_user(username='22000001', password='x',
                                 email='', role='enseignant')
        p = fiche(9103, 'Aboubakry LY', 44280405, 'kraybabou.ly@gmail.com')
        p.refresh_from_db()
        self.assertIsNotNone(
            p.user, "un courriel vide ailleurs ne regarde pas ce professeur")
        self.assertEqual(p.user.email, 'kraybabou.ly@gmail.com')

    def test_deux_profs_sans_courriel_echouent_tous_les_deux_proprement(self):
        """Et surtout : sans écrire le moindre compte à moitié."""
        avant = User.objects.count()
        fiche(9104, 'Premier', 111)
        fiche(9105, 'Second', 555)
        self.assertEqual(User.objects.count(), avant,
                         "aucun compte ne doit avoir été posé")


class DoublonsTest(TableHistoriqueMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        Year.objects.create(annee='2026-2027', est_active=True)

    def test_telephone_deja_pris_la_raison_le_dit(self):
        fiche(9201, 'Premier', 555, 'premier@gmail.com')
        second = fiche(9202, 'Second', 555, 'second@gmail.com')
        second.refresh_from_db()
        self.assertIsNone(second.user)
        cree, raison = creer_compte_pour_prof_detaille(second)
        self.assertFalse(cree)
        self.assertIn('555', raison)
        self.assertIn('éléphone', raison)

    def test_courriel_deja_pris_la_raison_le_dit(self):
        fiche(9203, 'Premier', 33000001, 'partage@gmail.com')
        second = Prof.objects.create(NNI=9204, nom='Second',
                                     telephone=33000002,
                                     email='PARTAGE@gmail.com')
        second.refresh_from_db()
        self.assertIsNone(second.user,
                          'la casse ne doit pas servir de passe-droit')
        cree, raison = creer_compte_pour_prof_detaille(second)
        self.assertFalse(cree)
        self.assertIn('ourriel', raison)

    def test_deja_lie_ne_recree_pas(self):
        p = fiche(9205, 'Deja', 33000003, 'deja@gmail.com')
        p.refresh_from_db()
        cree, raison = creer_compte_pour_prof_detaille(p)
        self.assertFalse(cree)
        self.assertIn('déjà', raison)

    def test_la_variante_booleenne_reste_utilisable(self):
        p = fiche(9206, 'Bool', 33000004, 'bool@gmail.com')
        self.assertFalse(creer_compte_pour_prof(p),
                         'déjà lié par le signal → False, sans exception')


class SerializerTest(TableHistoriqueMixin, TestCase):
    """Le refus doit tomber à la saisie, pas trois mois plus tard."""

    def _candidat(self, **extra):
        donnees = dict(NNI=9301, nom='Test', telephone=33000010, banque=None)
        donnees.update(extra)
        return ProfSerializer(data=donnees)

    def test_creation_sans_courriel_refusee(self):
        s = self._candidat()
        self.assertFalse(s.is_valid())
        self.assertIn('email', s.errors)

    def test_creation_avec_courriel_vide_refusee(self):
        s = self._candidat(email='   ')
        self.assertFalse(s.is_valid())
        self.assertIn('email', s.errors)

    def test_courriel_deja_porte_par_une_autre_fiche_refuse(self):
        fiche(9302, 'Occupant', 33000011, 'occupe@gmail.com')
        s = self._candidat(email='OCCUPE@gmail.com')
        self.assertFalse(s.is_valid())
        self.assertIn('email', s.errors)
        self.assertIn('Occupant', str(s.errors['email']))

    def test_une_fiche_garde_son_propre_courriel_a_la_modification(self):
        p = fiche(9303, 'Lui-meme', 33000012, 'lui@gmail.com')
        s = ProfSerializer(instance=p, data={'email': 'lui@gmail.com'},
                           partial=True)
        self.assertTrue(s.is_valid(), s.errors)

    def test_gmail_accepte(self):
        """Aucun domaine imposé : seul le champ `email` nous intéresse ici.

        La banque reste exigée par ailleurs — on ne juge que le courriel.
        """
        s = self._candidat(email='quelqu.un@gmail.com')
        s.is_valid()
        self.assertNotIn('email', s.errors, s.errors)


class RattrapageTest(TableHistoriqueMixin, APITestCase):
    """`/profs/generer-comptes/` rend ses échecs, pas seulement ses succès."""

    @classmethod
    def setUpTestData(cls):
        Year.objects.create(annee='2026-2027', est_active=True)
        cls.admin = User.objects.create_user(
            username='adm_profs', email='adm_profs@t.l',
            password='Xk93!plqz72', role='admin')

    def setUp(self):
        self.client.force_authenticate(user=self.admin)

    def test_le_detail_nomme_chaque_echec(self):
        fiche(9401, 'Sans adresse A', 34000001)
        fiche(9402, 'Sans adresse B', 34000002)
        fiche(9403, 'Complet', 34000003, 'complet@gmail.com')
        # Le troisième a déjà son compte par le signal : le rattrapage ne
        # porte donc que sur les deux premiers, et les refuse tous les deux.
        r = self.client.post('/api/v1/profs/generer-comptes/')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['created'], 0)
        self.assertEqual(len(r.data['echecs']), 2, r.data)
        self.assertTrue(all('ourriel' in e['raison'] for e in r.data['echecs']),
                        r.data['echecs'])
        self.assertIn('échec', r.data['detail'])

    def test_un_rattrapage_qui_reussit_le_dit_aussi(self):
        p = fiche(9404, 'A rattraper', 34000004)
        # `update()` ne déclenche aucun signal : on simule la fiche complétée
        # après coup, qui est exactement le cas des vingt-sept.
        Prof.objects.filter(pk=p.pk).update(email='rattrape@gmail.com')
        r = self.client.post('/api/v1/profs/generer-comptes/')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['created'], 1, r.data)
        self.assertEqual(r.data['echecs'], [])
        p.refresh_from_db()
        self.assertEqual(p.user.username, '34000004')
