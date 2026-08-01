from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import (
    ConventionStageViewSet, EvaluationStageViewSet, DerogationMedicaleViewSet,
    ClassementStageView, ClassementStageExcelView,
)

router = DefaultRouter()
router.register(r'conventions',  ConventionStageViewSet,    basename='convention-stage')
router.register(r'evaluations',  EvaluationStageViewSet,    basename='evaluation-stage')
router.register(r'derogations',  DerogationMedicaleViewSet, basename='derogation-medicale')

urlpatterns = router.urls + [
    path('classement/',       ClassementStageView.as_view(),      name='stages-classement'),
    path('classement/excel/', ClassementStageExcelView.as_view(), name='stages-classement-excel'),
]
