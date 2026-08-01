"""Factories pour les PV de deliberation."""
import factory
from datetime import date as _date
from factory.django import DjangoModelFactory

from .scolarite import FiliereFactory
from .parametres import InstitutionFactory, YearFactory
from .evaluations import SessionNormaleImpairsFactory


class PVDeliberationSemestrielFactory(DjangoModelFactory):
    """PV semestriel pour S1 (par defaut)."""
    class Meta:
        model = 'evaluations.PVDeliberation'

    type_pv           = 'semestriel'
    session           = factory.SubFactory(SessionNormaleImpairsFactory)
    filiere           = factory.SubFactory(FiliereFactory)
    institution       = factory.SubFactory(InstitutionFactory)
    niveau            = 1
    semestre_code     = 'S1'
    est_clos          = False
    date_deliberation = factory.LazyFunction(_date.today)


class PVDeliberationAnnuelFactory(DjangoModelFactory):
    class Meta:
        model = 'evaluations.PVDeliberation'

    type_pv           = 'annuel'
    annee_univ        = factory.SubFactory(YearFactory)
    filiere           = factory.SubFactory(FiliereFactory)
    institution       = factory.SubFactory(InstitutionFactory)
    niveau            = 1
    semestre_code     = ''
    est_clos          = False
    date_deliberation = factory.LazyFunction(_date.today)
