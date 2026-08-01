from django.contrib import admin

from .models import (
    AbsenceSeance, Classe, Deliberation, DetailLigneDeliberation, Evaluation,
    GrilleType, HistoriqueClasse, InscriptionIPGEI, LigneDeliberation, Matiere,
    Note, ParametresIPGEI, PermutationEtudiant, PermutationProf, SeanceReelle,
    SeanceType, SemaineIPGEI, SemestreIPGEI, SousGroupeTP,
)


@admin.register(ParametresIPGEI)
class ParametresIPGEIAdmin(admin.ModelAdmin):
    list_display = ('institution', 'seuil_validation', 'plafond_rattrapage',
                    'nb_semaines_defaut', 'droit_redoublement_max')


class SemaineInline(admin.TabularInline):
    model = SemaineIPGEI
    extra = 0


@admin.register(SemestreIPGEI)
class SemestreIPGEIAdmin(admin.ModelAdmin):
    list_display  = ('code', 'annee_universitaire', 'date_debut', 'date_fin',
                     'nb_semaines', 'est_cloture')
    list_filter   = ('code', 'annee_universitaire', 'est_cloture')
    inlines       = [SemaineInline]


class SousGroupeInline(admin.TabularInline):
    model = SousGroupeTP
    extra = 0
    # Une classe etant un departement, le lien de parente du sous-groupe est lui
    # aussi une cle vers Classe : il faut designer celle qui porte le
    # rattachement pedagogique.
    fk_name = 'classe'


@admin.register(Classe)
class ClasseAdmin(admin.ModelAdmin):
    # `libelle` est une propriete de lecture depuis l'heritage : le champ
    # stocke s'appelle `groupe`, et c'est lui qui doit etre cherche/filtre.
    list_display  = ('__str__', 'niveau', 'groupe', 'annee_universitaire', 'actif')
    list_filter   = ('niveau', 'annee_universitaire', 'actif')
    search_fields = ('groupe', 'nom')
    inlines       = [SousGroupeInline]


@admin.register(InscriptionIPGEI)
class InscriptionIPGEIAdmin(admin.ModelAdmin):
    list_display  = ('etudiant', 'classe', 'sous_groupe', 'statut', 'nb_redoublements', 'actif')
    list_filter   = ('classe__niveau', 'annee_universitaire', 'statut', 'actif')
    search_fields = ('etudiant__nom', 'etudiant__matricule')
    autocomplete_fields = ()


@admin.register(Matiere)
class MatiereAdmin(admin.ModelAdmin):
    list_display  = ('code', 'intitule', 'code_semestre', 'coefficient',
                     'volume_horaire', 'has_tp', 'pct_ds', 'pct_tp', 'pct_exam', 'actif')
    list_filter   = ('code_semestre', 'has_tp', 'actif')
    search_fields = ('code', 'intitule')


class EvaluationInline(admin.TabularInline):
    model = Evaluation
    extra = 0


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
    list_display  = ('inscription', 'matiere', 'semestre', 'moy_ds', 'note_tp',
                     'moy_exam', 'moyenne', 'note_retenue', 'verrouillee')
    list_filter   = ('semestre', 'matiere', 'verrouillee')
    search_fields = ('inscription__etudiant__nom', 'inscription__etudiant__matricule')
    inlines       = [EvaluationInline]
    readonly_fields = ('moy_ds', 'moy_exam', 'moyenne', 'note_retenue',
                       'pct_ds', 'pct_tp', 'pct_exam', 'coefficient', 'date_calcul')


class LigneInline(admin.TabularInline):
    model = LigneDeliberation
    extra = 0
    readonly_fields = ('moyenne_generale', 'rang', 'decision_auto')


@admin.register(Deliberation)
class DeliberationAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'niveau', 'annee_universitaire', 'portee',
                    'seuil_validation', 'statut')
    list_filter  = ('niveau', 'annee_universitaire', 'portee', 'statut')
    inlines      = [LigneInline]


@admin.register(GrilleType)
class GrilleTypeAdmin(admin.ModelAdmin):
    list_display = ('classe', 'type_semestre', 'actif')
    list_filter  = ('type_semestre', 'actif')


@admin.register(SeanceReelle)
class SeanceReelleAdmin(admin.ModelAdmin):
    list_display = ('classe', 'semaine', 'jour', 'creneau', 'matiere', 'prof', 'origine', 'annulee')
    list_filter  = ('classe', 'origine', 'annulee', 'type_seance')


@admin.register(PermutationProf)
class PermutationProfAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'statut', 'nb_semaines', 'action_directe', 'date_demande')
    list_filter  = ('statut', 'action_directe')


@admin.register(PermutationEtudiant)
class PermutationEtudiantAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'statut', 'action_directe', 'date_demande')
    list_filter  = ('statut', 'action_directe')


@admin.register(AbsenceSeance)
class AbsenceSeanceAdmin(admin.ModelAdmin):
    list_display  = ('inscription', 'seance', 'statut', 'date_saisie')
    list_filter   = ('statut',)
    search_fields = ('inscription__etudiant__nom',)


admin.site.register([
    SemaineIPGEI, SousGroupeTP, Evaluation, LigneDeliberation,
    DetailLigneDeliberation, SeanceType, HistoriqueClasse,
])
