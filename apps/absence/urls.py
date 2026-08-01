from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import EtudiantViewSet, PresenceViewSet, SeuilAbsenceView

router = DefaultRouter()
router.register('etudiants', EtudiantViewSet, basename='etudiant')
router.register('presences', PresenceViewSet, basename='presence')

urlpatterns = router.urls + [
    path('seuil/', SeuilAbsenceView.as_view(), name='seuil-absence'),
]
