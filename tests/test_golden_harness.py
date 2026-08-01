"""
Tests du harnais golden-dataset (Phase 3 migration MySQL → PostgreSQL).

PROUVE sur sqlite que golden_extract / golden_diff tiennent leur contrat :
  1. extract → JSON valide, sections attendues, counts corrects ;
  2. extract x2 → golden_diff exit 0 (déterminisme) + BD strictement inchangée
     entre les deux runs (counts + digest de table avant/après) ;
  3. mutation d'une valeur persistée → golden_diff détecte (exit != 0) et le
     delta imprimé porte la clé naturelle (matricule) ;
  4. read-only : aucun AuditLog créé, counts des tables de résultats inchangés.

Le dataset rejoue la chaîne réelle étudiant → inscriptions → notes → chaîne
canonique de calcul → PV de délibération (même pattern que
tests/test_deliberation_semestre.py), plus un DocumentOfficiel et une entrée
RegistreDiplome pour couvrir les sections documents.
"""
import hashlib
import io
import json
from datetime import date
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from tests.factories.deliberation import PVDeliberationSemestrielFactory
from tests.factories.em import EMLegacyFactory, ModuleLMDFactory
from tests.factories.evaluations import NoteFactory, SessionNormaleImpairsFactory
from tests.factories.inscriptions import (
    InscriptionAdministrativeFactory,
    InscriptionElementFactory,
    InscriptionPedagogiqueFactory,
)
from tests.factories.parametres import YearFactory
from tests.factories.scolarite import FiliereFactory


# ── Dataset réaliste ───────────────────────────────────────────────────────────

def _creer_etudiant_avec_notes(filiere, annee, semestre, session, em, cc, exam):
    """Étudiant inscrit (admin + ped + element) avec notes CC/EXAM saisies."""
    insc_admin = InscriptionAdministrativeFactory(
        filiere=filiere, annee_univ=annee, niveau=1, institution=filiere.institution,
    )
    insc_ped = InscriptionPedagogiqueFactory(
        inscription_admin=insc_admin, semestre=semestre,
    )
    insc_el = InscriptionElementFactory(inscription_ped=insc_ped, em=em, element=None)
    NoteFactory(inscription_element=insc_el, session=session, type_note='CC', valeur=cc)
    NoteFactory(inscription_element=insc_el, session=session, type_note='EXAM', valeur=exam)
    return insc_admin


@pytest.fixture
def golden_dataset(db, institution, semestre_S1):
    """Chaîne complète : notes → calculs réels → PV délibéré → doc officiel."""
    from apps.documents.models import DocumentOfficiel, RegistreDiplome
    from apps.evaluations.models import ResultatModule
    from apps.evaluations.services.calcul_notes import NoteCalculService
    from apps.evaluations.services.deliberation_semestre import DeliberationSemestreService

    annee = YearFactory(annee='2025-2026')
    filiere = FiliereFactory(institution=institution)
    session = SessionNormaleImpairsFactory(institution=institution, annee_univ=annee)
    module = ModuleLMDFactory(
        filiere=filiere, semestre=semestre_S1, institution=institution,
    )
    em = EMLegacyFactory(
        module_lmd=module, semestre=semestre_S1, institution=institution,
        has_tp=False, credits=6, coefficient=2,
    )

    # Un admis (ME=(12x2+15x3)/5=13.8) et un ajourné compensable (ME=8.0)
    ia_admis = _creer_etudiant_avec_notes(
        filiere, annee, semestre_S1, session, em, Decimal('12.00'), Decimal('15.00'),
    )
    ia_ajourne = _creer_etudiant_avec_notes(
        filiere, annee, semestre_S1, session, em, Decimal('8.00'), Decimal('8.00'),
    )

    # Chaîne canonique (identique à pv_actions._recalculer_chaine)
    svc = NoteCalculService(session)
    svc.calculer_tous_elements_session()
    ResultatModule.objects.filter(session=session).delete()
    svc.calculer_tous_modules_session()
    svc.calculer_tous_semestres_session()

    # PV semestriel délibéré (lignes + décisions + obligations Art. 17)
    pv = PVDeliberationSemestrielFactory(
        institution=institution, filiere=filiere,
        session=session, niveau=1, semestre_code='S1',
    )
    svc_pv = DeliberationSemestreService(pv)
    svc_pv.peupler_lignes()
    svc_pv.calculer_decisions()
    svc_pv.generer_obligations()

    # Document officiel (relevé) — hash déterministe de métadonnées
    etudiant = ia_admis.etudiant
    numero = 'RS-2026-00001'
    doc = DocumentOfficiel.objects.create(
        institution=institution, etudiant=etudiant,
        type_document='releve_semestre', numero_serie=numero,
        annee_universitaire='2025-2026', semestre=semestre_S1,
        hash_sha256=hashlib.sha256(
            f'{numero}{etudiant.matricule}releve_semestre2025-2026'.encode()
        ).hexdigest(),
    )

    # Registre des diplômes (append-only : create pur, jamais d'update)
    RegistreDiplome.objects.create(
        institution=institution, etudiant=etudiant, filiere=filiere,
        numero_diplome='DLP-2026-0001', mention='Assez Bien',
        moyenne_generale=Decimal('13.80'), date_delivrance=date(2026, 7, 1),
        annee_universitaire='2025-2026',
    )

    return {
        'annee': annee, 'filiere': filiere, 'session': session,
        'pv': pv, 'doc': doc,
        'matricule_admis': etudiant.matricule,
        'matricule_ajourne': ia_ajourne.etudiant.matricule,
        'insc_admin_admis': ia_admis,
    }


