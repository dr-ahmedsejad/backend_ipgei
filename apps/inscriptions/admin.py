from django.contrib import admin
from .models import (
    Preinscription, InscriptionAdministrative,
    InscriptionPedagogique, InscriptionElement,
)


@admin.register(Preinscription)
class PreinscriptionAdmin(admin.ModelAdmin):
    list_display  = ('numero_dossier', 'nom_fr', 'prenom_fr', 'filiere', 'annee_univ', 'statut', 'date_soumission')
    list_filter   = ('statut', 'filiere', 'annee_univ')
    search_fields = ('nom_fr', 'prenom_fr', 'cni', 'email')
    readonly_fields = ('numero_dossier', 'date_soumission')


@admin.register(InscriptionAdministrative)
class InscriptionAdministrativeAdmin(admin.ModelAdmin):
    list_display  = ('etudiant', 'filiere', 'annee_univ', 'niveau', 'statut', 'est_payee')
    list_filter   = ('statut', 'filiere', 'annee_univ', 'est_payee')
    search_fields = ('etudiant__matricule', 'etudiant__nom', 'numero_inscription')
    readonly_fields = ('date_inscription',)


@admin.register(InscriptionPedagogique)
class InscriptionPedagogiqueAdmin(admin.ModelAdmin):
    list_display  = ('inscription_admin', 'semestre', 'est_redoublant', 'date_inscription')
    list_filter   = ('est_redoublant', 'semestre')


@admin.register(InscriptionElement)
class InscriptionElementAdmin(admin.ModelAdmin):
    list_display  = ('inscription_ped', 'element', 'est_dette', 'annee_dette')
    list_filter   = ('est_dette',)
