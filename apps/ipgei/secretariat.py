"""
Pièces du secrétariat autour de l'anonymat des copies.

Deux documents, et TOUT tient dans ce qui les sépare :

  * **La liste du secrétariat** porte le couple `numéro ↔ matricule`. C'est la
    table de levée d'anonymat : elle ne quitte pas le secrétariat, et sert à
    rendre les copies à leurs auteurs une fois la correction close.

  * **La feuille du correcteur** ne porte QUE des numéros. La donner avec les
    matricules annulerait l'anonymat au moment précis où il sert — pendant la
    correction. Ce n'est pas une précaution de forme : c'est la raison d'être
    du dispositif.

Les deux se tirent de la même campagne, et l'on ne peut pas se tromper de
document : ils ne sortent pas de la même fonction.

Les numéros valent pour la SESSION, non par matière ni par classe : c'est
pourquoi la liste couvre toutes les classes d'un même niveau confondues — un
correcteur reçoit un paquet de copies, pas une classe.
"""
import io
import logging
from datetime import date

from django.core.exceptions import ValidationError
from django.db.models.functions import Length
from django.utils import timezone

from core.pdf_renderer import render_pdf_bytes
from core.pdf_utils import get_institution_context

#: `LIBELLES_EPREUVE` nomme l'épreuve en toutes lettres sur l'écran et sur la
#: fiche ; `TYPES_EPREUVE` est ré-exporté pour ses appelants. Les deux vivent
#: dans `core.types_seance`, seul endroit où ils sont écrits.
from core.types_seance import (LIBELLES_EPREUVE,         # noqa: F401
                               TYPES_EPREUVE)

from .collecte import OPTIONS_PDF, _entete_excel, _logo_reduit
from .models import AnonymatIPGEI

logger = logging.getLogger('siga')

TEMPLATE_LISTE      = 'ipgei/anonymat_liste.html'
TEMPLATE_REPARTITION = 'ipgei/anonymat_repartition.html'


#: Ce qu'écrit le Suivi quand la séance a EU LIEU. Le pointage vit sur
#: `SuiviePointage`, pas sur `Suivie` — cette dernière garde « Non fait »
#: depuis sa génération, et s'y fier ferait déclarer aucune épreuve tenue.
POINTAGE_FAIT = 'Fait'

#: Statuts qui signifient QU'AUCUNE COPIE N'EXISTE. Le retard n'en est pas :
#: l'étudiant arrivé en cours d'épreuve a composé, sa copie est dans le paquet,
#: et l'annoncer absente enverrait le correcteur chercher une feuille qu'il
#: tient en main. L'absence justifiée, elle, laisse le même vide qu'une autre
#: pour la correction — ce qu'elle change, c'est le sort de la note, décidé
#: après la levée d'anonymat, pas la présence d'une copie sur la table.
STATUTS_SANS_COPIE = ('absent', 'justifiee')

#: Les mêmes, côté SOCLE, où le statut est un entier (`absence.Presence`) :
#: 1 « Absent », 3 « Justifiée ». Le 2 « Sanctionné » n'y figure pas — il
#: qualifie une sanction, non l'absence d'une copie, et le compter ferait
#: annoncer « ABS » sur une copie posée sur la table.
STATUTS_SOCLE_SANS_COPIE = (1, 3)


