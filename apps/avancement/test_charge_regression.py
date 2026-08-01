"""
Non-régression des fonctions de charge permanente après l'optimisation N+1
(_compute_charge_permanents et _compute_charge_permanents_mensuel sont passés
d'un fetch par prof à un préchargement groupé + filtrage en mémoire).

Ces tests verrouillent la SÉMANTIQUE attendue, indépendamment de l'implémentation :
  - calcul eq_CM (CM + (TD+TP+PR)*2/3 + surveillance/encadrement/mission pondérés
    + charge institution) ;
  - source = SuiviePointage(commentaire='Fait') + Vacation + ChargeInstitution ;
  - fallback durée : duree_creneau sinon creneau_fk.duree ;
  - exclusion des types non réglementaires (vacataire) ;
  - version mensuelle : scoping institution principale, filtrage par date,
    exclusion des dates NULL, heures supp incrémentales (Σ mois = annuel).

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date

from django.db import connection
from django.test import TestCase

from apps.parametres.models import Institution, Seance, Creneau, Paiement
from apps.prof.models import Prof, ProfTypeHistory
from apps.suivi.models import SuiviePointage, ChargeInstitution
from apps.vacation.models import Vacation
from apps.avancement.views import (
    _compute_charge_permanents,
    _compute_charge_permanents_mensuel,
)

ANNEE = '2025-2026'


def _taux_fixtures():
    """Barème : taux_CM=1000 → factor_surv=0.5, factor_enc=0.8, factor_miss=0.2."""
    Paiement.objects.create(type='CM',           taux=1000, date_debut=date(2025, 1, 1))
    Paiement.objects.create(type='Surveillance', taux=500,  date_debut=date(2025, 1, 1))
    Paiement.objects.create(type='Encadrement',  taux=800,  date_debut=date(2025, 1, 1))
    Paiement.objects.create(type='Mission',      taux=200,  date_debut=date(2025, 1, 1))


class _SchemaGuard(TestCase):
    @classmethod
    def setUpClass(cls):
        # ProfTypeHistory peut ne pas être créé par syncdb (--no-migrations).
        # create_model plante la transaction PG si la table existe deja (creee par
        # la fixture session de tests/conftest.py) : verifier l'existence AVANT
        # d'entrer dans le schema_editor est vendor-neutre (sqlite ET postgresql).
        if ProfTypeHistory._meta.db_table not in connection.introspection.table_names():
            with connection.schema_editor() as se:
                se.create_model(ProfTypeHistory)
        super().setUpClass()


class ChargePermanentsAnnuelTest(_SchemaGuard):
    @classmethod
    def setUpTestData(cls):
        _taux_fixtures()
        cls.inst = Institution.objects.create(acronyme='TST', nom='Test', est_principale=True)
        cls.cm   = Seance.objects.create(type_seance='CM')
        cls.td   = Seance.objects.create(type_seance='TD')
        cls.surv = Seance.objects.create(type_seance='Surveillance')
        cls.cr   = Creneau.objects.create(creneau='C1', duree=1.5)

        cls.prof = Prof.objects.create(NNI=900001, nom='Permanent', type='permanent', charge=10)
        # Un vacataire NE DOIT PAS apparaître (hors charge réglementaire).
        cls.vac_prof = Prof.objects.create(NNI=900002, nom='Vacataire', type='vacataire', charge=0)

        def sp(prof, seance, commentaire, duree_creneau=None, creneau_fk=None):
            return SuiviePointage.objects.create(
                prof=prof, institution=cls.inst, annee_universitaire=ANNEE,
                commentaire=commentaire, type_seance_fk=seance,
                duree_creneau=duree_creneau, creneau_fk=creneau_fk,
                date_suivie=date(2026, 1, 10),
            )

        # Permanent : 2 CM 'Fait' (1.5 chacun) + 1 CM via creneau_fk (fallback 1.5)
        #             + 1 TD 'Fait' (3.0) + 1 CM 'Non fait' (ignoré).
        sp(cls.prof, cls.cm, 'Fait', duree_creneau=1.5)
        sp(cls.prof, cls.cm, 'Fait', duree_creneau=1.5)
        sp(cls.prof, cls.cm, 'Fait', creneau_fk=cls.cr)        # fallback duree=1.5
        sp(cls.prof, cls.td, 'Fait', duree_creneau=3.0)
        sp(cls.prof, cls.cm, 'Non fait', duree_creneau=1.5)    # ignoré

        # Vacation : 1 CM (2.0) + 1 Surveillance (4.0).
        Vacation.objects.create(prof=cls.prof, type=cls.cm,  duree=2.0, date=date(2026, 1, 11),
                                annee_univ=ANNEE, institution=cls.inst)
        Vacation.objects.create(prof=cls.prof, type=cls.surv, duree=4.0, date=date(2026, 1, 11),
                                annee_univ=ANNEE, institution=cls.inst)

        # Charge institution : 5 eq_CM.
        ChargeInstitution.objects.create(prof=cls.prof, institution=cls.inst,
                                         charge_cm=5, annee_universitaire=ANNEE)

        # Activité pour le vacataire (doit rester ignorée).
        sp(cls.vac_prof, cls.cm, 'Fait', duree_creneau=9.0)

    def test_vacataire_exclu(self):
        data, _ = _compute_charge_permanents(ANNEE)
        noms = {row['prof_nom'] for row in data}
        self.assertNotIn('Vacataire', noms)
        self.assertIn('Permanent', noms)

    def test_totaux_permanent(self):
        data, globaux = _compute_charge_permanents(ANNEE)
        row = next(r for r in data if r['prof_nom'] == 'Permanent')
        t = row['totaux']
        # CM = 1.5 + 1.5 + 1.5 (SP) + 2.0 (vacation) = 6.5 ; TD = 3.0
        self.assertAlmostEqual(t['CM_total'], 6.5, places=2)
        self.assertAlmostEqual(t['TD_total'], 3.0, places=2)
        self.assertAlmostEqual(t['Surveillance_total'], 4.0, places=2)
        self.assertAlmostEqual(t['total_charge_institution_cm'], 5.0, places=2)
        self.assertEqual(t['charges_institution'], {'TST': 5})
        # eq_base = 6.5 + 3.0*2/3 = 8.5 ; eq_CM = 8.5 + 4.0*0.5 + 5 = 15.5
        self.assertAlmostEqual(t['total_eq_CM'], 15.5, places=2)
        # difference = 15.5 - (charge 10 - decharge 0) = 5.5
        self.assertAlmostEqual(row['difference'], 5.5, places=2)

    def test_totaux_globaux(self):
        data, globaux = _compute_charge_permanents(ANNEE)
        self.assertAlmostEqual(globaux['CM_total'], 6.5, places=2)
        self.assertAlmostEqual(globaux['total_eq_CM'], 15.5, places=2)
        # heures supp = somme des différences positives = 5.5
        self.assertAlmostEqual(globaux['total_heures_supp'], 5.5, places=2)


class ChargePermanentsMensuelTest(_SchemaGuard):
    @classmethod
    def setUpTestData(cls):
        _taux_fixtures()
        cls.inst  = Institution.objects.create(acronyme='TST', nom='Principale', est_principale=True)
        cls.autre = Institution.objects.create(acronyme='ESP', nom='Autre', est_principale=False)
        cls.cm    = Seance.objects.create(type_seance='CM')

        # charge=1 → dépassée facilement → heures supp.
        cls.prof = Prof.objects.create(NNI=900010, nom='Perm', type='permanent', charge=1)

        def sp(institution, duree, jour, commentaire='Fait'):
            return SuiviePointage.objects.create(
                prof=cls.prof, institution=institution, annee_universitaire=ANNEE,
                commentaire=commentaire, type_seance_fk=cls.cm,
                duree_creneau=duree, date_suivie=jour,
            )

        # Institution principale :
        sp(cls.inst, 3.0, date(2026, 1, 15))   # dans le mois M (janvier) → +3 CM
        sp(cls.inst, 2.0, date(2025, 12, 10))  # mois précédent (cumul avant)
        # Date NULL → doit être exclue (équivalence SQL : NULL exclu par <= end_date).
        SuiviePointage.objects.create(
            prof=cls.prof, institution=cls.inst, annee_universitaire=ANNEE,
            commentaire='Fait', type_seance_fk=cls.cm, duree_creneau=99.0, date_suivie=None,
        )
        # Autre institution en janvier → doit être exclue (scoping principale).
        sp(cls.autre, 50.0, date(2026, 1, 20))

    def test_mois_janvier(self):
        data, globaux, acronyme = _compute_charge_permanents_mensuel(ANNEE, 2026, 1)
        self.assertEqual(acronyme, 'TST')
        self.assertEqual(len(data), 1, "Un seul prof attendu (autre institution exclue).")
        row = data[0]
        # eq du mois janvier = 3.0 (seul le CM du 15/01 à l'institution principale).
        self.assertAlmostEqual(row['eq_CM_mois'], 3.0, places=2)
        # cumul_apres = 5.0 (3+2), cumul_avant = 2.0 ; charge_nette = 1
        # hs_apres = 4, hs_avant = 1 → hs_mois = 3.0
        self.assertAlmostEqual(row['heures_supp_mois'], 3.0, places=2)
        # montant = 3.0 * taux_CM(1000) = 3000
        self.assertAlmostEqual(row['montant_a_payer'], 3000.0, places=2)
        self.assertAlmostEqual(globaux['total_heures_supp'], 3.0, places=2)
        self.assertAlmostEqual(globaux['montant_global'], 3000.0, places=2)

    def test_date_null_et_autre_institution_exclues(self):
        # Le mois ne doit PAS contenir les 50h de l'autre institution ni les 99h NULL.
        data, _globaux, _acro = _compute_charge_permanents_mensuel(ANNEE, 2026, 1)
        row = data[0]
        # eq_CM_mois resterait absurde (≫ 3) si l'une des deux fuitait.
        self.assertLess(row['eq_CM_mois'], 10.0)
