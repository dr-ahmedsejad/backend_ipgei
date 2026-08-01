from rest_framework.routers import DefaultRouter
from .views import (
    SuivieViewSet, SuiviePointageViewSet, ChargeInstitutionViewSet,
    SuiviGenerationAuthorizationViewSet,
)

router = DefaultRouter()
router.register('suivies',     SuivieViewSet,                       basename='suivie')
router.register('pointages',   SuiviePointageViewSet,               basename='pointage')
router.register('charges',     ChargeInstitutionViewSet,            basename='charge-institution')
router.register('rattrapages', SuiviGenerationAuthorizationViewSet, basename='rattrapages')

urlpatterns = router.urls