def _extract(tmp_path, nom, **options):
    """Lance golden_extract vers tmp_path/<nom> et retourne (chemin, dict)."""
    chemin = tmp_path / nom
    call_command('golden_extract', '--out', str(chemin), '--label', 'mysql',
                 stdout=io.StringIO(), **options)
    with open(chemin, encoding='utf-8') as fh:
        return chemin, json.load(fh)


# ── 1. Structure du JSON + counts ──────────────────────────────────────────────

class TestExtractStructure:

    def test_sections_attendues_et_counts(self, golden_dataset, tmp_path):
        from apps.absence.models import Etudiant
        from apps.evaluations.models import (
            LigneDeliberation, ResultatElement, ResultatSemestre,
        )

        _, data = _extract(tmp_path, 'a.json')

        assert data['label'] == 'mysql'
        assert set(data) == {'label', '_meta', 'invariants', 'digests', 'order_signatures'}
        inv = data['invariants']
        assert set(inv) == {
            'resultats_persistes', 'resultats_recalcules', 'pv_deliberation',
            'documents_officiels', 'registre_diplomes',
        }

        # Digests : counts exacts sur des tables connues
        assert data['digests']['absence_etudiant']['count'] == Etudiant.objects.count()
        assert (data['digests']['evaluations_resultat_semestre']['count']
                == ResultatSemestre.objects.count())
        assert (data['digests']['evaluations_resultat_element']['count']
                == ResultatElement.objects.count())
        assert (data['digests']['evaluations_ligne_deliberation']['count']
                == LigneDeliberation.objects.count())
        # Chaque digest est trié (contrat 'sha256_sorted')
        digest_rs = data['digests']['evaluations_resultat_semestre']['sha256_sorted']
        assert digest_rs == sorted(digest_rs)
        assert len(digest_rs) == ResultatSemestre.objects.count()

        # Invariants résultats : clé naturelle matricule|code_semestre|annee|acronyme
        mat = golden_dataset['matricule_admis']
        cle = f'{mat}|S1|2025-2026|TEST'
        assert cle in inv['resultats_persistes']
        (session_cle, valeurs), = inv['resultats_persistes'][cle].items()
        assert session_cle == 'TEST|2025-2026|normale|Impairs'
        assert valeurs['semestre']['est_admis'] == '1'
        assert valeurs['semestre']['moyenne'] == '13.8000'
        # Recalculé == persisté (le dataset a été produit par la même chaîne)
        assert inv['resultats_recalcules'][cle] == inv['resultats_persistes'][cle]

        # PV : lignes indexées par matricule, décisions présentes
        (pv_cle, pv_data), = inv['pv_deliberation'].items()
        assert pv_cle.startswith('semestriel|TEST|')
        assert pv_data['lignes'][mat]['decision'] == 'admis'
        ligne_ajourne = pv_data['lignes'][golden_dataset['matricule_ajourne']]
        assert ligne_ajourne['decision'] == 'ajourned'
        assert ligne_ajourne['obligations']    # NV → au moins une obligation Art. 17

        # Documents : clé numero_serie + contexte relevé déterministe + URL QR
        doc = inv['documents_officiels']['RS-2026-00001']
        assert doc['hash_sha256'] == golden_dataset['doc'].hash_sha256
        assert doc['token_verification'] == str(golden_dataset['doc'].token_verification)
        assert doc['qr_url'].endswith(f"/verifier/{doc['token_verification']}")
        assert 'photo_b64' not in doc['contexte_releve']
        assert 'logo_b64' not in doc['contexte_releve']
        assert doc['contexte_releve']['moyenne_semestre'] == '13.8000'

        # Registre des diplômes : liste ordonnée de clés naturelles
        assert inv['registre_diplomes'] == [
            [mat, 'DLP-2026-0001', 'Assez Bien', '13.8000', '2026-07-01'],
        ]

        # Signatures d'ordre : présentes, avec count + sha256
        sig = data['order_signatures']['etudiant_matricule']
        assert sig['count'] == Etudiant.objects.count()
        assert len(sig['sha256']) == 64

    def test_option_tables_restreint_les_digests(self, golden_dataset, tmp_path):
        _, data = _extract(tmp_path, 'restreint.json',
                           tables='absence_etudiant', skip_recalc=True)
        assert list(data['digests']) == ['absence_etudiant']
        assert 'resultats_recalcules' not in data['invariants']


