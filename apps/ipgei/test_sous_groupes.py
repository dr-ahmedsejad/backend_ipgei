"""
Test — on ne supprime pas un sous-groupe qui tient encore quelqu'un.

Les règles de suppression de la base sont silencieuses, et dans deux sens
opposés :

  - `InscriptionIPGEI.sous_groupe` est en SET_NULL : les étudiants seraient
    DÉTACHÉS, sans disparaître ni prévenir. On ne s'en apercevrait qu'au
    prochain appel de TP, devant une liste vide ;
  - `SeanceReelle.sous_groupe` et `SeanceType.sous_groupe` sont en CASCADE :
    les séances seraient DÉTRUITES, y compris celles d'une semaine publiée.

Relevé du 12/10/2026 : les vingt-six sous-groupes de la base tiennent chacun 17
à 20 élèves, et certains portent des séances. La suppression n'est donc jamais
anodine — elle est refusée tant qu'il reste quelque chose, et le refus dit quoi
faire.

Le module RBAC est séparé de celui des classes : composer des TP et créer des
classes ne demandent pas la même main.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from rest_framework.test import APITestCase

from apps.ipgei.models import (
    Classe, InscriptionIPGEI, NiveauIPGEI, SeanceReelle, SeanceType,
    SemaineIPGEI, SemestreIPGEI, SousGroupeTP,
)
from apps.ipgei.views import SousGroupeTPViewSet
from apps.parametres.models import Creneau, Institution, Jour, Niveau, Seance
from apps.absence.models import Etudiant

User = get_user_model()
ANNEE = '2026-2027'
URL = '/api/v1/ipgei/sous-groupes/'


class SuppressionSousGroupeTest(APITestCase):
    """Supprimer un sous-groupe parcourt tout ce qui pointe vers lui.

    Le collecteur de Django passe donc par `suivi_pointage_departements`, table
    `managed = False` que rien ne crée sur la base de test — la suppression y
    échouait avant d'atteindre la règle qu'on mesure. `tests/conftest.py` la
    pose pour les tests du dossier `tests/` ; ici on fait de même.
    """

    @classmethod
    def setUpClass(cls):
        if 'suivi_pointage_departements' not in connection.introspection.table_names():
            with connection.cursor() as curseur:
                curseur.execute("""
                    CREATE TABLE IF NOT EXISTS suivi_pointage_departements (
                        id                INTEGER PRIMARY KEY AUTOINCREMENT,
                        suiviepointage_id BIGINT NOT NULL,
                        departement_id    BIGINT NOT NULL
                    )
                """)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        niveau = Niveau.objects.create(niveau='MPSI')
        NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI',
                                   libelle='MPSI', rang=1, actif=True)
        cls.classe = Classe.objects.create(institution=cls.inst, niveau=niveau,
                                           annee_universitaire=ANNEE, groupe='A')
        cls.semestre = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S1', annee_universitaire=ANNEE,
            date_debut=date(2026, 9, 14))
        cls.admin = User.objects.create_user(username='adm_sg', email='sg@t.l',
                                             password='Xk93!plqz72', role='admin')

    def setUp(self):
        self.client.force_authenticate(user=self.admin)
        self.sg = SousGroupeTP.objects.create(
            institution=self.inst, niveau=self.classe.niveau,
            annee_universitaire=ANNEE, classe=self.classe, groupe='A1')

    def supprimer(self):
        return self.client.delete(f'{URL}{self.sg.pk}/')

    def _inscrire(self, matricule):
        etudiant = Etudiant.objects.create(matricule=str(matricule), nom='Elève')
        return InscriptionIPGEI.objects.create(
            etudiant=etudiant, classe=self.classe, sous_groupe=self.sg,
            annee_universitaire=ANNEE)

    def _seance(self):
        semaine = SemaineIPGEI.objects.create(
            semestre=self.semestre, numero=1, type_semaine='cours',
            date_debut=date(2026, 9, 14), date_fin=date(2026, 9, 19))
        return SeanceReelle.objects.create(
            classe=self.classe, semaine=semaine, sous_groupe=self.sg,
            jour=Jour.objects.create(jour='Lundi'),
            creneau=Creneau.objects.create(creneau='08h00-09h30', ordre=1),
            type_seance=Seance.objects.create(type_seance='TP'))

    # ── Le refus ────────────────────────────────────────────────────────────
    def test_un_sous_groupe_vide_se_supprime(self):
        self.assertEqual(self.supprimer().status_code, 204)
        self.assertFalse(SousGroupeTP.objects.filter(pk=self.sg.pk).exists())

    def test_des_etudiants_retiennent_la_suppression(self):
        """SET_NULL les détacherait sans un mot : on refuse avant."""
        self._inscrire(1001)
        self._inscrire(1002)
        reponse = self.supprimer()
        self.assertEqual(reponse.status_code, 409, reponse.data)
        self.assertIn('2 étudiants', reponse.data['error'])
        self.assertIn('Affecter les étudiants', reponse.data['error'])
        self.assertTrue(SousGroupeTP.objects.filter(pk=self.sg.pk).exists())

    def test_les_inscriptions_ne_sont_pas_detachees_par_le_refus(self):
        """Le refus ne doit rien casser non plus."""
        inscription = self._inscrire(1003)
        self.supprimer()
        inscription.refresh_from_db()
        self.assertEqual(inscription.sous_groupe_id, self.sg.pk)

    def test_des_seances_retiennent_la_suppression(self):
        """CASCADE les détruirait — une semaine publiée comprise."""
        self._seance()
        reponse = self.supprimer()
        self.assertEqual(reponse.status_code, 409, reponse.data)
        self.assertIn('1 séance', reponse.data['error'])
        self.assertEqual(SeanceReelle.objects.count(), 1, 'rien ne doit partir')

    def test_le_refus_nomme_TOUT_ce_qui_retient(self):
        """Une seule tentative doit suffire à connaître tout le travail."""
        self._inscrire(1004)
        self._seance()
        erreur = self.supprimer().data['error']
        self.assertIn('1 étudiant', erreur)
        self.assertIn('1 séance', erreur)
        self.assertIn(self.sg.libelle, erreur)

    def test_un_patron_de_grille_retient_aussi(self):
        """La grille type est l'emploi du temps de toutes les semaines à venir."""
        from apps.ipgei.models import GrilleType
        grille = GrilleType.objects.create(classe=self.classe, type_semestre='I')
        SeanceType.objects.create(
            grille=grille, sous_groupe=self.sg,
            jour=Jour.objects.create(jour='Mardi'),
            creneau=Creneau.objects.create(creneau='09h45-11h15', ordre=2),
            type_seance=Seance.objects.create(type_seance='TP'))
        reponse = self.supprimer()
        self.assertEqual(reponse.status_code, 409, reponse.data)
        self.assertIn('grille type', reponse.data['error'])

    def test_vider_le_sous_groupe_rouvre_la_suppression(self):
        """Le refus est une étape, pas un mur."""
        inscription = self._inscrire(1005)
        self.assertEqual(self.supprimer().status_code, 409)
        inscription.sous_groupe = None
        inscription.save(update_fields=['sous_groupe'])
        self.assertEqual(self.supprimer().status_code, 204)


class ModuleSepareTest(APITestCase):
    def test_le_viewset_a_son_propre_module(self):
        """Composer des TP et créer des classes ne demandent pas la même main.

        La migration `0047` recopie les droits de `ipgei_classes` : séparer
        n'est pas reprendre, personne ne perd l'accès qu'il avait.
        """
        self.assertEqual(SousGroupeTPViewSet.required_module,
                         'ipgei_sous_groupes')
