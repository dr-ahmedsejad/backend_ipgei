"""
Référentiels du socle SIGA utilisés par IPGEI.

À ne pas confondre avec un miroir : rien n'est copié ici. Ces fonctions
retrouvent — ou créent au premier appel — les quelques lignes de référentiel
que le socle attend pour rattacher une classe, une matière ou une séance :

    parametres.Niveau     MPSI, MP
    parametres.Semestre   S1..S4, impairs/pairs
    parametres.Seance     CM, TD, TP, DS
    scolarite.Filiere     « IPGEI », filière technique unique

Ces objets sont des *référentiels partagés*, pas des doublons d'entités IPGEI :
une classe n'a qu'une seule existence (voir `models.Classe`, qui hérite de
`departement.Departement`).
"""
from apps.parametres.models import Niveau, Semestre
from apps.scolarite.models import Filiere

from .models import NIVEAU_MP, NIVEAU_MPSI

# Correspondance semestre IPGEI → (niveau, type de semestre) du socle.
SEMESTRES = {
    'S1': (NIVEAU_MPSI, 'I'),
    'S2': (NIVEAU_MPSI, 'P'),
    'S3': (NIVEAU_MP,   'I'),
    'S4': (NIVEAU_MP,   'P'),
}

# Index Django du jour (0 = lundi), pour dater chaque jour d'une semaine.
INDEX_JOUR = {'lundi': 0, 'mardi': 1, 'mercredi': 2, 'jeudi': 3,
              'vendredi': 4, 'samedi': 5, 'dimanche': 6}


def filiere_socle(institution) -> Filiere:
    """
    Filière technique portant tout le cursus préparatoire.

    Le socle identifie une matière par sa filière et retrouve les matières d'un
    groupe via (filière + semestre). Laisser ce champ vide viderait donc
    l'autocomplétion « Matière » de l'écran « Gérer les emplois ». Une seule
    filière suffit : le semestre distingue déjà MPSI de MP.
    """
    filiere, _ = Filiere.objects.get_or_create(
        code='IPGEI',
        defaults={
            'intitule_fr':   'Classes préparatoires — MPSI / MP',
            'type_diplome':  'ING',
            'nb_semestres':  4,
            'niveau_debut':  1,
            'niveau_fin':    2,
            'credits_total': 0,
            'institution':   institution,
        },
    )
    return filiere


def niveau_socle(libelle: str) -> Niveau:
    """`parametres.Niveau` correspondant à MPSI ou MP, créé au besoin."""
    niveau, _ = Niveau.objects.get_or_create(niveau=libelle)
    return niveau


def semestre_socle(code: str) -> Semestre:
    """
    `parametres.Semestre` correspondant à S1..S4.

    `credits` reste à 0 : la prépa ne fonctionne pas en crédits ECTS mais en
    coefficients, portés par `ipgei.Matiere`. Y écrire une valeur inventée
    fausserait les écrans du socle qui les additionnent.
    """
    libelle_niveau, type_semestre = SEMESTRES[code]
    semestre, _ = Semestre.objects.get_or_create(
        code_semestre=code,
        niveau_semestre=niveau_socle(libelle_niveau),
        type_semestre=type_semestre,
        defaults={'semestre': f'{code} — {libelle_niveau}', 'credits': 0},
    )
    return semestre
