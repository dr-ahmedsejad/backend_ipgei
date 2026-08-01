from rest_framework.routers import DefaultRouter
from .views import DocumentOfficielViewSet, RegistreDiplomeViewSet

router = DefaultRouter()
router.register(r'officiels', DocumentOfficielViewSet, basename='document-officiel')
router.register(r'registre-diplomes', RegistreDiplomeViewSet, basename='registre-diplome')

urlpatterns = router.urls
