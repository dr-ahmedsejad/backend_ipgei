from rest_framework.routers import DefaultRouter
from .views import VacationViewSet, SurveillanceViewSet

router = DefaultRouter()
# Prefix vide → CRUD + actions directement sous /api/v1/vacations/
# ex: GET /api/v1/vacations/             → list
#     GET /api/v1/vacations/etat/        → action etat
#     GET /api/v1/vacations/pdf-fiches/  → action pdf-fiches
router.register(r'',              VacationViewSet,     basename='vacation')
router.register('surveillances',  SurveillanceViewSet, basename='surveillance')

urlpatterns = router.urls
