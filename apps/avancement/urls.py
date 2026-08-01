from django.urls import path
from .views import (
    AvancementEMView, AvancementProfsView, AvancementProfDetailView,
    ChargeProfsPermanantsView, ChargePermanentsMensuelView,
    RepartitionChargesView,
    SuiviProfView, SuiviPointageProfDetailView,
    StatistiquesProfsView, StatistiquesSemestresView, StatistiquesVacationsView,
    AvancementEMPDFView, AvancementProfsPDFView, AvancementProfDetailPDFView,
    ChargePermanentsPDFView, ChargePermanentsMensuelPDFView,
    ChargePermanentsMensuelExcelView, SuiviProfPDFView,
)

urlpatterns = [
    # ── Données JSON ─────────────────────────────────────────────────────────
    path('em/',                       AvancementEMView.as_view(),               name='avancement-em'),
    path('profs/',                    AvancementProfsView.as_view(),            name='avancement-profs'),
    path('profs/detail/',             AvancementProfDetailView.as_view(),       name='avancement-profs-detail'),
    path('charge-permanents/',        ChargeProfsPermanantsView.as_view(),      name='charge-permanents'),
    path('charge-permanents-mensuel/', ChargePermanentsMensuelView.as_view(),   name='charge-permanents-mensuel'),
    path('suivi-prof/',               SuiviProfView.as_view(),                  name='suivi-prof'),
    path('suivi-pointage-prof/',      SuiviPointageProfDetailView.as_view(),    name='suivi-pointage-prof'),
    path('stats/profs/',              StatistiquesProfsView.as_view(),          name='stats-profs'),
    # path('stats/semestres/',        StatistiquesSemestresView.as_view(),      name='stats-semestres'),
    path('semestres/',                StatistiquesSemestresView.as_view(),      name='stats-semestres'),
    path('vacations/',                StatistiquesVacationsView.as_view(),      name='stats-vacations'),
    path('repartition-charges/',      RepartitionChargesView.as_view(),         name='stats-repartition-charges'),

    # ── PDF wkhtmltopdf ───────────────────────────────────────────────────────
    path('em/pdf/',                       AvancementEMPDFView.as_view(),            name='avancement-em-pdf'),
    path('profs/pdf/',                    AvancementProfsPDFView.as_view(),         name='avancement-profs-pdf'),
    path('profs/detail/pdf/',             AvancementProfDetailPDFView.as_view(),    name='avancement-profs-detail-pdf'),
    path('charge-permanents/pdf/',           ChargePermanentsPDFView.as_view(),         name='charge-permanents-pdf'),
    path('charge-permanents-mensuel/pdf/',   ChargePermanentsMensuelPDFView.as_view(),  name='charge-permanents-mensuel-pdf'),
    path('charge-permanents-mensuel/excel/', ChargePermanentsMensuelExcelView.as_view(), name='charge-permanents-mensuel-excel'),
    path('suivi-prof/pdf/',                  SuiviProfPDFView.as_view(),                name='suivi-prof-pdf'),
]
