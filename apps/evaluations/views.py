"""Façade des vues evaluations.

Les ViewSets sont découpés par domaine dans les modules frères views_*.py
(sessions / notes / deliberation / reports + helpers partagés).
Ce module ré-exporte pour préserver `from .views import X` (urls.py, etc.).
"""
from .views_helpers import (  # noqa: F401
    _build_institution_context, _render_pdf, _enrichir_lignes_anonymat,
    _grouper_lignes_par_groupe, _resolve_em,
)
from .views_sessions import SessionEvaluationViewSet  # noqa: F401
from .views_notes import (  # noqa: F401
    NoteViewSet, ResultatElementViewSet, ResultatSemestreViewSet,
)
from .views_deliberation import (  # noqa: F401
    PVDeliberationViewSet, LigneDeliberationViewSet, ResultatModuleViewSet,
    MembreJuryViewSet, ObligationRattrapageViewSet, ParametreJuryViewSet,
    RachatNoteViewSet,
)
from .views_reports import (  # noqa: F401
    EmargementViewSet, CollecteNotesViewSet, AnonymatSessionViewSet,
)
