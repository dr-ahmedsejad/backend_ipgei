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


def annee_socle(annee_universitaire: str):
    """
    Inscrit l'année au registre du socle (`parametres.Year`, table `annee`) si
    elle n'y est pas, et la renvoie.

    IPGEI porte l'année en TEXTE sur ses classes, ses semestres et ses
    inscriptions ; le socle la porte en TABLE, et c'est celle-là qui est
    l'autorité — l'écran Paramètres → Années la montre, la grille tarifaire s'y
    accroche, la clôture d'exercice s'y décide.

    Ouvrir une année côté IPGEI sans l'y déclarer laissait les deux sources
    diverger en silence : `frais_pour()` cherche l'année au registre et, ne la
    trouvant pas, rendait un tarif NUL. C'est ainsi que des inscriptions se sont
    ouvertes à zéro sans que rien ne l'explique.
    """
    from apps.parametres.models import Year

    annee, _ = Year.objects.get_or_create(annee=annee_universitaire)
    return annee


def niveau_socle(libelle: str) -> Niveau:
    """`parametres.Niveau` correspondant à MPSI ou MP, créé au besoin."""
    niveau, _ = Niveau.objects.get_or_create(niveau=libelle)
    return niveau


def libelle_annee(code: str) -> str:
    """
    « 1re année » ou « 2e année » — l'année d'étude d'un semestre.

    Un semestre ne relève pas d'un niveau mais d'un RANG d'année : S3 est un
    semestre de deuxième année, suivi par MP comme par tout autre niveau du même
    rang — MPI. L'appeler « S3 — MP » devient faux dès qu'un second niveau
    partage l'année, et c'est le cas depuis que les niveaux s'administrent.
    """
    return '1re année' if code in ('S1', 'S2') else '2e année'


def rang_de_rattachement(code: str, niveau=None) -> int:
    """
    Année d'étude d'un semestre : 1 pour la première, 2 pour la deuxième.

    Le code suffit pour le cursus (S1..S4). Au-delà, on lit le rang du niveau
    auquel le semestre est rattaché — c'est là qu'un « S5 » de troisième année
    conserve son sens.
    """
    import re

    from .models import NiveauIPGEI, SEMESTRES_PAR_RANG

    code = (code or '').strip().upper()
    for rang, codes in SEMESTRES_PAR_RANG.items():
        if code in codes:
            return rang
    if niveau is not None:
        fiche = NiveauIPGEI.objects.filter(code=niveau.niveau).first()
        if fiche is not None:
            return fiche.rang
        # Rattachement créé faute de niveau du bon rang : son libellé porte
        # l'année (« 3e année »), seule trace de ce qui a été saisi.
        trouve = re.match(r'^(\d+)', (niveau.niveau or '').strip())
        if trouve:
            return int(trouve.group(1))
    return 1


def niveau_de_rattachement(code: str, annee=None):
    """
    `parametres.Niveau` auquel rattacher un semestre, ou None si indéterminable.

    Ce rattachement est une jointure interne au socle — il lui sert à rejoindre
    les départements — et non une donnée que la scolarité doit choisir. On le
    déduit donc de l'année d'étude, elle-même déduite du code quand il relève du
    cursus. Un niveau du bon rang est réutilisé s'il en existe un ; sinon une
    ligne « Ne année » est créée, faute de niveau à qui s'adosser.
    """
    from .models import NiveauIPGEI

    code = (code or '').strip().upper()
    if annee is None:
        if code not in SEMESTRES:
            return None
        return niveau_socle(SEMESTRES[code][0])

    fiche = (NiveauIPGEI.objects.filter(rang=annee, actif=True)
             .order_by('ordre', 'code').first())
    if fiche is not None:
        return niveau_socle(fiche.code)
    return niveau_socle('1re année' if annee == 1 else f'{annee}e année')


def semestre_socle(code: str) -> Semestre:
    """
    `parametres.Semestre` correspondant à S1..S4.

    L'intitulé porte l'année d'étude, pas le niveau : c'est ce qui est vrai du
    semestre lui-même, et ce qui le reste quand un niveau s'ajoute.

    `niveau_semestre` continue de pointer sur le niveau d'origine — le socle
    s'en sert pour rejoindre les départements au calcul des décalages de
    semaines. C'est une jointure technique, pas un affichage.

    `credits` reste à 0 : la prépa ne fonctionne pas en crédits ECTS mais en
    coefficients, portés par `ipgei.Matiere`. Y écrire une valeur inventée
    fausserait les écrans du socle qui les additionnent.
    """
    libelle_niveau, type_semestre = SEMESTRES[code]
    semestre, _ = Semestre.objects.get_or_create(
        code_semestre=code,
        niveau_semestre=niveau_socle(libelle_niveau),
        type_semestre=type_semestre,
        defaults={'semestre': f'{code} — {libelle_annee(code)}', 'credits': 0},
    )
    return semestre