def epreuves_tenues(matiere, semestre) -> list:
    """
    Les épreuves de cette matière qui ont EU LIEU — déclarées, ou déduites
    du Suivi.

    Une fiche de collecte porte sur UNE épreuve. Tant qu'on ne la désignait
    pas, les absences de toutes les épreuves du semestre se mélangeaient : un
    absent du premier devoir ressortait « ABS » sur la fiche du second, et sur
    celle de l'examen. Le correcteur cherchait alors à ne pas corriger une
    copie posée devant lui.

    Ce qui identifie une épreuve n'est pas une séance mais un COUPLE
    (type, date) : une épreuve de niveau se tient dans plusieurs classes le
    même jour, donc sur plusieurs séances. Les classes sont réunies ici, et
    c'est bien ce que couvre la fiche.

    Seules les épreuves **pointées « Fait »** sont rendues. Une séance
    planifiée n'est pas une épreuve tenue : proposer un devoir reporté, ou
    programmé pour le mois prochain, inviterait à éditer des fiches pour une
    correction qui n'a pas lieu d'être. Le pointage vit sur `SuiviePointage`,
    le seul endroit où « Fait » s'écrit.

    Renvoie `[{date, type, libelle, classes}]`, de la plus récente à la plus
    ancienne — on édite les fiches de l'épreuve qu'on vient de faire passer.
    """
    if matiere is None or semestre is None or matiere.em_miroir_id is None:
        return []
    from apps.suivi.models import SuiviePointage

    pointages = (
        SuiviePointage.objects
        .filter(em_id=matiere.em_miroir_id,
                annee_universitaire=semestre.annee_universitaire,
                type_semestre=semestre.type_semestre,
                type_seance_fk__type_seance__in=TYPES_EPREUVE,
                commentaire=POINTAGE_FAIT,
                date_suivie__isnull=False)
        .select_related('type_seance_fk')
        .prefetch_related('departements')
    )

    groupees = {}
    for pointage in pointages:
        code = pointage.type_seance_fk.type_seance
        cle = (pointage.date_suivie, code)
        entree = groupees.setdefault(cle, {
            'date':    pointage.date_suivie,
            'type':    code,
            'classes': set(),
        })
        entree['classes'].update(d.nom for d in pointage.departements.all())

    # Les épreuves DÉCLARÉES, désormais, en plus de celles déduites du pointage.
    #
    # Le groupement ci-dessus reconstitue une épreuve à partir de ses séances,
    # faute de l'avoir jamais eue sous la main. `EpreuveIPGEI` la porte enfin —
    # avec ses classes, sans reconstruction. Les deux sources cohabitent parce
    # que les semestres déjà saisis n'ont que des séances, et qu'on ne va pas
    # ressaisir leur calendrier pour rééditer une fiche.
    #
    # Ce qui rend une épreuve déclarée « tenue » est sa DATE PASSÉE. Le pointage
    # ne vaut que pour une séance, et une épreuve n'en est pas une : exiger un
    # « Fait » qui ne sera jamais écrit n'en rendrait aucune.
    from .models import EpreuveIPGEI

    aujourdhui = timezone.localdate()
    declarees = (
        EpreuveIPGEI.objects
        .filter(matiere_id=matiere.pk,
                session__semestre_id=semestre.pk,
                date__lte=aujourdhui)
        .prefetch_related('convocations__classe')
    )
    for epreuve in declarees:
        cle = (epreuve.date, epreuve.type_epreuve)
        entree = groupees.setdefault(cle, {
            'date': epreuve.date, 'type': epreuve.type_epreuve, 'classes': set(),
        })
        entree['classes'].update(c.classe.nom for c in epreuve.convocations.all())

    epreuves = []
    for entree in groupees.values():
        classes = sorted(entree['classes'])
        epreuves.append({
            'date':    entree['date'].isoformat(),
            'type':    entree['type'],
            'classes': classes,
            'libelle': f"{LIBELLES_EPREUVE.get(entree['type'], entree['type'])} "
                       f"du {entree['date'].strftime('%d/%m/%Y')}"
                       + (f" — {', '.join(classes)}" if classes else ''),
        })
    return sorted(epreuves, key=lambda e: (e['date'], e['type']), reverse=True)


def _libelle_epreuve(epreuve) -> str:
    """
    « Devoir surveillé du 12/03/2027 » — ce que la fiche imprime.

    Vide quand aucune épreuve n'est désignée : la fiche reste alors ce qu'elle
    était, un document qui ne dit pas sur quoi il porte. On ne le remplace pas
    par une mention fantaisiste.
    """
    if not epreuve or not epreuve.get('type'):
        return ''
    intitule = LIBELLES_EPREUVE.get(epreuve['type'], epreuve['type'])
    brute = epreuve.get('date')
    if not brute:
        return intitule
    jour = date.fromisoformat(brute) if isinstance(brute, str) else brute
    return f"{intitule} du {jour.strftime('%d/%m/%Y')}"


