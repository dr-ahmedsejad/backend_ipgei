"""Factories pour apps.inscriptions : InscriptionAdmin, InscriptionPed, InscriptionElement."""
import factory
from factory.django import DjangoModelFactory

from .scolarite import EtudiantFactory, FiliereFactory
from .parametres import InstitutionFactory, SemestreFactory, YearFactory
from .em import ElementModuleFactory, EMLegacyFactory


class InscriptionAdministrativeFactory(DjangoModelFactory):
    class Meta:
        model = 'inscriptions.InscriptionAdministrative'

    etudiant           = factory.SubFactory(EtudiantFactory)
    annee_univ         = factory.SubFactory(YearFactory)
    filiere            = factory.SubFactory(FiliereFactory)
    institution        = factory.SubFactory(InstitutionFactory)
    niveau             = 1
    numero_inscription = factory.Sequence(lambda n: f'INS-{n:06d}')
    statut             = 'validee'
    est_payee          = True


class InscriptionPedagogiqueFactory(DjangoModelFactory):
    class Meta:
        model = 'inscriptions.InscriptionPedagogique'

    inscription_admin = factory.SubFactory(InscriptionAdministrativeFactory)
    semestre          = factory.SubFactory(SemestreFactory)
    est_redoublant    = False
    est_dette         = False


class InscriptionElementFactory(DjangoModelFactory):
    class Meta:
        model = 'inscriptions.InscriptionElement'

    inscription_ped = factory.SubFactory(InscriptionPedagogiqueFactory)
    element         = factory.SubFactory(ElementModuleFactory)
    em              = factory.SubFactory(EMLegacyFactory)
    est_dette       = False
