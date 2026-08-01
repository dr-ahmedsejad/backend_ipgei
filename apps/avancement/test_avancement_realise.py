"""
Test — avancement enseignant : la RÉALISATION ne compte que les séances
effectivement pointées 'Fait' (source SuiviePointage), pas toutes les lignes
planifiées. Régression corrigée : le résumé lisait la table `Suivie` (toutes
lignes) et sur-comptait, en contradiction avec le détail séance/séance et le
calcul de charge (qui utilisent SuiviePointage commentaire='Fait').

Base SQLite en mémoire (siga.settings.test).
"""
from django.db import connection
from django.test import TestCase

from apps.parametres.models import Institution, Niveau, Semestre, Seance
from apps.scolarite.models import Filiere
from apps.departement.models import Departement
from apps.em.models import EM
from apps.prof.models import Prof, ProfTypeHistory
from apps.suivi.models import SuiviePointage
from apps.avancement.views import _compute_avancement_prof


class AvancementRealiseTest(TestCase):
    @classmethod
    def setUpClass(cls):
        # create_model plante la transaction PG si la table existe deja (creee par
        # la fixture session de tests/conftest.py) : verifier l'existence AVANT
        # d'entrer dans le schema_editor est vendor-neutre (sqlite ET postgresql).
        if ProfTypeHistory._meta.db_table not in connection.introspection.table_names():
            with connection.schema_editor() as se:
                se.create_model(ProfTypeHistory)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Test', est_principale=True)
        cls.niveau = Niveau.objects.create(niveau='L1')
        cls.fil = Filiere.objects.create(code='LP', intitule_fr='LP', institution=cls.inst)
        cls.dept = Departement.objects.create(nom='G1', institution=cls.inst, filiere=cls.fil, niveau=cls.niveau)
        cls.sem = Semestre.objects.create(code_semestre='S1', semestre='S1', niveau_semestre=cls.niveau, type_semestre='I')
        cls.cm = Seance.objects.create(type_seance='CM')
        cls.em = EM.objects.create(code_em='X1', intitule='Cours X', departement=cls.dept,
                                   semestre=cls.sem, institution=cls.inst, CM=10)
        cls.prof = Prof.objects.create(NNI=999001, nom='P', type='permanent')

        def pointage(commentaire):
            return SuiviePointage.objects.create(
                prof=cls.prof, em=cls.em, institution=cls.inst,
                annee_universitaire='2025-2026', numero_semaine=1, type_semestre='I',
                type_seance_fk=cls.cm, duree_creneau=1.5, commentaire=commentaire,
            )
        # 3 séances faites + 2 non faites → réalisé attendu = 3 × 1.5 = 4.5 h
        for _ in range(3):
            pointage('Fait')
        for _ in range(2):
            pointage('Non fait')

    def test_realise_compte_uniquement_les_faites(self):
        items, totaux = _compute_avancement_prof('2025-2026', self.prof.pk)
        self.assertEqual(len(items), 1, "L'EM doit apparaître.")
        row = items[0]
        self.assertEqual(row['real_CM'], 4.5,
                         f"Réalisé attendu 4.5h (3 'Fait'), obtenu {row['real_CM']} "
                         f"(si 7.5 -> compte aussi les non faites = bug).")
        self.assertEqual(row['plan_CM'], 10)
        self.assertEqual(row['pct_CM'], 45)   # 4.5 / 10
        self.assertEqual(totaux['CM'], 4.5)
