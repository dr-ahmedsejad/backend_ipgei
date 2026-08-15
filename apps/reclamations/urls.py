from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ReclamationAdminViewSet, PeriodeReclamationViewSet
from .views_seance import ReclamationSeanceAdminViewSet

router = DefaultRouter()
# IMPORTANT : enregistrer 'periodes' AVANT '' (sinon ReclamationAdminViewSet
# capture toutes les routes y compris /periodes/).
router.register('periodes', PeriodeReclamationViewSet, basename='reclamations-periodes')
# Même précaution pour les réclamations de séance : préfixe déclaré avant la
# route fourre-tout, sinon '' l'avale et /seances/ répond 404 sans motif lisible.
router.register('seances', ReclamationSeanceAdminViewSet,
                basename='reclamations-seances')
router.register('', ReclamationAdminViewSet, basename='reclamations')

urlpatterns = [
    path('', include(router.urls)),
]
