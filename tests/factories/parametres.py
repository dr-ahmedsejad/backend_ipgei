"""Factories pour apps.parametres : Institution, Niveau, Semestre, Year, Paiement."""
import factory
from factory.django import DjangoModelFactory


class InstitutionFactory(DjangoModelFactory):
    class Meta:
        model = 'parametres.Institution'
        django_get_or_create = ('acronyme',)

    acronyme       = 'TEST'
    nom            = 'Institution Test'
    nom_fr         = 'Institution Test'
    est_principale = True


class YearFactory(DjangoModelFactory):
    class Meta:
        model = 'parametres.Year'
        django_get_or_create = ('annee',)

    annee      = '2025-2026'
    est_active = True


class NiveauFactory(DjangoModelFactory):
    class Meta:
        model = 'parametres.Niveau'
        django_get_or_create = ('niveau',)

    niveau = factory.Sequence(lambda n: f'L{n+1}')


class SemestreFactory(DjangoModelFactory):
    class Meta:
        model = 'parametres.Semestre'
        django_get_or_create = ('code_semestre',)

    code_semestre   = factory.Sequence(lambda n: f'S{n+1}')
    semestre        = factory.LazyAttribute(lambda o: f'Semestre {o.code_semestre[1:]}')
    type_semestre   = 'I'
    credits         = 30
    niveau_semestre = factory.SubFactory(NiveauFactory)


class PaiementFactory(DjangoModelFactory):
    class Meta:
        model = 'parametres.Paiement'

    type       = 'CM'
    taux       = 500.0
    date_debut = '2024-09-01'


class JourFactory(DjangoModelFactory):
    class Meta:
        model = 'parametres.Jour'
        django_get_or_create = ('jour',)

    jour = 'Lundi'


class CreneauFactory(DjangoModelFactory):
    class Meta:
        model = 'parametres.Creneau'
        django_get_or_create = ('creneau',)

    creneau      = '08:00 - 09:30'
    duree        = 1.5
    type_creneau = 'matin'
    ordre        = 1


class SeanceFactory(DjangoModelFactory):
    class Meta:
        model = 'parametres.Seance'
        django_get_or_create = ('type_seance',)

    type_seance = 'CM'
    is_special  = False