def _absents_socle(matiere, semestre, types=TYPES_EPREUVE, date=None) -> dict:
    """
    Les absents relevés par le SUIVI — l'autre moitié du dispositif.

    L'établissement dispose de deux chemins pour pointer une épreuve, et tous
    deux sont légitimes :

      * la feuille d'appel de la séance, côté IPGEI (`ipgei_absence`) ;
      * le pointage du Suivi, côté socle (`absence_presence`), qui s'accroche
        à la ligne de Suivi produite pour la séance.

    Ils n'écrivent pas dans la même table. Ne lire que l'un revenait à imprimer
    une fiche muette pour une scolarité qui pointe par l'autre — et rien ne
    l'aurait signalé : la fiche serait simplement sortie sans « ABS », comme si
    tout le monde avait composé.

    Le raccord se fait par le miroir de la matière (`em_miroir`), par lequel
    l'emploi du temps IPGEI est déjà projeté dans le Suivi.

    Renvoie `{inscription_id: statut}`, avec les libellés IPGEI, pour que
    l'appelant n'ait pas à savoir de quel côté vient la ligne.
    """
    if matiere is None or semestre is None or matiere.em_miroir_id is None:
        return {}
    from apps.absence.models import Presence

    from .models import InscriptionIPGEI

    presences = (
        Presence.objects
        .filter(suivi__em_id=matiere.em_miroir_id,
                suivi__annee_universitaire=semestre.annee_universitaire,
                suivi__type_semestre=semestre.type_semestre,
                suivi__type_seance_fk__type_seance__in=types,
                statut__in=STATUTS_SOCLE_SANS_COPIE)
        .values_list('etudiant_id', 'statut')
    )
    # L'épreuve désignée, et elle seule : sans cette date, les absents des
    # autres devoirs du semestre remonteraient sur la même fiche.
    if date:
        presences = presences.filter(suivi__date_suivie=date)
    if not presences:
        return {}

    # Le socle pointe des ÉTUDIANTS, la fiche parle d'INSCRIPTIONS : c'est
    # l'inscription qui porte le numéro d'anonymat, un étudiant pouvant en
    # avoir plusieurs au fil des années.
    par_etudiant = dict(
        InscriptionIPGEI.objects
        .filter(annee_universitaire=semestre.annee_universitaire, actif=True)
        .values_list('etudiant_id', 'id')
    )
    correspondance = {1: 'absent', 3: 'justifiee'}
    return {
        par_etudiant[etudiant]: correspondance.get(statut, 'absent')
        for etudiant, statut in presences if etudiant in par_etudiant
    }


def absents(matiere, semestre, types=TYPES_EPREUVE, date=None) -> dict:
    """
    Inscriptions absentes à l'épreuve, avec le statut relevé à l'émargement.

    L'absence est saisie sur la SÉANCE — l'épreuve planifiée à l'emploi du
    temps, pointée le jour même : qui n'a pas signé est saisi, les autres sont
    présents par défaut. Il ne reste qu'à la rattacher à la copie, ce que fait
    la chaîne `absence → inscription → anonymat → numéro`.

    On ne retient que les séances d'ÉPREUVE de cette matière et de ce semestre.
    Une absence à un cours ne dit rien de la copie ; la compter marquerait
    absent un étudiant qui a composé.

    `types` et `date` DÉSIGNENT L'ÉPREUVE. Sans eux, toutes celles du semestre
    se confondent : l'absent du premier devoir ressort « ABS » sur la fiche du
    second et sur celle de l'examen. Les laisser ouverts reste possible — pour
    un relevé d'ensemble —, mais une fiche de collecte doit toujours les
    passer.

    Les DEUX chemins de pointage sont lus — la feuille d'appel de la séance et
    le pointage du Suivi (voir `_absents_socle`). L'établissement peut utiliser
    l'un, l'autre, ou les deux selon les épreuves ; la fiche doit être juste
    dans les trois cas.

    Renvoie `{inscription_id: statut}` — le statut est conservé pour que la
    suite (report des zéros, jury) puisse traiter l'absence justifiée
    autrement ; ici, les deux valent pareil : pas de copie.
    """
    if matiere is None or semestre is None:
        return {}
    from .models import AbsenceSeance

    manquants = _absents_socle(matiere, semestre, types, date)
    # La feuille d'appel IPGEI passe en dernier : c'est la saisie la plus
    # proche de la salle, et si les deux se contredisent, c'est elle qui a vu
    # la copie — ou son absence.
    cote_ipgei = (
        AbsenceSeance.objects
        .filter(seance__matiere=matiere,
                seance__semaine__semestre=semestre,
                seance__type_seance__type_seance__in=types,
                statut__in=STATUTS_SANS_COPIE)
    )
    if date:
        cote_ipgei = cote_ipgei.filter(seance__date=date)
    manquants.update(
        cote_ipgei.values_list('inscription_id', 'statut')
    )
    return manquants


