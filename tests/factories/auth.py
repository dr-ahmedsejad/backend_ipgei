"""Factories pour les utilisateurs et le RBAC."""
import factory
from factory.django import DjangoModelFactory


class UserFactory(DjangoModelFactory):
    class Meta:
        model = 'authentication.CustomUser'
        django_get_or_create = ('username',)

    username  = factory.Sequence(lambda n: f'user_{n}')
    email     = factory.LazyAttribute(lambda o: f'{o.username}@test.mr')
    role      = 'AA'
    password  = factory.PostGenerationMethodCall('set_password', 'testpass')


class AdminUserFactory(UserFactory):
    role         = 'admin'
    is_superuser = True
    is_staff     = True
    username     = 'test_admin'


class DEUserFactory(UserFactory):
    role     = 'DE'
    username = 'test_de'


class EnseignantUserFactory(UserFactory):
    role     = 'enseignant'
    username = 'test_ens'


class EtudiantUserFactory(UserFactory):
    role     = 'etudiant'
    username = 'test_etu'


class ModuleRBACFactory(DjangoModelFactory):
    class Meta:
        model = 'authentication.Module'
        django_get_or_create = ('code',)

    code  = 'emplois'
    nom   = 'Emplois'
    icone = 'Calendar'
    ordre = 10


class ActionRBACFactory(DjangoModelFactory):
    class Meta:
        model = 'authentication.Action'
        django_get_or_create = ('code',)

    code  = 'voir'
    nom   = 'Voir'
    icone = 'Eye'


class ModuleActionRBACFactory(DjangoModelFactory):
    class Meta:
        model = 'authentication.ModuleAction'

    module = factory.SubFactory(ModuleRBACFactory)
    action = factory.SubFactory(ActionRBACFactory)


class RoleDefaultFactory(DjangoModelFactory):
    class Meta:
        model = 'authentication.RoleDefault'

    role          = 'DE'
    module_action = factory.SubFactory(ModuleActionRBACFactory)
    allowed       = True


class UserPermissionFactory(DjangoModelFactory):
    class Meta:
        model = 'authentication.UserPermission'

    user          = factory.SubFactory(UserFactory)
    module_action = factory.SubFactory(ModuleActionRBACFactory)
    allowed       = True
