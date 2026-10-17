"""
Décor commun des tests du module absences à l'IPGEI (17/10/2026).

Le monde est celui de la vue de surveillance (`tests/test_ipgei_surveillance`) :
classes MPSI A / MPSI B / MP A, sous-groupes A1 et A2 de MPSI A, une semaine 5
au calendrier, des séances réelles et les lignes de suivi qu'en tire la
génération. On y ajoute des étudiants INSCRITS — l'inscription IPGEI fait foi
pour la liste d'appel, et c'est elle qui pose `Etudiant.departement`.
"""
from tests.factories.auth import UserFactory
from tests.test_ipgei_surveillance import (  # noqa: F401  (fixtures réexportées)
    ANNEE, LUNDI, api, droit, monde, seance, suivi,
)


def inscrit(monde, matricule, classe='MPSI A', sous_groupe=None, **autres):
    """Un étudiant inscrit dans une classe (et un sous-groupe) de l'année."""
    from apps.absence.models import Etudiant
    from apps.ipgei.models import InscriptionIPGEI
    etu = Etudiant.objects.create(matricule=matricule, nom=f'Etudiant {matricule}')
    InscriptionIPGEI.objects.create(
        etudiant=etu, classe=monde['classes'][classe], annee_universitaire=ANNEE,
        sous_groupe=monde['sg'][sous_groupe] if sous_groupe else None, **autres)
    etu.refresh_from_db()
    return etu


def agent_de_saisie(role='scolarite'):
    """Fait l'appel (`abs_saisie` voir + modifier), ne justifie pas — la
    scolarité de l'IPGEI telle que la matrice la règle."""
    for action in ('voir', 'modifier'):
        droit(role, 'abs_saisie', action)
    return UserFactory(username=f'u_saisie_{role}', role=role)


def superutilisateur():
    return UserFactory(username='u_super', role='admin', is_superuser=True)
