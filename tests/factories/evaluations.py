"""Factories pour apps.evaluations : SessionEvaluation, Note, ResultatElement, ResultatSemestre."""
from decimal import Decimal
import factory
from factory.django import DjangoModelFactory

from .parametres import InstitutionFactory, YearFactory
from .inscriptions import InscriptionElementFactory, InscriptionPedagogiqueFactory


class SessionNormaleImpairsFactory(DjangoModelFactory):
    """Session NORMALE des semestres Impairs (S1, S3, S5)."""
    class Meta:
        model = 'evaluations.SessionEvaluation'

    code          = factory.Sequence(lambda n: f'SN-I-{n:03d}')
    intitule      = 'Session normale Impairs'
    annee_univ    = factory.SubFactory(YearFactory)
    institution   = factory.SubFactory(InstitutionFactory)
    type_session  = 'normale'
    type_semestre = 'Impairs'
    est_ouverte   = True
    est_close     = False


class SessionRattrapageImpairsFactory(SessionNormaleImpairsFactory):
    code         = factory.Sequence(lambda n: f'SR-I-{n:03d}')
    intitule     = 'Session rattrapage Impairs'
    type_session = 'rattrapage'


class SessionNormalePairsFactory(SessionNormaleImpairsFactory):
    code          = factory.Sequence(lambda n: f'SN-P-{n:03d}')
    intitule      = 'Session normale Pairs'
    type_semestre = 'Pairs'


class NoteFactory(DjangoModelFactory):
    class Meta:
        model = 'evaluations.Note'

    inscription_element = factory.SubFactory(InscriptionElementFactory)
    session             = factory.SubFactory(SessionNormaleImpairsFactory)
    type_note           = 'CC'    # CC | TP | EXAM (cf. TYPE_NOTE_CHOICES)
    valeur              = Decimal('12.00')


class ResultatElementFactory(DjangoModelFactory):
    class Meta:
        model = 'evaluations.ResultatElement'

    inscription_element = factory.SubFactory(InscriptionElementFactory)
    session             = factory.SubFactory(SessionNormaleImpairsFactory)
    note_finale         = Decimal('12.00')
    est_valide          = True
    est_eliminatoire    = False


class ResultatSemestreFactory(DjangoModelFactory):
    class Meta:
        model = 'evaluations.ResultatSemestre'

    inscription_ped = factory.SubFactory(InscriptionPedagogiqueFactory)
    session         = factory.SubFactory(SessionNormaleImpairsFactory)
    moyenne         = Decimal('12.50')
    credits_valides = 30
    est_admis       = True
