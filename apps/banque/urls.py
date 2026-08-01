from rest_framework.routers import DefaultRouter
from .views import BanqueViewSet

router = DefaultRouter()
router.register('', BanqueViewSet, basename='banque')
urlpatterns = router.urls
