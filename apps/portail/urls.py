from django.urls import path

from apps.reclamations.views_seance import ReclamerSeanceView

from .views import (
    MonProfilView, MonEmploiView, MesAbsencesView,
    MesNotesView, MesResultatsView,
    MesDocumentsView, TelechargerDocumentView,
    DocumentsDisponiblesView, TelechargerDirectView,
    MesReclamationsView, DetailReclamationView,
    SemaniesEtudiantView, PeriodesReclamationActivesView,
    MesAnneesView,
)

urlpatterns = [
    path('annees/',                         MesAnneesView.as_view(),          name='portail-annees'),
    # Boîte de réception : l'enseignant conteste EN LIGNE une séance « Non
    # fait ». Écrit dans une table exclue de la synchronisation, donc
    # préservée au push suivant — d'où son chemin dans MIRROR_WRITE_ALLOWLIST.
    path('reclamer-seance/',                ReclamerSeanceView.as_view(),     name='portail-reclamer-seance'),
    path('semaines/',                        SemaniesEtudiantView.as_view(),   name='portail-semaines'),
    path('profil/',                         MonProfilView.as_view(),          name='portail-profil'),
    path('emploi-du-temps/',                MonEmploiView.as_view(),          name='portail-emploi'),
    path('absences/',                       MesAbsencesView.as_view(),        name='portail-absences'),
    path('notes/',                          MesNotesView.as_view(),           name='portail-notes'),
    path('resultats/semestres/',            MesResultatsView.as_view(),       name='portail-resultats'),
    path('documents/',                           MesDocumentsView.as_view(),          name='portail-documents'),
    path('documents/disponibles/',               DocumentsDisponiblesView.as_view(),  name='portail-docs-disponibles'),
    path('documents/telecharger-direct/',        TelechargerDirectView.as_view(),     name='portail-doc-direct'),
    path('documents/<int:pk>/telecharger/',      TelechargerDocumentView.as_view(),   name='portail-doc-dl'),
    path('reclamations/',                   MesReclamationsView.as_view(),    name='portail-reclamations'),
    path('reclamations/<int:pk>/',          DetailReclamationView.as_view(),  name='portail-reclamation-detail'),
    path('reclamations/periodes-actives/',  PeriodesReclamationActivesView.as_view(), name='portail-reclamations-periodes'),
]
