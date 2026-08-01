from rest_framework.routers import DefaultRouter
from .views import DepartementViewSet

router = DefaultRouter()
router.register('', DepartementViewSet, basename='departement')
urlpatterns = router.urls
