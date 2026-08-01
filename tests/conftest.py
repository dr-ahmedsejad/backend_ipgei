"""
Fixtures partagees pour toute la suite tests.
Approche : factories synthetiques (factory_boy) creees dans une BD sqlite RAM.
La BD prod gesafped26 n'est JAMAIS touchee.
"""
import pytest
from decimal import Decimal


# ── Vide le cache RBAC entre chaque test (LocMemCache partage par defaut) ──────
@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


# ── Cree manuellement les tables managed=False (vendor-aware) ───────────────────
# Sans cette fixture, le signal post_save de Prof essaie d'insérer dans une
# table inexistante (managed=False -> Django ne les cree ni en syncdb ni via
# les migrations). Deux tables : prof_type_history et suivi_pointage_departements
# (through du M2M SuiviePointage.departements).
_DDL_UNMANAGED = {
    'sqlite': [
        """
        CREATE TABLE IF NOT EXISTS prof_type_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            prof_id     INTEGER NOT NULL,
            type        VARCHAR(50) NOT NULL,
            date_debut  DATE NOT NULL,
            date_fin    DATE,
            motif       TEXT NOT NULL DEFAULT '',
            cree_par    VARCHAR(100) NOT NULL DEFAULT '',
            cree_le     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS suivi_pointage_departements (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            suiviepointage_id  BIGINT NOT NULL REFERENCES suivi_suivie_pointage (id) ON DELETE CASCADE,
            departement_id     BIGINT NOT NULL REFERENCES departement (id),
            UNIQUE (suiviepointage_id, departement_id)
        );
        """,
    ],
    'postgresql': [
        """
        CREATE TABLE IF NOT EXISTS prof_type_history (
            id          bigserial PRIMARY KEY,
            prof_id     bigint NOT NULL REFERENCES prof (id) ON DELETE CASCADE,
            type        varchar(50) NOT NULL,
            date_debut  date NOT NULL,
            date_fin    date,
            motif       text NOT NULL DEFAULT '',
            cree_par    varchar(100) NOT NULL DEFAULT '',
            cree_le     timestamp with time zone NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS suivi_pointage_departements (
            id                 bigserial PRIMARY KEY,
            suiviepointage_id  bigint NOT NULL REFERENCES suivi_suivie_pointage (id) ON DELETE CASCADE,
            departement_id     bigint NOT NULL REFERENCES departement (id),
            UNIQUE (suiviepointage_id, departement_id)
        );
        """,
    ],
}


@pytest.fixture(autouse=True, scope='session')
def _create_unmanaged_tables(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        from django.db import connection
        ddl_list = _DDL_UNMANAGED.get(connection.vendor)
        if ddl_list is None:  # vendor inattendu (mysql interdit en test)
            raise RuntimeError(f'Vendor de test non supporté : {connection.vendor}')
        with connection.cursor() as cur:
            for ddl in ddl_list:
                cur.execute(ddl)


# ── Auto-marker @pytest.mark.django_db pour tout test qui utilise une fixture DB ──
@pytest.fixture(autouse=False)
def _db_marker(db):
    """Helper : utilise dans les fixtures qui ont besoin de la BD."""
    return db


# ── Institution principale (auto-injection depuis sérialiseurs) ───────────────
@pytest.fixture
def institution(db):
    from tests.factories.parametres import InstitutionFactory
    return InstitutionFactory(est_principale=True)


# ── Annee universitaire courante ───────────────────────────────────────────────
@pytest.fixture
def annee_2025(db, institution):
    return '2025-2026'


# ── Niveaux LMD (L1, L2, L3) ───────────────────────────────────────────────────
@pytest.fixture
def niveau_L1(db):
    from tests.factories.parametres import NiveauFactory
    return NiveauFactory(niveau='L1')


@pytest.fixture
def niveau_L2(db):
    from tests.factories.parametres import NiveauFactory
    return NiveauFactory(niveau='L2')


@pytest.fixture
def niveau_L3(db):
    from tests.factories.parametres import NiveauFactory
    return NiveauFactory(niveau='L3')


# ── Semestres S1 (Impair) et S2 (Pair) — niveau L1 ─────────────────────────────
@pytest.fixture
def semestre_S1(db, niveau_L1):
    from tests.factories.parametres import SemestreFactory
    return SemestreFactory(
        code_semestre='S1',
        semestre='Semestre 1',
        type_semestre='I',
        niveau_semestre=niveau_L1,
        credits=30,
    )


@pytest.fixture
def semestre_S2(db, niveau_L1):
    from tests.factories.parametres import SemestreFactory
    return SemestreFactory(
        code_semestre='S2',
        semestre='Semestre 2',
        type_semestre='P',
        niveau_semestre=niveau_L1,
        credits=30,
    )


# ── Filière DLP ─────────────────────────────────────────────────────────────────
@pytest.fixture
def filiere_dlp(db, institution):
    from tests.factories.scolarite import FiliereFactory
    return FiliereFactory(
        intitule_fr='Comptabilite et Gestion',
        intitule_ar='محاسبة وإدارة',
        institution=institution,
    )


# ── Helpers de calcul Decimal ──────────────────────────────────────────────────
@pytest.fixture
def D():
    """Raccourci pour Decimal('xx') dans les tests."""
    return Decimal