# ── 2. Déterminisme + BD inchangée entre deux runs ─────────────────────────────

class TestDeterminisme:

    def test_deux_extracts_identiques_et_bd_intacte(self, golden_dataset, tmp_path):
        from apps.evaluations.models import (
            ResultatElement, ResultatModule, ResultatSemestre,
        )
        from core.management.commands.golden_extract import digest_model

        counts_avant = (
            ResultatElement.objects.count(),
            ResultatModule.objects.count(),
            ResultatSemestre.objects.count(),
        )
        digest_avant = digest_model(ResultatSemestre)

        chemin_a, _ = _extract(tmp_path, 'run1.json')
        chemin_b, _ = _extract(tmp_path, 'run2.json')

        # La BD n'a pas bougé entre les deux runs (extract = READ-ONLY)
        counts_apres = (
            ResultatElement.objects.count(),
            ResultatModule.objects.count(),
            ResultatSemestre.objects.count(),
        )
        assert counts_apres == counts_avant
        assert digest_model(ResultatSemestre) == digest_avant

        # golden_diff : 0 écart, exit 0 (pas de CommandError)
        out = io.StringIO()
        call_command('golden_diff', str(chemin_a), str(chemin_b), stdout=out)
        assert 'GOLDEN DIFF: OK (0 écart)' in out.getvalue()

    def test_meta_et_label_ignores(self, golden_dataset, tmp_path):
        """Deux extracts avec labels différents restent équivalents pour le diff."""
        chemin_a = tmp_path / 'label_a.json'
        chemin_b = tmp_path / 'label_b.json'
        call_command('golden_extract', '--out', str(chemin_a), '--label', 'mysql',
                     skip_recalc=True, stdout=io.StringIO())
        call_command('golden_extract', '--out', str(chemin_b), '--label', 'pg',
                     skip_recalc=True, stdout=io.StringIO())

        out = io.StringIO()
        call_command('golden_diff', str(chemin_a), str(chemin_b), stdout=out)
        assert 'GOLDEN DIFF: OK (0 écart)' in out.getvalue()


# ── 3. Une mutation persistée est détectée avec sa clé naturelle ───────────────

class TestMutationDetectee:

    def test_update_moyenne_detecte_par_le_diff(self, golden_dataset, tmp_path):
        from apps.evaluations.models import ResultatSemestre

        chemin_a, _ = _extract(tmp_path, 'avant.json')

        # Mutation silencieuse (queryset.update : ni signal ni audit)
        insc_ped = golden_dataset['insc_admin_admis'].inscriptions_ped.first()
        ResultatSemestre.objects.filter(inscription_ped=insc_ped).update(
            moyenne=Decimal('19.99'),
        )

        chemin_b, _ = _extract(tmp_path, 'apres.json')

        out = io.StringIO()
        with pytest.raises(CommandError, match='écart'):
            call_command('golden_diff', str(chemin_a), str(chemin_b), stdout=out)

        sortie = out.getvalue()
        # Le delta imprimé porte la clé naturelle (matricule) et les 2 valeurs
        assert golden_dataset['matricule_admis'] in sortie
        assert '13.8000' in sortie
        assert '19.9900' in sortie

    def test_max_prints_tronque(self, golden_dataset, tmp_path):
        """--max-prints limite l'affichage en annonçant le total."""
        from apps.evaluations.models import ResultatElement, ResultatSemestre

        chemin_a, _ = _extract(tmp_path, 'trunc_avant.json')
        ResultatSemestre.objects.all().update(moyenne=Decimal('19.99'))
        ResultatElement.objects.all().update(note_finale=Decimal('19.99'))
        chemin_b, _ = _extract(tmp_path, 'trunc_apres.json')

        out = io.StringIO()
        with pytest.raises(CommandError, match='écart'):
            call_command('golden_diff', str(chemin_a), str(chemin_b),
                         max_prints=1, stdout=out)
        sortie = out.getvalue()
        assert sortie.count('DELTA') == 1
        assert 'tronquée' in sortie


