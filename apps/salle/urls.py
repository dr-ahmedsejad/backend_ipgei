from rest_framework.routers import DefaultRouter
from .views import SalleViewSet

router = DefaultRouter()
router.register('', SalleViewSet, basename='salle')
urlpatterns = router.urls
