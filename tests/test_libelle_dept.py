"""
Test — le libellé d'une classe, partout où il s'affiche.

Les fiches annonçaient « IPGEI - MPSI - MPSI A » là où « MPSI A » dit la même
chose. Deux redites, et toutes deux commandées par les DONNÉES — un
établissement qui se diversifierait les verrait revenir d'elles-mêmes :

  * le CODE DE FILIÈRE ne distingue rien quand il n'y en a qu'une ;
  * le NIVEAU est déjà dans le nom, à l'IPGEI où les classes se nomment
    d'après lui.

C'est la règle que `lib/format-depts.ts` applique côté écran depuis longtemps,
et que les fiches — écran ET PDF, qui lisent le même libellé — ignoraient.

Le test porte sur `core.libelle_dept`, seul endroit où la règle est écrite. Elle
vivait en deux copies littérales, `apps/suivi/views.py` et
`apps/avancement/views.py` : corriger l'une avait laissé l'autre dire encore
« IPGEI - MPSI - MPSI A ». Un seul test vaut désormais pour les deux familles
d'écrans.

Base SQLite en mémoire (siga.settings.test).
"""
from django.db import connection
from django.test import TestCase

from apps.departement.models import Departement
from apps.parametres.models import Institution, Jour, Niveau, Seance, Semestre
from apps.scolarite.models import Filiere
from apps.suivi.models import SuiviePointage, SuiviePointageDepartement
from core.libelle_dept import format_depts_compact as formater


class LibelleClasseTest(TestCase):
    @classmethod
    def setUpClass(cls):
        existantes = set(connection.introspection.table_names())
        if SuiviePointageDepartement._meta.db_table not in existantes:
            with connection.schema_editor() as se:
                se.create_model(SuiviePointageDepartement)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.inst   = Institution.objects.create(acronyme='IPGEI', nom='Test',
                                                est_principale=True)
        cls.mpsi   = Niveau.objects.create(niveau='MPSI')
        cls.ipgei  = Filiere.objects.create(code='IPGEI', intitule_fr='Prépa',
                                            institution=cls.inst)
        cls.cm     = Seance.objects.create(type_seance='CM')
        cls.jour   = Jour.objects.create(jour='Lundi')
        Semestre.objects.create(code_semestre='S1', semestre='S1',
                                niveau_semestre=cls.mpsi, type_semestre='I')

    def _classe(self, nom, groupe='A', niveau=None, filiere=None):
        return Departement.objects.create(
            nom=nom, groupe=groupe, institution=self.inst,
            niveau=niveau or self.mpsi, filiere=filiere or self.ipgei,
            annee_universitaire='2025-2026')

    def _pointage(self, *classes):
        p = SuiviePointage.objects.create(
            institution=self.inst, annee_universitaire='2025-2026',
            numero_semaine=1, type_semestre='I', type_seance_fk=self.cm,
            jour_fk=self.jour)
        p.departements.set(classes)
        return p

    # ── Une seule filière : elle ne distingue rien ───────────────────────────
    def test_une_classe_se_nomme_par_son_nom_seul(self):
        """Le cas de l'IPGEI : une filière, et le niveau déjà dans le nom."""
        self.assertEqual(formater(self._pointage(self._classe('MPSI A'))), 'MPSI A')

    def test_le_niveau_tombe_aussi_quand_il_est_colle_au_nom(self):
        self.assertEqual(formater(self._pointage(self._classe('MPSI-A'))), 'MPSI-A')

    def test_un_nom_qui_ne_porte_pas_le_niveau_le_garde(self):
        """
        L'allègement n'efface pas une information : « Groupe 1 » ne dit pas de
        quel niveau il est, le niveau reste donc affiché.
        """
        self.assertEqual(formater(self._pointage(self._classe('Groupe 1'))),
                         'MPSI - Groupe 1')

    def test_un_sous_groupe_garde_son_nom_complet(self):
        self.assertEqual(formater(self._pointage(self._classe('MPSI A — A1'))),
                         'MPSI A — A1')

    # ── Plusieurs filières : le code redevient utile ─────────────────────────
    def test_le_code_de_filiere_revient_des_qu_il_y_en_a_deux(self):
        """
        L'allègement est commandé par les données. Un établissement qui ouvre
        une seconde filière retrouve le préfixe sans qu'on touche au code.
        """
        Filiere.objects.create(code='LP', intitule_fr='Licence',
                               institution=self.inst)
        self.assertEqual(formater(self._pointage(self._classe('MPSI A'))),
                         'IPGEI - MPSI A')

    # ── Le regroupement d'origine n'a pas bougé ──────────────────────────────
    def test_deux_classes_du_meme_niveau_restent_regroupees(self):
        """
        La règle historique : même filière ET même niveau → un seul bloc avec
        les groupes entre parenthèses. Elle survit à l'allègement.
        """
        libelle = formater(self._pointage(self._classe('MPSI A', 'A'),
                                          self._classe('MPSI B', 'B')))
        self.assertEqual(libelle, 'MPSI (A / B)')

    def test_deux_niveaux_differents_restent_separes(self):
        mp = Niveau.objects.create(niveau='MP')
        libelle = formater(self._pointage(self._classe('MPSI A'),
                                          self._classe('MP A', niveau=mp)))
        self.assertEqual(sorted(libelle.split(' / ')), ['MP A', 'MPSI A'])

    def test_sans_aucun_departement_le_libelle_est_vide(self):
        self.assertEqual(formater(self._pointage()), '')
