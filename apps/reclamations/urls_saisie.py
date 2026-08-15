"""
Routes de la saisie de notes en ligne — `/api/v1/saisie-en-ligne/`.

Ce préfixe figure dans `MIRROR_WRITE_ALLOWLIST` : c'est le seul chemin par
lequel un enseignant écrit sur le miroir, et il n'écrit que des brouillons.
"""
from django.urls import path

from .views_saisie import (
    SaisieBulkView, SaisieExportView, SaisieFeuilleView, SaisieListView,
)

urlpatterns = [
    path('feuille/',     SaisieFeuilleView.as_view(), name='saisie-en-ligne-feuille'),
    path('saisir-bulk/', SaisieBulkView.as_view(),    name='saisie-en-ligne-bulk'),
    path('export/',      SaisieExportView.as_view(),  name='saisie-en-ligne-export'),
    path('',             SaisieListView.as_view(),    name='saisie-en-ligne-list'),
]
