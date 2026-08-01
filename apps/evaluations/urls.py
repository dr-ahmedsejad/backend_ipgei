from rest_framework.routers import DefaultRouter
from .views import (
    SessionEvaluationViewSet, NoteViewSet,
    ResultatElementViewSet, ResultatSemestreViewSet,
    ResultatModuleViewSet, MembreJuryViewSet, ObligationRattrapageViewSet,
    PVDeliberationViewSet, LigneDeliberationViewSet,
    ParametreJuryViewSet, RachatNoteViewSet,
    EmargementViewSet, CollecteNotesViewSet, AnonymatSessionViewSet,
)

router = DefaultRouter()
router.register(r'sessions',                SessionEvaluationViewSet,   basename='session-evaluation')
router.register(r'notes',                   NoteViewSet,                basename='note')
router.register(r'resultats/elements',      ResultatElementViewSet,     basename='resultat-element')
router.register(r'resultats/semestres',     ResultatSemestreViewSet,    basename='resultat-semestre')
router.register(r'resultats/modules',       ResultatModuleViewSet,      basename='resultat-module')
router.register(r'pvs',                     PVDeliberationViewSet,      basename='pv-deliberation')
router.register(r'lignes-deliberation',     LigneDeliberationViewSet,   basename='ligne-deliberation')
router.register(r'membres-jury',            MembreJuryViewSet,          basename='membre-jury')
router.register(r'obligations-rattrapage',  ObligationRattrapageViewSet, basename='obligation-rattrapage')
router.register(r'parametres-jury',         ParametreJuryViewSet,       basename='parametre-jury')
router.register(r'rachats',                 RachatNoteViewSet,          basename='rachat-note')
router.register(r'emargement',              EmargementViewSet,          basename='emargement')
router.register(r'collecte-notes',          CollecteNotesViewSet,       basename='collecte-notes')
router.register(r'anonymats',               AnonymatSessionViewSet,     basename='anonymat-session')

urlpatterns = router.urls
