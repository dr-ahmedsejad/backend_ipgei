"""
Tests pour Vacation et Paiement (taux historique, calcul montant).

Couvre :
  - Vacation.save() auto-calcule taux_paiement depuis Paiement.get_taux_at(type, date)
  - Vacation.save() preserve taux explicitement fourni
  - Vacation.save() sans type -> taux=0
  - Vacation.montant : duree * taux arrondi 2 decimales
  - Vacation.departements (M2M) creation
  - Paiement.get_taux_at : changement de taux dans le temps
"""
from datetime import date
from decimal import Decimal
import pytest

from apps.parametres.models import Paiement
from tests.factories.vacation import VacationFactory, ProfVacataireFactory
from tests.factories.parametres import InstitutionFactory, SeanceFactory, PaiementFactory
from tests.factories.em import EMLegacyFactory, DepartementAnnuelFactory


# ── Paiement.get_taux_at ───────────────────────────────────────────────────────
class TestPaiementGetTauxAt:

    def test_un_paiement_renvoie_son_taux(self, db):
        Paiement.objects.create(type='CM', taux=500.0, date_debut=date(2024, 9, 1))
        assert Paiement.get_taux_at('CM', date(2025, 1, 1)) == 500.0

    def test_aucun_paiement_renvoie_zero(self, db):
        assert Paiement.get_taux_at('CM', date(2025, 1, 1)) == 0.0

    def test_date_anterieure_au_premier_paiement_renvoie_zero(self, db):
        Paiement.objects.create(type='CM', taux=500.0, date_debut=date(2024, 9, 1))
        assert Paiement.get_taux_at('CM', date(2024, 1, 1)) == 0.0

    def test_changement_de_taux_garde_la_bonne_periode(self, db):
        """2 paiements (avant/apres reforme tarifaire) -> doit retourner le bon."""
        Paiement.objects.create(type='CM', taux=400.0, date_debut=date(2023, 9, 1))
        Paiement.objects.create(type='CM', taux=600.0, date_debut=date(2025, 1, 1))

        # Date < reforme : ancien taux
        assert Paiement.get_taux_at('CM', date(2024, 6, 1)) == 400.0
        # Date >= reforme : nouveau taux
        assert Paiement.get_taux_at('CM', date(2025, 6, 1)) == 600.0
        # Date pile sur la reforme : nouveau taux
        assert Paiement.get_taux_at('CM', date(2025, 1, 1)) == 600.0


# ── Vacation.save : auto-calcul du taux ────────────────────────────────────────
class TestVacationSaveAutoTaux:

    def test_taux_calcule_si_absent_et_type_present(self, db):
        from datetime import date
        institution = InstitutionFactory()
        Paiement.objects.create(type='CM', taux=500.0, date_debut=date(2024, 9, 1))

        seance_cm = SeanceFactory(type_seance='CM')
        prof  = ProfVacataireFactory()
        em    = EMLegacyFactory(institution=institution)
        vac   = VacationFactory.build(
            prof=prof, type=seance_cm, em=em, institution=institution,
            duree=2.0, date=date(2025, 5, 10), taux_paiement=0.0,
        )
        vac.save()

        assert vac.taux_paiement == 500.0

    def test_taux_explicite_preserve(self, db):
        from datetime import date
        institution = InstitutionFactory()
        Paiement.objects.create(type='CM', taux=500.0, date_debut=date(2024, 9, 1))
        seance_cm = SeanceFactory(type_seance='CM')
        prof  = ProfVacataireFactory()
        em    = EMLegacyFactory(institution=institution)

        # taux explicite = 999 -> doit etre preserve (pas ecrase a 500)
        vac = VacationFactory.build(
            prof=prof, type=seance_cm, em=em, institution=institution,
            duree=2.0, date=date(2025, 5, 10), taux_paiement=999.0,
        )
        vac.save()
        assert vac.taux_paiement == 999.0

    def test_pas_de_type_pas_de_taux(self, db):
        """Vacation sans type Seance -> save() ne calcule pas, taux reste 0."""
        from datetime import date
        institution = InstitutionFactory()
        prof  = ProfVacataireFactory()
        em    = EMLegacyFactory(institution=institution)

        vac = VacationFactory.build(
            prof=prof, type=None, em=em, institution=institution,
            duree=1.5, date=date(2025, 5, 10), taux_paiement=0.0,
        )
        vac.save()
        assert vac.taux_paiement == 0.0

    def test_taux_change_selon_date_vacation(self, db):
        """Save() utilise la date de la vacation pour calculer le bon taux."""
        from datetime import date
        institution = InstitutionFactory()
        Paiement.objects.create(type='CM', taux=400.0, date_debut=date(2023, 9, 1))
        Paiement.objects.create(type='CM', taux=600.0, date_debut=date(2025, 1, 1))

        seance_cm = SeanceFactory(type_seance='CM')
        prof  = ProfVacataireFactory()
        em    = EMLegacyFactory(institution=institution)

        v_anc = VacationFactory.build(
            prof=prof, type=seance_cm, em=em, institution=institution,
            duree=1.0, date=date(2024, 6, 1), taux_paiement=0.0,
        )
        v_anc.save()
        assert v_anc.taux_paiement == 400.0

        v_new = VacationFactory.build(
            prof=prof, type=seance_cm, em=em, institution=institution,
            duree=1.0, date=date(2025, 6, 1), taux_paiement=0.0,
        )
        v_new.save()
        assert v_new.taux_paiement == 600.0


# ── Vacation.montant ───────────────────────────────────────────────────────────
class TestVacationMontant:

    def test_montant_simple(self, db):
        institution = InstitutionFactory()
        v = VacationFactory(institution=institution, duree=2.0, taux_paiement=400.0)
        assert v.montant == 800.0

    def test_montant_demi_heure(self, db):
        institution = InstitutionFactory()
        v = VacationFactory(institution=institution, duree=1.5, taux_paiement=333.33)
        # 1.5 * 333.33 = 499.995 -> arrondi 500.00 (round half away from zero)
        assert v.montant == 500.00

    def test_montant_zero_si_taux_zero(self, db):
        institution = InstitutionFactory()
        v = VacationFactory(institution=institution, duree=2.0, taux_paiement=0.0)
        assert v.montant == 0.0


# ── Vacation.departements (M2M) ────────────────────────────────────────────────
class TestVacationDepartements:

    def test_aucun_departement_par_defaut(self, db):
        institution = InstitutionFactory()
        v = VacationFactory(institution=institution)
        assert v.departements.count() == 0

    def test_ajout_departement_apres_save(self, db):
        institution = InstitutionFactory()
        v = VacationFactory(institution=institution)
        d1 = DepartementAnnuelFactory(institution=institution)
        d2 = DepartementAnnuelFactory(institution=institution, nom='TEST_DEP_2')

        v.departements.set([d1, d2])
        assert v.departements.count() == 2
        assert d1 in v.departements.all()
