"""Factories pour apps.scolarite : Filiere, DepartementAcademique, Etudiant."""
import factory
from factory.django import DjangoModelFactory

from .parametres import InstitutionFactory


class DepartementAcademiqueFactory(DjangoModelFactory):
    class Meta:
        model = 'scolarite.DepartementAcademique'
        django_get_or_create = ('code',)

    code        = 'INFO'
    intitule_fr = 'Informatique'
    institution = factory.SubFactory(InstitutionFactory)


class FiliereFactory(DjangoModelFactory):
    """Filiere par defaut : Licence Professionnelle (LP) — Arrete 562."""
    class Meta:
        model = 'scolarite.Filiere'
        django_get_or_create = ('code',)

    code          = factory.Sequence(lambda n: f'F{n:03d}')
    intitule_fr   = 'Filiere Test LP'
    type_diplome  = 'LP'
    nb_semestres  = 6
    credits_total = 180
    niveau_debut  = 1
    niveau_fin    = 3
    institution   = factory.SubFactory(InstitutionFactory)


class FiliereIngenieurFactory(FiliereFactory):
    """Filiere Ingenieur — Decret 2018-070."""
    intitule_fr  = 'Genie Logiciel'
    type_diplome = 'ING'


class EtudiantFactory(DjangoModelFactory):
    """Etudiant defini dans apps.absence (legacy) — utilise matricule, pas NNI."""
    class Meta:
        model = 'absence.Etudiant'
        django_get_or_create = ('matricule',)

    matricule  = factory.Sequence(lambda n: f'MAT{n:06d}')
    nom        = factory.Sequence(lambda n: f'Etudiant_{n}')
    nom_fr     = factory.SelfAttribute('nom')
    prenom_fr  = 'Test'
    genre      = 'M'
    email      = factory.LazyAttribute(lambda o: f'{o.matricule.lower()}@test.mr')
    nationalite_fr = 'Mauritanienne'
    # departement annuel (NOT NULL en BD)
    departement = factory.SubFactory(
        'tests.factories.em.DepartementAnnuelFactory',
    )
