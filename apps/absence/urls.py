from django.urls import path
from rest_framework.routers import DefaultRouter
from .enseignant import ListeSeanceEnseignantView
from .photos import PhotosEtudiantsView
from .views import EtudiantViewSet, PresenceViewSet, SeuilAbsenceView

router = DefaultRouter()
router.register('etudiants', EtudiantViewSet, basename='etudiant')
router.register('presences', PresenceViewSet, basename='presence')

urlpatterns = router.urls + [
    path('seuil/', SeuilAbsenceView.as_view(), name='seuil-absence'),
    path('enseignant/liste/', ListeSeanceEnseignantView.as_view(), name='enseignant-liste-seance'),
    # Photos déposées en une fois, chacune nommée par le matricule (photos.py).
    path('photos-etudiants/', PhotosEtudiantsView.as_view(), name='photos-etudiants'),
]
