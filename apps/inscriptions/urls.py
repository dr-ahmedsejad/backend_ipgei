from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    PreinscriptionViewSet, InscriptionAdministrativeViewSet,
    InscriptionPedagogiqueViewSet, InscriptionElementViewSet,
    DerogationViewSet, GrilleFraisViewSet, CandidatBacViewSet,
)
from .views_progression import (
    GenererProgressionsView,
    ListeProgressionsView,
    ModifierProgressionView,
    ExecuterReinscriptionsView,
)
from .views_etudiant import (
    ReleveAnnuelEtudiantView,
    ProgressionEtudiantView,
)

router = DefaultRouter()
router.register(r'preinscriptions', PreinscriptionViewSet,            basename='preinscription')
router.register(r'admin',           InscriptionAdministrativeViewSet, basename='inscription-admin')
router.register(r'pedagogique',     InscriptionPedagogiqueViewSet,    basename='inscription-ped')
router.register(r'elements',        InscriptionElementViewSet,        basename='inscription-element')
router.register(r'derogations',     DerogationViewSet,                basename='derogation')
router.register(r'grilles-frais',   GrilleFraisViewSet,               basename='grille-frais')
router.register(r'candidats-bac',   CandidatBacViewSet,               basename='candidat-bac')

# IMPORTANT : les path() personnalisés DOIVENT être déclarés AVANT router.urls
# pour avoir la priorité de résolution. Sinon le ViewSet 'admin' intercepte
# 'admin/<anything>/' comme un detail/<pk>.
urlpatterns = [
    # ── Progressions (admin scolarité) ───────────────────────────────────────
    path('progressions/generer/',              GenererProgressionsView.as_view(),    name='generer-progressions'),
    path('progressions/executer/',             ExecuterReinscriptionsView.as_view(), name='executer-reinscriptions'),
    path('progressions/<int:pk>/',             ModifierProgressionView.as_view(),    name='modifier-progression'),
    path('progressions/',                      ListeProgressionsView.as_view(),      name='liste-progressions'),

    # ── Portail étudiant (lecture seule) ─────────────────────────────────────
    path('etudiant/releve/',                   ReleveAnnuelEtudiantView.as_view(),   name='etudiant-releve'),
    path('etudiant/progression/',              ProgressionEtudiantView.as_view(),    name='etudiant-progression'),
] + router.urls
