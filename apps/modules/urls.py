from rest_framework.routers import DefaultRouter
from .views import ModuleViewSet, ElementModuleViewSet

router = DefaultRouter()
router.register(r'modules',  ModuleViewSet,        basename='module')
router.register(r'elements', ElementModuleViewSet, basename='element')

urlpatterns = router.urls
