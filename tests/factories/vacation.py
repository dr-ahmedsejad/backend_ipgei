"""Factories pour Prof et Vacation."""
from datetime import date as _date
import factory
from factory.django import DjangoModelFactory

from .em import DepartementAnnuelFactory, EMLegacyFactory
from .parametres import InstitutionFactory, SeanceFactory, PaiementFactory


class ProfVacataireFactory(DjangoModelFactory):
    class Meta:
        model = 'prof.Prof'
        django_get_or_create = ('NNI',)

    NNI    = factory.Sequence(lambda n: 9000000000 + n)
    nom    = factory.Sequence(lambda n: f'Prof_{n}')
    type   = 'vacataire'
    genre  = 'M'


class ProfPermanentFactory(ProfVacataireFactory):
    type  = 'permanent'
    grade = 'Maitre-assistant'


class VacationFactory(DjangoModelFactory):
    class Meta:
        model = 'vacation.Vacation'

    prof          = factory.SubFactory(ProfVacataireFactory)
    em            = factory.SubFactory(EMLegacyFactory)
    type          = factory.SubFactory(SeanceFactory)
    duree         = 1.5
    date          = _date(2025, 10, 15)
    annee_univ    = '2025-2026'
    institution   = factory.SubFactory(InstitutionFactory)
