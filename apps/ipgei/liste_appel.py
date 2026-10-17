"""
La liste d'appel d'une séance à l'IPGEI — qui doit être là (17/10/2026).

Le socle liste les étudiants d'une séance par `Etudiant.departement`, que
`InscriptionIPGEI.save` aligne sur la CLASSE. Une séance de TP se tient sur un
SOUS-GROUPE — un département à part entière (`models.SousGroupeTP`) — et sa
liste était donc vide : l'appel des TP était impossible, à l'écran comme sur la
fiche PDF, et la vue de surveillance le disait (« Appel par sous-groupe
indisponible »). Au 17/10/2026 : 16 séances de TP générées, 188 au planning.

La règle est celle de l'application enseignant (`apps/absence/enseignant.py`),
qui lisait déjà juste : les inscriptions IPGEI ACTIVES de l'année —

  * de la classe, pour une séance de classe ;
  * du sous-groupe, pour une séance de TP ;
  * auditeurs libres compris (ils suivent les cours) ;
  * abandons et réorientations ÉCARTÉS (`STATUTS_ECARTES`) : le statut de
    l'inscription les reconnaît. Le modèle ne date pas la décision : un
    étudiant déclaré en abandon sort de la liste de TOUTES les séances de
    l'année, passées comprises. Les écrans gardent donc à l'affichage qui a
    déjà un pointage sur la séance (« hors liste ») : on ne cache pas une
    présence enregistrée.

Il n'y a ni dette ni crédit à l'IPGEI (`services.inscrire_aux_matieres`) : un
étudiant de MP ne doit aucune matière de MPSI, et la liste d'une séance ne
dépend pas de sa matière.

Un département qui n'est ni une classe ni un sous-groupe (rien de tel n'est
planifié à l'IPGEI) garde la règle du socle, `Etudiant.departement` — signalée
par `source = SOURCE_GROUPE`.

Rien ici n'écrit.
"""
from .models import Classe, InscriptionIPGEI, SousGroupeTP

SOURCE_CLASSE = 'classe'
SOURCE_SOUS_GROUPE = 'sous_groupe'
SOURCE_GROUPE = 'groupe'

#: Les inscriptions qui ne sont plus appelées : l'étudiant a quitté la classe.
STATUTS_ECARTES = (InscriptionIPGEI.STATUT_ABANDON, InscriptionIPGEI.STATUT_REORIENTE)

MENTION_AUDITEUR = 'auditeur libre'


def ordre_matricule(matricule):
    """Matricules en NOMBRE (« 9999 » avant « 10000 ») ; un matricule non
    numérique passe après, par ordre alphabétique."""
    m = (matricule or '').strip()
    return (0, int(m), '') if m.isdigit() else (1, 0, m)


def nature(departement_id) -> str:
    """`SOURCE_CLASSE`, `SOURCE_SOUS_GROUPE` ou `SOURCE_GROUPE`."""
    if SousGroupeTP.objects.filter(pk=departement_id).exists():
        return SOURCE_SOUS_GROUPE
    if Classe.objects.filter(pk=departement_id).exists():
        return SOURCE_CLASSE
    return SOURCE_GROUPE


def unites(departement_id) -> list:
    """Les départements dont les séances relèvent de ce choix d'écran : une
    classe ET ses sous-groupes (ses TP), ou le département seul.

    Choisir « MPSI A » dans un écran d'appel montre ainsi ses TP — on n'avait
    aucun moyen de les atteindre."""
    ids = [departement_id]
    ids += list(SousGroupeTP.objects.filter(classe_id=departement_id)
                .values_list('pk', flat=True))
    return ids


def inscriptions_appelees(departement_id, annee=None):
    """Les inscriptions appelées d'une classe ou d'un sous-groupe (queryset),
    ou None pour un autre département."""
    source = nature(departement_id)
    if source == SOURCE_GROUPE:
        return None
    qs = InscriptionIPGEI.objects.filter(actif=True).exclude(statut__in=STATUTS_ECARTES)
    if source == SOURCE_SOUS_GROUPE:
        qs = qs.filter(sous_groupe_id=departement_id)
    else:
        qs = qs.filter(classe_id=departement_id)
    if annee:
        qs = qs.filter(annee_universitaire=annee)
    return qs


def liste_appel(departement_id, annee=None) -> dict:
    """Qui doit figurer à l'appel d'une séance de ce département.

    Rend {'source': …, 'etudiants': [Etudiant…]} — chaque étudiant porte
    `mention` (« auditeur libre » ou vide), dans l'ordre des matricules.
    """
    from apps.absence.models import Etudiant

    inscriptions = inscriptions_appelees(departement_id, annee)
    if inscriptions is None:
        etudiants = list(Etudiant.objects.filter(departement_id=departement_id))
        for e in etudiants:
            e.mention = ''
        source = SOURCE_GROUPE
    else:
        etudiants = []
        for i in inscriptions.select_related('etudiant'):
            e = i.etudiant
            e.mention = MENTION_AUDITEUR if i.auditeur_libre else ''
            etudiants.append(e)
        source = nature(departement_id)
    etudiants.sort(key=lambda e: ordre_matricule(e.matricule))
    return {'source': source, 'etudiants': etudiants}


def resume(e) -> dict:
    """Une ligne de liste d'appel, telle que les écrans la lisent."""
    return {'id': e.id, 'matricule': e.matricule, 'nom': e.nom, 'genre': e.genre,
            'mention': getattr(e, 'mention', '')}
