"""
Tests de protection contre les suppressions accidentelles (2026-06-11).

Couvre :
  - P0 : supprimer une banque ne supprime PLUS les profs (SET_NULL).
  - P1 : un prof avec vacation / surveillance / charge est PROTÉGÉ (PROTECT).
  - P1bis : champ Prof.actif (archivage) — alternative à la suppression.

Runner : python -m pytest tests/test_protection_suppression.py
"""
from datetime import date as _date
from decimal import Decimal

import pytest
from django.db.models import ProtectedError

from apps.prof.models import Prof
from tests.factories.vacation import ProfVacataireFactory, VacationFactory
from tests.factories.parametres import InstitutionFactory


@pytest.fixture
def institution(db):
    return InstitutionFactory()


# ── P0 : banque → SET_NULL (ne détruit plus les profs) ────────────────────────

def test_supprimer_banque_ne_supprime_pas_prof(db):
    from apps.banque.models import Banque
    banque = Banque.objects.create(nom='Banque Test')
    prof = ProfVacataireFactory(banque=banque)

    banque.delete()

    prof.refresh_from_db()
    assert Prof.objects.filter(pk=prof.pk).exists(), 'Le prof ne doit pas être supprimé'
    assert prof.banque_id is None, 'La banque doit être mise à NULL (SET_NULL)'


# ── P1 : prof avec données de paie → PROTECT ──────────────────────────────────

def test_prof_avec_vacation_protege(db, institution):
    prof = ProfVacataireFactory()
    VacationFactory(prof=prof, institution=institution)

    with pytest.raises(ProtectedError):
        prof.delete()
    assert Prof.objects.filter(pk=prof.pk).exists()


def test_prof_avec_surveillance_protege(db, institution):
    from apps.vacation.models import Surveillance
    from tests.factories.em import DepartementAnnuelFactory
    prof = ProfVacataireFactory()
    Surveillance.objects.create(
        prof=prof, departement=DepartementAnnuelFactory(),
        duree=2.0, date=_date(2025, 10, 15),
        annee_univ='2025-2026', institution=institution,
    )

    with pytest.raises(ProtectedError):
        prof.delete()


def test_prof_avec_charge_protege(db):
    from apps.suivi.models import ChargeInstitution
    from tests.factories.parametres import InstitutionFactory as Inst
    prof = ProfVacataireFactory()
    ChargeInstitution.objects.create(
        prof=prof, institution=Inst(), charge_cm=10, annee_universitaire='2025-2026',
    )

    with pytest.raises(ProtectedError):
        prof.delete()


def test_prof_sans_donnees_supprimable(db):
    """Un prof créé par erreur, sans aucune donnée, reste supprimable."""
    prof = ProfVacataireFactory()
    pk = prof.pk
    prof.delete()
    assert not Prof.objects.filter(pk=pk).exists()


# ── P1bis : archivage ─────────────────────────────────────────────────────────

def test_prof_actif_par_defaut(db):
    prof = ProfVacataireFactory()
    assert prof.actif is True


def test_archiver_prof(db, institution):
    """Archiver un prof protégé : alternative à la suppression."""
    prof = ProfVacataireFactory()
    VacationFactory(prof=prof, institution=institution)

    prof.actif = False
    prof.save(update_fields=['actif'])

    prof.refresh_from_db()
    assert prof.actif is False
    assert Prof.objects.filter(pk=prof.pk).exists()   # toujours là, juste archivé


# ── P2 : parents structurels protégés ────────────────────────────────────────

# NB : la suppression d'un Departement traverse la through-table managed=False
# `suivi_pointage_departements`, absente de la base de test SQLite (--no-migrations).
# Pour ces deux FK on vérifie donc la CONFIG on_delete (le comportement réel est
# couvert en prod par PROTECT, identique aux autres cas testés ci-dessus).

def test_etudiant_departement_est_protect():
    from django.db.models import PROTECT
    from apps.absence.models import Etudiant
    field = Etudiant._meta.get_field('departement')
    assert field.remote_field.on_delete is PROTECT


