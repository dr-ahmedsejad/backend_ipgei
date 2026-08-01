from django.contrib import admin
from .models import Reclamation

@admin.register(Reclamation)
class ReclamationAdmin(admin.ModelAdmin):
    list_display  = ['id', 'etudiant', 'type_reclamation', 'statut', 'date_soumission']
    list_filter   = ['statut', 'type_reclamation']
    search_fields = ['etudiant__matricule', 'etudiant__nom']