def est_anonyme(epreuve) -> bool:
    """
    Cette épreuve se corrige-t-elle sous anonymat ?

    **Le devoir surveillé, non.** Il se tient en classe, l'enseignant corrige
    ses propres élèves, et il rend les copies au cours suivant : masquer les
    noms n'y protège personne et oblige à une levée d'anonymat pour rien. Les
    numéros sont faits pour l'examen, où plusieurs correcteurs se partagent une
    pile venue de plusieurs classes.

    Sans épreuve désignée, on reste sur l'anonymat : c'est le cas le plus
    exigeant, et se tromper dans ce sens ne divulgue rien.
    """
    if not epreuve or not epreuve.get('type'):
        return True
    return epreuve['type'] in EPREUVES_ANONYMES


#: Les épreuves qui se corrigent sous anonymat. Le devoir surveillé n'en est
#: pas : voir `est_anonyme`.
EPREUVES_ANONYMES = ('EF', 'ER')


def lignes(session, niveau: str = '', matiere=None, epreuve=None) -> list:
    """
    Les copies à corriger, dans l'ordre où on les distribue.

    Deux formes, selon l'épreuve :

      * **sous anonymat** (examen, rattrapage) — une ligne par numéro tiré,
        rangée par numéro. Le nom n'y figure jamais ;
      * **nominative** (devoir surveillé) — une ligne par inscrit du niveau,
        rangée par MATRICULE croissant. Le correcteur y écrit ses notes en
        face des noms.

    Toutes les classes du niveau sont confondues — c'est ainsi qu'on répartit
    des copies. `niveau` restreint quand deux niveaux partagent le semestre
    (MPSI et MPI en première année) et qu'on ne corrige que l'un des deux.

    `matiere` fournie, chaque ligne porte en plus `absent` et `statut_absence` :
    l'émargement de l'épreuve est reporté sur la copie, et le correcteur sait
    qu'il ne cherchera pas cette copie dans son paquet.
    """
    from .models import InscriptionIPGEI

    # `epreuve` = {'type': 'DS', 'date': '2027-03-12'} — l'épreuve désignée sur
    # l'écran, prise parmi celles que le Suivi déclare tenues.
    manquants = absents(
        matiere, session.semestre,
        types=(epreuve['type'],) if epreuve and epreuve.get('type') else TYPES_EPREUVE,
        date=epreuve.get('date') if epreuve else None)

    if not est_anonyme(epreuve):
        # Le devoir surveillé n'exige AUCUN tirage : la liste est celle des
        # inscrits. Exiger des numéros pour un devoir bloquait l'édition d'une
        # feuille qui n'en a jamais eu besoin.
        # Rangée par MATRICULE croissant, toutes classes confondues : c'est
        # l'ordre dans lequel la scolarité relit une pile et retrouve un
        # étudiant d'un coup d'œil. La colonne « Classe » reste là pour savoir
        # à qui rendre la copie.
        #
        # Le tri passe par la LONGUEUR avant la valeur : le matricule est un
        # texte, et « 9001 » se rangerait après « 26001 » en ordre purement
        # alphabétique. Ils ont tous cinq chiffres aujourd'hui — rien ne dit
        # qu'une reprise d'historique n'en apportera pas de plus courts.
        inscrits = (InscriptionIPGEI.objects
                    .filter(actif=True,
                            annee_universitaire=session.semestre.annee_universitaire)
                    .select_related('etudiant', 'classe')
                    .order_by(Length('etudiant__matricule'),
                              'etudiant__matricule'))
        if niveau:
            inscrits = inscrits.filter(classe__niveau__niveau=niveau)
        return [
            {
                'numero':    None,
                'matricule': inscription.etudiant.matricule,
                'nom':       inscription.etudiant.nom_display,
                'classe':    inscription.classe.nom,
                'absent':    inscription.pk in manquants,
                'statut_absence': manquants.get(inscription.pk, ''),
            }
            for inscription in inscrits
        ]

    requete = (AnonymatIPGEI.objects
               .filter(session=session)
               .select_related('inscription__etudiant', 'inscription__classe')
               .order_by('numero'))
    if niveau:
        requete = requete.filter(inscription__classe__niveau__niveau=niveau)

    return [
        {
            'numero':    ligne.numero,
            'matricule': ligne.inscription.etudiant.matricule,
            'nom':       ligne.inscription.etudiant.nom_display,
            'classe':    ligne.inscription.classe.nom,
            'absent':    ligne.inscription_id in manquants,
            'statut_absence': manquants.get(ligne.inscription_id, ''),
        }
        for ligne in requete
    ]


