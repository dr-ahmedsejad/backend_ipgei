from rest_framework.routers import DefaultRouter

from .views import (
    AbsenceSeanceViewSet, ClasseViewSet, DeliberationViewSet, DocumentIPGEIViewSet,
    EmploiArchiveViewSet, EvaluationViewSet,
    GrilleTypeViewSet, HistoriqueClasseViewSet, InscriptionIPGEIViewSet,
    LigneDeliberationViewSet, MatiereViewSet, MembreJuryIPGEIViewSet,
    NiveauIPGEIViewSet, NoteViewSet,
    ParametresIPGEIViewSet,
    PermutationEtudiantViewSet, PermutationProfViewSet, SeanceReelleViewSet,
    SeanceTypeViewSet, SemaineIPGEIViewSet, SemestreIPGEIViewSet,
    SessionEvaluationIPGEIViewSet, SousGroupeTPViewSet, TableauBordIPGEIViewSet,
)

router = DefaultRouter()
# Paramétrage & calendrier
router.register(r'parametres',      ParametresIPGEIViewSet,      basename='ipgei-parametres')
router.register(r'niveaux',         NiveauIPGEIViewSet,          basename='ipgei-niveau')
router.register(r'semestres',       SemestreIPGEIViewSet,        basename='ipgei-semestre')
router.register(r'semaines',        SemaineIPGEIViewSet,         basename='ipgei-semaine')
# Structure
router.register(r'classes',         ClasseViewSet,               basename='ipgei-classe')
router.register(r'sous-groupes',    SousGroupeTPViewSet,         basename='ipgei-sous-groupe')
router.register(r'inscriptions',    InscriptionIPGEIViewSet,     basename='ipgei-inscription')
router.register(r'historique-classes', HistoriqueClasseViewSet,  basename='ipgei-historique-classe')
# Académique
router.register(r'matieres',        MatiereViewSet,              basename='ipgei-matiere')
router.register(r'sessions',        SessionEvaluationIPGEIViewSet, basename='ipgei-session')
router.register(r'notes',           NoteViewSet,                 basename='ipgei-note')
router.register(r'evaluations',     EvaluationViewSet,           basename='ipgei-evaluation')
router.register(r'deliberations',   DeliberationViewSet,         basename='ipgei-deliberation')
router.register(r'lignes-deliberation', LigneDeliberationViewSet, basename='ipgei-ligne-deliberation')
router.register(r'membres-jury',    MembreJuryIPGEIViewSet,      basename='ipgei-membre-jury')
# EDT & permutations
router.register(r'grilles',         GrilleTypeViewSet,           basename='ipgei-grille')
router.register(r'seances-type',    SeanceTypeViewSet,           basename='ipgei-seance-type')
router.register(r'seances',         SeanceReelleViewSet,         basename='ipgei-seance')
router.register(r'archives-edt',    EmploiArchiveViewSet,        basename='ipgei-archive-edt')
router.register(r'absences',        AbsenceSeanceViewSet,        basename='ipgei-absence')
router.register(r'permutations-prof',     PermutationProfViewSet,     basename='ipgei-permutation-prof')
router.register(r'permutations-etudiant', PermutationEtudiantViewSet, basename='ipgei-permutation-etudiant')
# Documents officiels
router.register(r'documents',       DocumentIPGEIViewSet,        basename='ipgei-document')
# Tableau de bord
router.register(r'tableau-bord',    TableauBordIPGEIViewSet,     basename='ipgei-tableau-bord')

urlpatterns = router.urls
