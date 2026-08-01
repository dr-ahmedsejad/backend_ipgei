from rest_framework.routers import DefaultRouter
from .views import DepartementAcademiqueViewSet, FiliereViewSet, ParametresPonderationViewSet

router = DefaultRouter()
router.register(r'departements-academiques',  DepartementAcademiqueViewSet,   basename='departement-academique')
router.register(r'filieres',                  FiliereViewSet,                 basename='filiere')
router.register(r'parametres-ponderation',    ParametresPonderationViewSet,   basename='parametres-ponderation')

urlpatterns = router.urls