def _contexte(session, niveau: str, copies: list, epreuve=None) -> dict:
    donnees = get_institution_context()
    semestre = session.semestre
    donnees.update({
        # Les gabarits changent de colonnes selon la forme : numéro d'anonymat,
        # ou matricule et nom. Un seul drapeau, décidé ici.
        'anonyme':             est_anonyme(epreuve),
        'session':             session,
        'semestre':            semestre,
        'annee_universitaire': semestre.annee_universitaire,
        'niveau':              niveau or 'tous niveaux',
        'campagne':            session.get_type_session_display(),
        'copies':              copies,
        'effectif':            len(copies),
        'date_edition':        date.today().strftime('%d/%m/%Y'),
    })
    return donnees


# ═════════════════════════════════════════════════════════════════════════════
# Liste du secrétariat — numéro ↔ matricule
# ═════════════════════════════════════════════════════════════════════════════
def _refuser_si_nominative(epreuve):
    """
    La table de levée d'anonymat n'a d'objet que s'il y a un anonymat à lever.

    Pour un devoir surveillé, elle se résumerait à la liste de la classe — un
    document qui existe déjà ailleurs, et dont le titre « Correspondance
    anonymat — matricule » serait un mensonge.
    """
    if not est_anonyme(epreuve):
        raise ValidationError(
            "Un devoir surveillé ne se corrige pas sous anonymat : il n'y a "
            "pas de correspondance à éditer. Les fiches des correcteurs, "
            'elles, portent directement les noms.')


def generer_pdf(session, niveau: str = '', matiere=None, epreuve=None) -> bytes:
    _refuser_si_nominative(epreuve)
    copies = lignes(session, niveau, matiere, epreuve)
    if not copies:
        raise ValidationError(_rien_a_editer(session, niveau))
    return render_pdf_bytes(TEMPLATE_LISTE, _contexte(session, niveau, copies),
                            OPTIONS_PDF)


def generer_excel(session, niveau: str = '', matiere=None, epreuve=None) -> bytes:
    """
    Deux colonnes, numéro et matricule, et rien d'autre.

    Le classeur est fait pour être trié et recherché — c'est ce qu'on demande à
    une table de correspondance. Le nom de la classe suit en troisième colonne :
    il ne lève pas l'anonymat davantage que le matricule, et évite au
    secrétariat de croiser une seconde liste pour savoir où rendre la copie.
    """
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    _refuser_si_nominative(epreuve)
    copies = lignes(session, niveau, matiere, epreuve)
    if not copies:
        raise ValidationError(_rien_a_editer(session, niveau))

    donnees = _contexte(session, niveau, copies)
    VERT = '006633'
    trait = Side(border_style='thin', color='999999')
    bordure = Border(left=trait, right=trait, top=trait, bottom=trait)
    centre = Alignment(horizontal='center', vertical='center')

    classeur = openpyxl.Workbook()
    feuille = classeur.active
    feuille.title = 'Anonymat'
    entetes = ['N° anonymat', 'Matricule', 'Classe']
    for index, largeur in enumerate((16, 18, 14), 1):
        feuille.column_dimensions[get_column_letter(index)].width = largeur

    rang = _entete_excel(feuille, donnees, len(entetes), _logo_reduit(donnees))

    titre = feuille.cell(rang, 1, 'Correspondance anonymat — matricule')
    titre.font = Font(bold=True, size=12, color=VERT)
    titre.alignment = Alignment(horizontal='center')
    feuille.merge_cells(start_row=rang, start_column=1,
                        end_row=rang, end_column=len(entetes))
    rang += 2

    for etiquette, valeur in (
        ('Semestre',       donnees['semestre'].code),
        ('Campagne',       donnees['campagne']),
        ('Niveau',         donnees['niveau']),
        ('Année',          donnees['annee_universitaire']),
        ('Copies',         str(donnees['effectif'])),
        ("Date d'édition", donnees['date_edition']),
    ):
        feuille.cell(rang, 1, f'{etiquette} :').font = Font(bold=True)
        feuille.cell(rang, 2, valeur)
        rang += 1

    ligne_entete = rang + 1
    for colonne, entete in enumerate(entetes, 1):
        cellule = feuille.cell(ligne_entete, colonne, entete)
        cellule.font = Font(bold=True, color='FFFFFF')
        cellule.fill = PatternFill('solid', fgColor=VERT)
        cellule.alignment = centre
        cellule.border = bordure

    rang = ligne_entete + 1
    for copie in copies:
        for colonne, valeur in enumerate(
            (copie['numero'], copie['matricule'], copie['classe']), 1,
        ):
            cellule = feuille.cell(rang, colonne, valeur)
            cellule.border = bordure
            if colonne < 3:
                cellule.alignment = centre
        rang += 1

    feuille.freeze_panes = feuille.cell(ligne_entete + 1, 1)
    tampon = io.BytesIO()
    classeur.save(tampon)
    return tampon.getvalue()


