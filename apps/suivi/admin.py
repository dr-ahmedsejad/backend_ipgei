from django.contrib import admin
from .models import Suivie, SuiviePointage, ChargeInstitution


@admin.register(Suivie)
class SuivieAdmin(admin.ModelAdmin):
    list_display  = ['id', 'prof', 'em', 'jour_fk', 'creneau_fk', 'departement',
                     'numero_semaine', 'commentaire', 'annee_universitaire']
    list_filter   = ['annee_universitaire', 'commentaire', 'type_seance_fk',
                     'type_semestre', 'jour_fk']
    search_fields = ['prof__nom', 'em__code_em', 'departement__nom']
    ordering      = ['-annee_universitaire', '-numero_semaine']


@admin.register(SuiviePointage)
class SuiviePointageAdmin(admin.ModelAdmin):
    list_display  = ['id', 'prof', 'em', 'jour_fk', 'creneau_fk',
                     'numero_semaine', 'annee_universitaire']
    list_filter   = ['annee_universitaire', 'type_seance_fk', 'numero_semaine']
    search_fields = ['prof__nom', 'em__code_em']
    ordering      = ['-annee_universitaire', '-numero_semaine']


@admin.register(ChargeInstitution)
class ChargeInstitutionAdmin(admin.ModelAdmin):
    list_display  = ['id', 'prof', 'institution', 'charge_cm', 'annee_universitaire']
    list_filter   = ['annee_universitaire', 'institution']
    search_fields = ['prof__nom']
