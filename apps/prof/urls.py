from rest_framework.routers import DefaultRouter
from .views import ProfViewSet

router = DefaultRouter()
router.register('', ProfViewSet, basename='prof')
urlpatterns = router.urls