# ── 4. Read-only : pas d'audit, pas d'écriture résiduelle ──────────────────────

class TestReadOnly:

    def test_aucun_audit_ni_ecriture_apres_extract(self, golden_dataset, tmp_path):
        from apps.evaluations.models import (
            LigneDeliberation, ObligationRattrapage,
            ResultatElement, ResultatModule, ResultatSemestre,
        )
        from core.models import AuditLog

        audits_avant = AuditLog.objects.count()
        counts_avant = {
            'resultat_element':  ResultatElement.objects.count(),
            'resultat_module':   ResultatModule.objects.count(),
            'resultat_semestre': ResultatSemestre.objects.count(),
            'ligne_deliberation': LigneDeliberation.objects.count(),
            'obligation':        ObligationRattrapage.objects.count(),
        }

        _extract(tmp_path, 'ro.json')

        assert AuditLog.objects.count() == audits_avant
        assert {
            'resultat_element':  ResultatElement.objects.count(),
            'resultat_module':   ResultatModule.objects.count(),
            'resultat_semestre': ResultatSemestre.objects.count(),
            'ligne_deliberation': LigneDeliberation.objects.count(),
            'obligation':        ObligationRattrapage.objects.count(),
        } == counts_avant


# ── Canonicalisation : pièges documentés (bool avant int, sentinelles…) ────────

class TestCanonicalisation:
    """Tests unitaires purs des fonctions module-level importables."""

    def test_bool_avant_int_et_sentinelle_null(self):
        from core.management.commands.golden_extract import (
            NULL_SENTINEL, canon_value,
        )
        assert canon_value(True) == '1'
        assert canon_value(False) == '0'
        assert canon_value(1) == '1'          # int reste int → str
        assert canon_value(None) == NULL_SENTINEL
        assert canon_value('') == ''          # chaîne vide != NULL

    def test_decimal_float_datetime_uuid(self):
        import datetime as dt
        import uuid as _uuid
        from core.management.commands.golden_extract import canon_value

        assert canon_value(Decimal('12.5')) == '12.5000'
        assert canon_value(0.1 + 0.2) == '0.3'           # repr canonique arrondi
        aware = dt.datetime(2026, 1, 1, 12, 0, 0,
                            tzinfo=dt.timezone(dt.timedelta(hours=1)))
        assert canon_value(aware) == '2026-01-01T11:00:00.000000+00:00'
        naif = dt.datetime(2026, 1, 1, 12, 0, 0)
        assert canon_value(naif) == 'naive:2026-01-01T12:00:00.000000'
        u = _uuid.UUID('ABCDEF01-2345-6789-ABCD-EF0123456789')
        assert canon_value(u) == 'abcdef01-2345-6789-abcd-ef0123456789'

    def test_str_brut_sensible_a_la_casse(self):
        """Le texte reste BRUT : un diff de casse MySQL(_ci) vs PG doit se voir."""
        from core.management.commands.golden_extract import row_hash
        assert row_hash(['Dupont']) != row_hash(['DUPONT'])

    def test_deep_diff_liste_de_hashes(self):
        from core.management.commands.golden_diff import deep_diff
        deltas = deep_diff({'t': {'sha256_sorted': ['aaa', 'bbb']}},
                           {'t': {'sha256_sorted': ['aaa', 'ccc']}})
        texte = '\n'.join(deltas)
        assert 'uniquement dans A → bbb' in texte
        assert 'uniquement dans B → ccc' in texte
