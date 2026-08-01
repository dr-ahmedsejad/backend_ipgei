from rest_framework.routers import DefaultRouter
from .views import EmploisViewSet

router = DefaultRouter()
router.register('', EmploisViewSet, basename='emplois')
urlpatterns = router.urls
