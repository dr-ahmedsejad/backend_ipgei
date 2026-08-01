"""Factories pour apps.em (EM legacy planification) et apps.modules (LMD)."""
from decimal import Decimal
import factory
from factory.django import DjangoModelFactory

from .scolarite import FiliereFactory
from .parametres import SemestreFactory, InstitutionFactory


# ── Departement annuel (planification) ─────────────────────────────────────────
class DepartementAnnuelFactory(DjangoModelFactory):
    class Meta:
        model = 'departement.Departement'
        django_get_or_create = ('nom',)

    nom                 = factory.Sequence(lambda n: f'TEST_DEP_{n}')
    annee_universitaire = '2025-2026'
    institution         = factory.SubFactory(InstitutionFactory)
    filiere             = factory.SubFactory(FiliereFactory)


# ── Module LMD (modules.Module) ────────────────────────────────────────────────
class ModuleLMDFactory(DjangoModelFactory):
    class Meta:
        model = 'modules.Module'
        django_get_or_create = ('code',)

    code               = factory.Sequence(lambda n: f'MOD{n:03d}')
    intitule_fr        = factory.Sequence(lambda n: f'Module {n}')
    semestre           = factory.SubFactory(SemestreFactory)
    filiere            = factory.SubFactory(FiliereFactory)
    institution        = factory.SubFactory(InstitutionFactory)
    credits            = 6
    coefficient        = Decimal('1.00')
    seuil_compensation = Decimal('10.00')
    actif              = True


# ── ElementModule LMD (modules.ElementModule) ──────────────────────────────────
class ElementModuleFactory(DjangoModelFactory):
    """EM cote Module LMD (poids CC/TP/Exam, seuil eliminatoire)."""
    class Meta:
        model = 'modules.ElementModule'
        django_get_or_create = ('code',)

    module             = factory.SubFactory(ModuleLMDFactory)
    code               = factory.Sequence(lambda n: f'EM{n:03d}')
    intitule_fr        = factory.Sequence(lambda n: f'Element {n}')
    credits            = 3
    coefficient        = Decimal('1.00')
    poids_cc           = Decimal('0.30')
    poids_tp           = Decimal('0.20')
    poids_exam         = Decimal('0.50')
    seuil_eliminatoire = Decimal('6.00')
    ordre              = 0


# ── EM legacy (apps.em — planification annuelle) ───────────────────────────────
class EMLegacyFactory(DjangoModelFactory):
    """EM legacy (apps.em.EM) — utilise pour planification, vacation, charge."""
    class Meta:
        model = 'em.EM'
        django_get_or_create = ('code_em', 'departement')

    code_em            = factory.Sequence(lambda n: f'CS{n:03d}')
    intitule           = factory.Sequence(lambda n: f'Cours Test {n}')
    CM                 = 0
    TD                 = 0
    TP                 = 0
    PR                 = 0
    departement        = factory.SubFactory(DepartementAnnuelFactory)
    semestre           = factory.SubFactory(SemestreFactory)
    institution        = factory.SubFactory(InstitutionFactory)
    has_tp             = False
    seuil_eliminatoire = Decimal('6.00')
