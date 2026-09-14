from django.urls import path
from rest_framework.routers import DefaultRouter

from .perimetre import PerimetreResponsableView

from .views import (
    AbsenceSeanceViewSet, ClassementS3ViewSet, ClasseViewSet, ConcoursCNIMViewSet,
    DeliberationViewSet,
    DocumentIPGEIViewSet,
    EmploiArchiveViewSet, EpreuveIPGEIViewSet, EvaluationViewSet,
    GrilleTypeViewSet, HistoriqueClasseViewSet, InscriptionIPGEIViewSet,
    LigneDeliberationViewSet, MatiereViewSet, MembreJuryIPGEIViewSet,
    NiveauIPGEIViewSet, NoteViewSet,
    ParametresIPGEIViewSet,
    PermutationEtudiantViewSet, PermutationProfViewSet, SeanceReelleViewSet,
    SeanceTypeViewSet, SemaineIPGEIViewSet, SemestreIPGEIViewSet,
    SessionEvaluationIPGEIViewSet, SignataireIPGEIViewSet, SousGroupeTPViewSet,
    TableauBordIPGEIViewSet,
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
# Le calendrier des epreuves : la composition elle-meme — date, heure, duree,
# classes convoquees. Ni l'emploi du temps (elle n'entre pas dans la grille de
# 1h30) ni la note (qui est par etudiant) ne savaient la porter.
router.register(r'epreuves',        EpreuveIPGEIViewSet,         basename='ipgei-epreuve')
router.register(r'deliberations',   DeliberationViewSet,         basename='ipgei-deliberation')
router.register(r'lignes-deliberation', LigneDeliberationViewSet, basename='ipgei-ligne-deliberation')
router.register(r'membres-jury',    MembreJuryIPGEIViewSet,      basename='ipgei-membre-jury')
# Concours CNIM : résultat externe, constaté puis appliqué — pas un jury.
router.register(r'concours',        ConcoursCNIMViewSet,         basename='ipgei-concours')
# Classement de S3 : feuille de travail de l'affectation, hors registre.
router.register(r'classement',      ClassementS3ViewSet,         basename='ipgei-classement')
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
router.register(r'signataires',     SignataireIPGEIViewSet,      basename='ipgei-signataire')
# Tableau de bord
router.register(r'tableau-bord',    TableauBordIPGEIViewSet,     basename='ipgei-tableau-bord')

urlpatterns = router.urls + [
    # Le perimetre disciplinaire d'un responsable : quel departement, quelles
    # matieres. Pose par l'admin, lu par le RBAC a chaque requete.
    path('perimetres/<int:user_id>/', PerimetreResponsableView.as_view(),
         name='ipgei-perimetre'),
]
