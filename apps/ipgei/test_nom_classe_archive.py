"""
Test — l'emploi du temps publié porte le nom ACTUEL de la classe.

La prise de vue recopie le nom de la classe au moment où elle est faite. Une
classe renommée — « MP MPA » corrigé en « MP A » — laissait donc les semaines
déjà publiées annoncer l'ancien nom, sur l'écran comme sur le PDF, alors qu'il
s'agit de la même classe. Le PDF l'affichait jusque dans son titre.

Ce que le test fige :
  1. la grille publiée d'une classe renommée porte le nouveau nom ;
  2. la version précise d'une prise de vue aussi ;
  3. l'emploi du temps d'un ENSEIGNANT nomme la classe à jour dans ses cases ;
  4. l'enseignant et la salle archivés, eux, ne bougent pas — ce sont les faits
     de la semaine, pas l'identité de la classe ;
  5. l'archive en base n'est pas réécrite : seule la lecture est rafraîchie.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.ipgei.models import (
    Classe, EmploiArchive, NiveauIPGEI, SemaineIPGEI, SemestreIPGEI,
)
from apps.parametres.models import Institution, Niveau

User = get_user_model()
ANNEE = '2026-2027'
GRILLE = '/api/v1/ipgei/archives-edt/grille/'


class NomClasseArchiveTest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        socle = Niveau.objects.create(niveau='MP')
        NiveauIPGEI.objects.create(institution=cls.inst, code='MP', libelle='MP', rang=2)
        cls.semestre = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S3', annee_universitaire=ANNEE,
            date_debut=date.today())
        cls.semaine = SemaineIPGEI.objects.create(
            semestre=cls.semestre, numero=1, date_debut=date.today(),
            date_fin=date.today() + timedelta(days=5), type_semaine='cours')
        cls.classe = Classe.objects.create(institution=cls.inst, niveau=socle,
                                           annee_universitaire=ANNEE, groupe='MPA')
        cls.admin = User.objects.create_user(username='adm_archive', email='a@t.l',
                                             password='Xk93!plqz72', role='admin')

    def _archive(self, version=1, **extra):
        donnees = dict(
            semaine=self.semaine, classe=self.classe, version=version,
            genere_le=date.today(), classe_nom=self.classe.nom,
            prof_ref=7, prof_nom='Ancien Enseignant', salle_nom='S1',
            matiere_code='MATH', jour_libelle='Lundi', creneau_libelle='08h00-09h30',
        )
        donnees.update(extra)
        return EmploiArchive.objects.create(**donnees)

    def setUp(self):
        self.client.force_authenticate(user=self.admin)

    def _renommer(self):
        """La correction du libellé, telle que la scolarité la fait à l'écran."""
        self.classe.groupe = 'A'
        self.classe.save()
        self.assertEqual(Classe.objects.get(pk=self.classe.pk).nom, 'MP A')

    def _noms(self, **params):
        reponse = self.client.get(GRILLE, {'semaine': self.semaine.pk, **params})
        self.assertEqual(reponse.status_code, 200, reponse.data)
        return [l['classe_nom'] for l in reponse.data]

    # ── Le nom suit la classe ───────────────────────────────────────────────
    def test_la_grille_publiee_porte_le_nom_actuel(self):
        self._archive()
        self._renommer()
        self.assertEqual(self._noms(classe=self.classe.pk), ['MP A'])

    def test_une_version_precise_aussi(self):
        self._archive(version=1)
        self._archive(version=2)
        self._renommer()
        self.assertEqual(self._noms(classe=self.classe.pk, version=2), ['MP A'])

    def test_l_emploi_d_un_enseignant_nomme_la_classe_a_jour(self):
        self._archive()
        self._renommer()
        self.assertEqual(self._noms(prof=7), ['MP A'])

    # ── Ce qui ne doit PAS bouger ───────────────────────────────────────────
    def test_l_enseignant_et_la_salle_archives_restent(self):
        self._archive()
        self._renommer()
        reponse = self.client.get(GRILLE, {'semaine': self.semaine.pk,
                                           'classe': self.classe.pk})
        ligne = reponse.data[0]
        self.assertEqual(ligne['prof_nom'], 'Ancien Enseignant')
        self.assertEqual(ligne['salle_nom'], 'S1')

    def test_l_archive_en_base_n_est_pas_reecrite(self):
        archive = self._archive()
        self._renommer()
        self._noms(classe=self.classe.pk)
        archive.refresh_from_db()
        self.assertEqual(archive.classe_nom, 'MP MPA')
