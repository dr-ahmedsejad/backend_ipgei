from rest_framework.routers import DefaultRouter
from .views import ProfTypeHistoryViewSet

router = DefaultRouter()
router.register('', ProfTypeHistoryViewSet, basename='prof-type-history')
urlpatterns = router.urls