def test_surveillance_departement_est_protect():
    from django.db.models import PROTECT
    from apps.vacation.models import Surveillance
    field = Surveillance._meta.get_field('departement')
    assert field.remote_field.on_delete is PROTECT


# ── Vérif #3 : EM avec inscription protégé (dettes) ───────────────────────────

def test_em_avec_inscription_protege(db):
    """Un EM rattaché à un InscriptionElement ne peut plus être supprimé
    (sinon la reconstruction des dettes, clé sur em, est cassée)."""
    from tests.factories.inscriptions import InscriptionElementFactory
    ie = InscriptionElementFactory()      # crée em via EMLegacyFactory
    em = ie.em
    assert em is not None

    with pytest.raises(ProtectedError):
        em.delete()


# ── Vérif #4 : prof avec suivi / pointage protégé ─────────────────────────────

def test_prof_avec_suivi_protege(db, institution):
    from apps.suivi.models import Suivie
    prof = ProfVacataireFactory()
    Suivie.objects.create(prof=prof, annee_universitaire='2025-2026', institution=institution)

    with pytest.raises(ProtectedError):
        prof.delete()


def test_prof_avec_pointage_protege(db, institution):
    from apps.suivi.models import SuiviePointage
    prof = ProfVacataireFactory()
    SuiviePointage.objects.create(
        prof=prof, annee_universitaire='2025-2026', institution=institution,
    )

    with pytest.raises(ProtectedError):
        prof.delete()


# ── Vérif #2 : guard suppression convention avec notes ────────────────────────

def test_guard_evaluation_stage_detecte_notes(db, institution):
    """Le guard ConventionStage détecte une évaluation portant des notes."""
    from decimal import Decimal as D
    from apps.stages.models import ConventionStage, EvaluationStage
    from apps.stages.views import ConventionStageViewSet
    from apps.absence.models import Etudiant
    from apps.departement.models import Departement

    dept = Departement.objects.create(nom='Dept Stage', institution=institution)
    etu = Etudiant.objects.create(matricule='ETU-ST-1', nom='Stagiaire',
                                  departement=dept, genre='M')
    conv = ConventionStage.objects.create(
        etudiant=etu, entreprise_nom='ACME',
        date_debut=_date(2025, 1, 1), date_fin=_date(2025, 3, 1), sujet='PFE',
    )
    # Sans notes → guard laisse passer
    ev = EvaluationStage.objects.create(convention=conv)
    assert ConventionStageViewSet._evaluation_a_des_notes(ev) is False

    # Avec une note → guard bloque
    ev.note_soutenance = D('14.00')
    ev.save(update_fields=['note_soutenance'])
    assert ConventionStageViewSet._evaluation_a_des_notes(ev) is True


def test_semestre_avec_em_set_null(db, institution):
    """Choix de conception (EM.semestre = SET_NULL, comme departement) :
    supprimer un Semestre ne DÉTRUIT PAS les EM rattachés — l'EM survit, son
    semestre devient NULL (évite la perte de données lors d'une purge d'année)."""
    from apps.parametres.models import Niveau, Semestre
    from apps.em.models import EM
    from tests.factories.em import DepartementAnnuelFactory
    niveau = Niveau.objects.create(niveau='1')
    sem = Semestre.objects.create(
        code_semestre='S1', semestre='Semestre 1',
        niveau_semestre=niveau, type_semestre='I', credits=30,
    )
    em = EM.objects.create(code_em='EMP2', intitule='EM', semestre=sem,
                           departement=DepartementAnnuelFactory(), institution=institution)

    sem.delete()

    em.refresh_from_db()
    assert em.semestre_id is None              # SET_NULL
    assert EM.objects.filter(pk=em.pk).exists()  # l'EM survit


def test_niveau_avec_semestre_protege(db):
    from apps.parametres.models import Niveau, Semestre
    niveau = Niveau.objects.create(niveau='2')
    Semestre.objects.create(
        code_semestre='S3', semestre='Semestre 3',
        niveau_semestre=niveau, type_semestre='I', credits=30,
    )

    with pytest.raises(ProtectedError):
        niveau.delete()