def _rien_a_editer(session, niveau: str, epreuve=None) -> str:
    quoi = f' en {niveau}' if niveau else ''
    if not est_anonyme(epreuve):
        # Un devoir surveillé n'attend aucun tirage : s'il n'y a rien à
        # corriger, c'est qu'aucun étudiant n'est inscrit.
        return (f'Aucun inscrit{quoi} en '
                f'{session.semestre.annee_universitaire}. Un devoir surveillé '
                "se corrige sur la liste de la classe, il n'exige aucun "
                "tirage d'anonymat.")
    return (f"Aucun numéro d'anonymat tiré pour "
            f'{session.get_type_session_display().lower()} de '
            f'{session.semestre.code}{quoi}. Le tirage se fait depuis '
            'Notes → Anonymat des copies, avant la correction.')


# ═════════════════════════════════════════════════════════════════════════════
# Répartition des copies entre correcteurs
# ═════════════════════════════════════════════════════════════════════════════
def repartir(session, lots: list, niveau: str = '', matiere=None, epreuve=None) -> dict:
    """
    Partage les copies entre correcteurs, par paquets consécutifs.

    `lots` : `[{'correcteur': 'Nom', 'nb': 20}, …]`. Les paquets suivent l'ordre
    des numéros — c'est ainsi qu'on distribue une pile de copies, et cela permet
    de dire « de 1 à 20 » plutôt que d'énumérer vingt nombres.

    Le total demandé n'a pas à couvrir toute la pile : le reste est RENDU dans
    le bilan plutôt que réparti d'office. Un secrétariat qui garde vingt copies
    pour un correcteur qui n'est pas encore désigné doit pouvoir le faire — mais
    il doit le voir.

    Demander PLUS de copies qu'il n'en existe est en revanche refusé : servir
    les premiers et laisser les derniers sans rien, en silence, ferait croire à
    une répartition complète.
    """
    copies = lignes(session, niveau, matiere, epreuve)
    if not copies:
        raise ValidationError(_rien_a_editer(session, niveau, epreuve))
    anonyme = est_anonyme(epreuve)

    propres = []
    for lot in lots:
        nom = (lot.get('correcteur') or '').strip()
        try:
            nb = int(lot.get('nb') or 0)
        except (TypeError, ValueError):
            raise ValidationError({'lots': f'Nombre de copies invalide pour « {nom} ».'})
        if not nom:
            raise ValidationError({'lots': 'Chaque lot doit nommer son correcteur.'})
        if nb <= 0:
            raise ValidationError({'lots': f'« {nom} » : le nombre de copies doit '
                                           'être supérieur à zéro.'})
        propres.append({'correcteur': nom, 'nb': nb})
    if not propres:
        raise ValidationError({'lots': 'Aucun correcteur désigné.'})

    demande = sum(lot['nb'] for lot in propres)
    if demande > len(copies):
        raise ValidationError(
            {'lots': f'{demande} copies demandées pour {len(copies)} disponibles. '
                     f'Il en manque {demande - len(copies)}.'})

    paquets, curseur = [], 0
    for lot in propres:
        tranche = copies[curseur:curseur + lot['nb']]
        curseur += lot['nb']
        # Ce qui BORNE le paquet, dans l'ordre où la liste est rangée : le
        # numéro sous anonymat, le MATRICULE sinon. Borner par les noms sur une
        # liste triée par matricule donnait un intervalle qu'on ne pouvait pas
        # vérifier — « de Mariem à Yahya » ne se retrouve pas dans une pile
        # rangée par numéro d'étudiant.
        borne = (lambda c: c['numero']) if anonyme else (lambda c: c['matricule'])
        paquets.append({
            'correcteur': lot['correcteur'],
            'nb':         len(tranche),
            'premier':    borne(tranche[0]),
            'dernier':    borne(tranche[-1]),
            # Les NUMÉROS seuls : la feuille remise au correcteur ne doit
            # porter aucun matricule, sans quoi l'anonymat tombe au moment
            # même où il sert. L'absence, elle, ne dit rien de l'identité —
            # seulement qu'aucune copie ne porte ce numéro.
            'numeros':    [c['numero'] for c in tranche],
            'lignes':     [{'numero': c['numero'], 'absent': c['absent'],
                            'matricule': c['matricule'], 'nom': c['nom'],
                            'classe': c['classe']}
                           for c in tranche],
            'absents':    sum(1 for c in tranche if c['absent']),
            # Calculé ici, et non dans le gabarit : le filtre `add` de Django
            # ne soustrait pas, il concatène — « 25-3 » se serait imprimé tel quel.
            'a_corriger': len(tranche) - sum(1 for c in tranche if c['absent']),
        })
    return {
        'paquets':    paquets,
        'anonyme':    anonyme,
        'total':      len(copies),
        'reparties':  curseur,
        # Ce qui n'a pas été distribué, désigné comme le reste : des numéros
        # sous anonymat, des noms sinon — une liste de « None » n'aurait rien
        # appris au secrétariat.
        'restantes':  [(c['numero'] if anonyme else c['matricule'])
                       for c in copies[curseur:]],
        'absents':    sum(1 for c in copies if c['absent']),
    }


