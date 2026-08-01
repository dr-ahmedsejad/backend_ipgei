from django.contrib import admin
from .models import (
    SessionEvaluation, Note, ResultatElement,
    ResultatSemestre, PVDeliberation, LigneDeliberation,
)


@admin.register(SessionEvaluation)
class SessionEvaluationAdmin(admin.ModelAdmin):
    list_display  = ('annee_univ', 'type_session', 'type_semestre', 'date_debut_saisie', 'date_cloture_saisie', 'est_close', 'rattrapage_vcs_actif', 'rattrapage_vci_actif')
    list_editable = ('rattrapage_vcs_actif', 'rattrapage_vci_actif')   # toggles directs des exceptions VCS / VCI (figées par session)
    list_filter   = ('type_session', 'est_close', 'annee_univ', 'rattrapage_vcs_actif', 'rattrapage_vci_actif')


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
    list_display  = ('inscription_element', 'session', 'type_note', 'valeur', 'saisie_par')
    list_filter   = ('type_note', 'session')
    search_fields = ('inscription_element__inscription_ped__inscription_admin__etudiant__matricule',)


@admin.register(ResultatElement)
class ResultatElementAdmin(admin.ModelAdmin):
    list_display  = ('inscription_element', 'session', 'note_finale', 'est_valide', 'est_eliminatoire')
    list_filter   = ('est_valide', 'est_eliminatoire')


@admin.register(ResultatSemestre)
class ResultatSemestreAdmin(admin.ModelAdmin):
    list_display  = ('inscription_ped', 'session', 'moyenne', 'credits_valides', 'est_admis')
    list_filter   = ('est_admis',)


@admin.register(PVDeliberation)
class PVDeliberationAdmin(admin.ModelAdmin):
    list_display  = ('session', 'filiere', 'niveau', 'date_deliberation', 'est_clos')
    list_filter   = ('est_clos', 'filiere')


@admin.register(LigneDeliberation)
class LigneDeliberationAdmin(admin.ModelAdmin):
    list_display  = ('pv', 'inscription_admin', 'decision', 'moyenne_annuelle', 'credits_annuels')
    list_filter   = ('decision',)
