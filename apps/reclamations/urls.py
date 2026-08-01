from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ReclamationAdminViewSet, PeriodeReclamationViewSet

router = DefaultRouter()
# IMPORTANT : enregistrer 'periodes' AVANT '' (sinon ReclamationAdminViewSet
# capture toutes les routes y compris /periodes/).
router.register('periodes', PeriodeReclamationViewSet, basename='reclamations-periodes')
router.register('', ReclamationAdminViewSet, basename='reclamations')

urlpatterns = [
    path('', include(router.urls)),
]