def generer_repartition_pdf(session, lots: list, niveau: str = '',
                            matiere=None, epreuve=None) -> bytes:
    """
    Une FICHE DE COLLECTE par correcteur : deux colonnes, numéro et note.

    Ce n'est pas une liste à cocher mais la feuille sur laquelle il écrit ses
    notes en corrigeant, puis qu'il signe et rend. D'où la colonne vide, et une
    ligne par copie plutôt qu'une grille de numéros.

    Aucun matricule n'y figure — c'est la règle de tout ce module.
    """
    bilan = repartir(session, lots, niveau, matiere, epreuve)
    donnees = _contexte(session, niveau, [], epreuve)
    donnees.update(bilan)
    donnees['matiere'] = matiere
    # La fiche DIT de quelle épreuve elle parle. Deux fiches de devoir étaient
    # jusqu'ici des documents identiques, impossibles à distinguer une fois
    # posées sur un bureau.
    donnees['epreuve'] = _libelle_epreuve(epreuve)
    return render_pdf_bytes(TEMPLATE_REPARTITION, donnees, OPTIONS_PDF)


def generer_repartition_excel(session, lots: list, niveau: str = '',
                              matiere=None, epreuve=None) -> bytes:
    """
    Le même partage en classeur — un onglet par correcteur.

    Le format est CELUI de la fiche de collecte anonyme : mêmes en-têtes de
    colonnes, même bloc d'identification. Ce n'est pas une coïncidence — la
    feuille remplie se REVERSE par Notes → Import Excel, sans format de plus à
    documenter. C'est ce qui ferme la boucle : on répartit, on corrige, on
    reverse.

    La matière est donc EXIGÉE ici, alors qu'elle reste facultative sur le PDF :
    l'import la lit dans le bloc d'identification, et sans elle le classeur
    reviendrait illisible.
    """
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    if matiere is None:
        raise ValidationError(
            {'matiere': "La matière est requise pour le classeur : c'est elle "
                        "que l'import lit pour savoir où verser les notes. Le "
                        "PDF, lui, s'édite sans."})

    bilan = repartir(session, lots, niveau, matiere, epreuve)
    donnees = _contexte(session, niveau, [], epreuve)
    VERT = '006633'
    trait = Side(border_style='thin', color='999999')
    bordure = Border(left=trait, right=trait, top=trait, bottom=trait)
    centre = Alignment(horizontal='center', vertical='center')
    logo = _logo_reduit(donnees)

    classeur = openpyxl.Workbook()
    classeur.remove(classeur.active)
    # La PREMIÈRE colonne est celle que l'import relit pour reconnaître
    # l'étudiant : le numéro sous anonymat, le matricule sinon. Le nom suit,
    # pour que le correcteur sache devant qui il met sa note.
    anonyme = donnees['anonyme']
    entetes = (['N° anonymat', 'Note /20', 'Observations'] if anonyme
               else ['Matricule', 'Nom', 'Classe', 'Note /20', 'Observations'])
    largeurs = (16, 12, 34) if anonyme else (14, 30, 12, 12, 26)

    for paquet in bilan['paquets']:
        feuille = classeur.create_sheet(_onglet(paquet['correcteur'], classeur))
        for index, largeur in enumerate(largeurs, 1):
            feuille.column_dimensions[get_column_letter(index)].width = largeur

        rang = _entete_excel(feuille, donnees, len(entetes), logo)
        titre = feuille.cell(
            rang, 1,
            'Fiche de collecte — copies anonymes' if anonyme
            else 'Fiche de collecte — devoir surveillé')
        titre.font = Font(bold=True, size=12, color=VERT)
        titre.alignment = Alignment(horizontal='center')
        feuille.merge_cells(start_row=rang, start_column=1,
                            end_row=rang, end_column=len(entetes))
        rang += 2

        # « Matière » n'est pas décoratif : c'est la ligne que relit l'import.
        # « Épreuve » n'est pas décoratif non plus : c'est ce qui distingue le
        # classeur du devoir précédent, dans une boîte de réception où ils
        # portent le même nom de fichier à un mot près.
        for etiquette, valeur in (
            ('Correcteur',     paquet['correcteur']),
            ('Matière',        matiere.intitule),
            ('Épreuve',        _libelle_epreuve(epreuve) or '—'),
            ('Semestre',       donnees['semestre'].code),
            ('Campagne',       donnees['campagne']),
            ('Copies',         f"{paquet['nb']} — "
                               + (f"du n° {paquet['premier']} au n° {paquet['dernier']}"
                                  if anonyme
                                  else f"du matricule {paquet['premier']} "
                                       f"au {paquet['dernier']}")),
            ('Année',          donnees['annee_universitaire']),
            ("Date d'édition", donnees['date_edition']),
        ):
            feuille.cell(rang, 1, f'{etiquette} :').font = Font(bold=True)
            feuille.cell(rang, 2, valeur)
            feuille.merge_cells(start_row=rang, start_column=2,
                                end_row=rang, end_column=len(entetes))
            rang += 1

        ligne_entete = rang + 1
        for colonne, entete in enumerate(entetes, 1):
            cellule = feuille.cell(ligne_entete, colonne, entete)
            cellule.font = Font(bold=True, color='FFFFFF')
            cellule.fill = PatternFill('solid', fgColor=VERT)
            cellule.alignment = centre
            cellule.border = bordure

        rang = ligne_entete + 1
        for ligne in paquet['lignes']:
            # « ABS » écrit dans la case note, et non dans les observations :
            # c'est la colonne que l'import relit, et l'absence y vaut zéro
            # comme une épreuve manquée. Le correcteur voit du premier coup
            # qu'il ne cherchera pas cette copie dans son paquet.
            marque = 'ABS' if ligne['absent'] else None
            valeurs = ((ligne['numero'], marque, None) if anonyme
                       else (ligne['matricule'], ligne['nom'], ligne['classe'],
                             marque, None))
            colonne_note = 2 if anonyme else 4
            for colonne, valeur in enumerate(valeurs, 1):
                cellule = feuille.cell(rang, colonne, valeur)
                cellule.border = bordure
                # Le nom se lit à gauche ; les codes et les notes, centrés.
                if colonne != (2 if not anonyme else 3):
                    cellule.alignment = centre
                if colonne == colonne_note and ligne['absent']:
                    cellule.font = Font(bold=True, color='B45309')
            rang += 1

        rang += 2
        feuille.cell(rang, 1, "Signature du correcteur").font =             Font(bold=True, size=9, color='555555')
        feuille.freeze_panes = feuille.cell(ligne_entete + 1, 1)

    if not classeur.sheetnames:
        classeur.create_sheet('Vide')
    tampon = io.BytesIO()
    classeur.save(tampon)
    return tampon.getvalue()


def _onglet(correcteur: str, classeur) -> str:
    """
    Nom d'onglet tiré du correcteur — Excel refuse `: \ / ? * [ ]` et plus de
    31 caractères, et deux homonymes ne peuvent pas partager un nom.
    """
    propre = ''.join(c for c in correcteur if c not in ':\/?*[]')[:28].strip()
    base = propre or 'Correcteur'
    nom, suite = base, 1
    while nom in classeur.sheetnames:
        suite += 1
        nom = f'{base}_{suite}'
    return nom
