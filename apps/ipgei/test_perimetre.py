"""
Test — périmètre disciplinaire des chefs de département et coordinateurs.

Ce que le périmètre garantit :
  1. il ne restreint QUE le rôle `chef_departement` — les autres passent ;
  2. un responsable sans périmètre ne voit rien, plutôt que tout ;
  3. un chef de département voit toutes les matières de son département ;
  4. un coordinateur ne voit que les matières qu'on lui a confiées ;
  5. les deux se cumulent ;
  6. l'écriture est gardée : une séance sur une matière hors périmètre est
     refusée même si l'on connaît son identifiant ;
  7. le périmètre se traduit en EM du socle pour les réclamations.

Base SQLite en mémoire (siga.settings.test).
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.ipgei.models import Matiere, NiveauIPGEI, PerimetreResponsable
from apps.ipgei.perimetre import ems_autorises, matieres_autorisees
from apps.ipgei.views import MatiereViewSet
from apps.parametres.models import Institution, Niveau

User = get_user_model()


class PerimetreTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.niveau_socle = Niveau.objects.create(niveau='MPSI')
        cls.niveau = NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI',
                                                libelle='MPSI', rang=1)

        def matiere(code, departement):
            return Matiere.objects.create(
                institution=cls.inst, code=code, intitule=code, code_semestre='S1',
                niveau_ref=cls.niveau, coefficient=1, departement=departement)

        cls.math = matiere('MATH', 'ST')
        cls.phys = matiere('PHYS', 'ST')
        cls.angl = matiere('ANGL', 'LANGUES')
        cls.eps  = matiere('EPS',  '')          # sans département

        def compte(username, role):
            return User.objects.create_user(
                username=username, email=f'{username}@t.l', password='Xk93!plqz72', role=role)

        cls.admin        = compte('adm',  'admin')
        cls.scolarite    = compte('sco',  'scolarite')
        cls.chef_st      = compte('st',   'chef_departement')
        cls.chef_langues = compte('lang', 'chef_departement')
        cls.coord_math   = compte('cm',   'chef_departement')
        cls.sans_perim   = compte('vide', 'chef_departement')

        PerimetreResponsable.objects.create(utilisateur=cls.chef_st, departement='ST')
        PerimetreResponsable.objects.create(utilisateur=cls.chef_langues, departement='LANGUES')
        coord = PerimetreResponsable.objects.create(utilisateur=cls.coord_math)
        coord.matieres.add(cls.math)

    # ── la fonction de périmètre ─────────────────────────────────────────────
    def test_les_autres_roles_ne_sont_pas_restreints(self):
        self.assertIsNone(matieres_autorisees(self.admin))
        self.assertIsNone(matieres_autorisees(self.scolarite))

    def test_un_responsable_sans_perimetre_ne_voit_rien(self):
        self.assertEqual(matieres_autorisees(self.sans_perim), set())

    def test_le_chef_de_departement_voit_toutes_ses_matieres(self):
        self.assertEqual(matieres_autorisees(self.chef_st), {self.math.pk, self.phys.pk})
        self.assertEqual(matieres_autorisees(self.chef_langues), {self.angl.pk})

    def test_le_coordinateur_ne_voit_que_les_matieres_confiees(self):
        self.assertEqual(matieres_autorisees(self.coord_math), {self.math.pk})

    def test_departement_et_matieres_confiees_se_cumulent(self):
        perimetre = self.chef_langues.perimetre_ipgei
        perimetre.matieres.add(self.eps)        # l'EPS lui est confiée en plus
        self.assertEqual(matieres_autorisees(self.chef_langues), {self.angl.pk, self.eps.pk})

    def test_une_matiere_sans_departement_n_appartient_a_aucun_chef(self):
        self.assertNotIn(self.eps.pk, matieres_autorisees(self.chef_st))
        self.assertNotIn(self.eps.pk, matieres_autorisees(self.chef_langues))

    # ── le queryset des matières ─────────────────────────────────────────────
    def _matieres_vues_par(self, user):
        # Hors routeur, le ViewSet n'a pas d'action_map : on lui donne celle de
        # la liste, faute de quoi initialize_request echoue.
        vue = MatiereViewSet(action_map={'get': 'list'})
        requete = APIRequestFactory().get('/api/v1/ipgei/matieres/')
        force_authenticate(requete, user=user)
        vue.request = vue.initialize_request(requete)
        vue.action = 'list'
        return set(vue.get_queryset().values_list('code', flat=True))

    def test_le_viewset_des_matieres_applique_le_perimetre(self):
        self.assertEqual(self._matieres_vues_par(self.chef_st), {'MATH', 'PHYS'})
        self.assertEqual(self._matieres_vues_par(self.coord_math), {'MATH'})
        self.assertEqual(self._matieres_vues_par(self.sans_perim), set())
        self.assertEqual(self._matieres_vues_par(self.scolarite),
                         {'MATH', 'PHYS', 'ANGL', 'EPS'}, 'la scolarite voit tout')

    # ── la garde en écriture ─────────────────────────────────────────────────
    def test_ecrire_hors_perimetre_est_refuse_meme_avec_l_identifiant(self):
        from apps.ipgei.perimetre import PerimetreMatiereMixin

        class Faux(PerimetreMatiereMixin):
            perimetre_champ = 'matiere'

        class Serialiseur:                       # juste ce que la garde lit
            instance = None
            validated_data = {'matiere': self.angl}

        garde = Faux()
        requete = APIRequestFactory().post('/')
        force_authenticate(requete, user=self.chef_st)
        from rest_framework.request import Request
        garde.request = Request(requete)
        garde.request.user = self.chef_st

        with self.assertRaises(ValidationError):
            garde._verifier_perimetre(Serialiseur())

        Serialiseur.validated_data = {'matiere': self.math}
        garde._verifier_perimetre(Serialiseur())      # la sienne : passe

    # ── la traduction en EM du socle ─────────────────────────────────────────
    def test_le_perimetre_se_traduit_en_em_pour_les_reclamations(self):
        # Le miroir est AUTOMATIQUE : creer une matiere cree son EM (signal
        # post_save). Le test s'appuie donc sur les miroirs reels, il ne les
        # fabrique pas — c'est ce chemin-la que les reclamations empruntent.
        for m in (self.math, self.phys, self.angl):
            m.refresh_from_db()
            self.assertIsNotNone(m.em_miroir_id, f'{m.code} doit avoir son EM miroir')

        self.assertEqual(ems_autorises(self.chef_st),
                         {self.math.em_miroir_id, self.phys.em_miroir_id})
        self.assertEqual(ems_autorises(self.coord_math), {self.math.em_miroir_id})
        self.assertNotIn(self.angl.em_miroir_id, ems_autorises(self.chef_st),
                         "l'anglais n'est pas du departement ST")
        self.assertIsNone(ems_autorises(self.admin))

    # ── les réclamations ─────────────────────────────────────────────────────
    def test_le_chef_ne_voit_que_les_reclamations_de_ses_matieres(self):
        """
        L'écran /dashboard/reclamations lit ReclamationAdminViewSet. Un chef doit
        y trouver les réclamations portant sur SES matières, et rien d'autre ;
        la scolarité, elle, continue de tout voir.
        """
        from apps.absence.models import Etudiant, Presence
        from apps.reclamations.models import Reclamation
        from apps.reclamations.views import ReclamationAdminViewSet
        from apps.suivi.models import Suivie

        for m in (self.math, self.angl):
            m.refresh_from_db()

        etudiant = Etudiant.objects.create(matricule='900001', nom='Etudiant Test')

        def reclamation(matiere, motif):
            suivi = Suivie.objects.create(institution=self.inst, em=matiere.em_miroir,
                                          annee_universitaire='2025-2026')
            presence = Presence.objects.create(suivi=suivi, etudiant=etudiant)
            return Reclamation.objects.create(etudiant=etudiant, motif=motif,
                                              type_reclamation='absence', presence=presence)

        sur_math = reclamation(self.math, 'absence en maths contestee')
        sur_angl = reclamation(self.angl, 'absence en anglais contestee')

        def vues_par(user):
            vue = ReclamationAdminViewSet(action_map={'get': 'list'})
            requete = APIRequestFactory().get('/api/v1/reclamations/')
            force_authenticate(requete, user=user)
            vue.request = vue.initialize_request(requete)
            vue.action = 'list'
            return set(vue.get_queryset().values_list('pk', flat=True))

        self.assertEqual(vues_par(self.chef_st), {sur_math.pk},
                         'le chef de ST ne voit que la reclamation de maths')
        self.assertEqual(vues_par(self.chef_langues), {sur_angl.pk})
        self.assertEqual(vues_par(self.sans_perim), set())
        self.assertEqual(vues_par(self.scolarite), {sur_math.pk, sur_angl.pk},
                         'la scolarite voit tout, comme avant')
