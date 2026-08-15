from rest_framework.routers import DefaultRouter

from .views import CoursViewSet

router = DefaultRouter()
router.register('', CoursViewSet, basename='cours')

urlpatterns = router.urls
