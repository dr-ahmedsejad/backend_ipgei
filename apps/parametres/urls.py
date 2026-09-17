from rest_framework.routers import DefaultRouter
from .views import (
    YearViewSet, NiveauViewSet, SemestreViewSet, SeanceViewSet,
    CreneauViewSet, JourViewSet, SemaineViewSet, PaiementViewSet,
    RamadanViewSet, InstitutionViewSet, JourFerieFixeViewSet,
)

router = DefaultRouter()
router.register('annees',      YearViewSet,        basename='annees')
router.register('niveaux',     NiveauViewSet,       basename='niveaux')
router.register('semestres',   SemestreViewSet,     basename='semestres')
router.register('seances',     SeanceViewSet,       basename='seances')
router.register('creneaux',    CreneauViewSet,      basename='creneaux')
router.register('jours',       JourViewSet,         basename='jours')
router.register('semaines',    SemaineViewSet,      basename='semaines')
router.register('paiements',   PaiementViewSet,     basename='paiements')
router.register('ramadan',     RamadanViewSet,      basename='ramadan')
router.register('jours-feries-fixes', JourFerieFixeViewSet, basename='jours-feries-fixes')
router.register('institutions',InstitutionViewSet,  basename='institutions')

urlpatterns = router.urls
