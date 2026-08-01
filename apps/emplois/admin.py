from django.contrib import admin
from .models import Emplois, EmploisArchive


@admin.register(Emplois)
class EmploisAdmin(admin.ModelAdmin):
    list_display  = ['id', 'prof', 'em', 'type_seance_fk', 'jour_fk', 'creneau_fk',
                     'salle', 'departement', 'semestre', 'annee_universitaire']
    list_filter   = ['annee_universitaire', 'type_semestre', 'jour_fk', 'type_seance_fk']
    search_fields = ['prof__nom', 'em__code_em', 'departement__nom']
    raw_id_fields = ['prof', 'em', 'salle', 'departement', 'semestre',
                     'creneau_fk', 'type_seance_fk', 'jour_fk']
    list_select_related = ['prof', 'em', 'salle', 'departement', 'semestre',
                           'creneau_fk', 'type_seance_fk', 'jour_fk']


@admin.register(EmploisArchive)
class EmploisArchiveAdmin(admin.ModelAdmin):
    list_display  = ['id', 'prof', 'em', 'type_seance_fk', 'jour_fk', 'departement',
                     'annee_universitaire']
    list_filter   = ['annee_universitaire', 'type_semestre', 'jour_fk', 'type_seance_fk']
    search_fields = ['prof__nom', 'em__code_em', 'departement__nom']
    raw_id_fields = ['prof', 'em', 'salle', 'departement', 'semestre',
                     'creneau_fk', 'type_seance_fk', 'jour_fk']
