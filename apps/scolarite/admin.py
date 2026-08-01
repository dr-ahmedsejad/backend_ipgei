from django.contrib import admin
from .models import Filiere


@admin.register(Filiere)
class FiliereAdmin(admin.ModelAdmin):
    list_display  = ('code', 'intitule_fr', 'type_diplome', 'nb_semestres', 'est_active', 'institution')
    list_filter   = ('type_diplome', 'est_active', 'institution')
    search_fields = ('code', 'intitule_fr', 'intitule_ar')
    ordering      = ('code',)
