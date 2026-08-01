from rest_framework.routers import DefaultRouter
from .views import EMViewSet

router = DefaultRouter()
router.register('', EMViewSet, basename='em')

urlpatterns = router.urls
