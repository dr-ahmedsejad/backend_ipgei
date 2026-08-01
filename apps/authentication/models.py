import os
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models


def validate_avatar(value):
    """Valide la taille (5 Mo max) et le format (JPEG/PNG) de l'avatar uploadé."""
    max_size_bytes = 5 * 1024 * 1024  # 5 Mo
    allowed_extensions = {'.jpg', '.jpeg', '.png'}
    allowed_content_types = {'image/jpeg', 'image/png'}

    if value.size > max_size_bytes:
        mb = value.size // (1024 * 1024)
        raise ValidationError(
            f"La taille de l'avatar ne peut pas dépasser 5 Mo (reçu : {mb} Mo)."
        )

    ext = os.path.splitext(value.name)[1].lower()
    if ext not in allowed_extensions:
        raise ValidationError(
            "Format non supporté. Seuls les fichiers JPEG et PNG sont acceptés."
        )

    # Vérification du Content-Type déclaré par le navigateur (disponible sur InMemoryUploadedFile)
    content_type = getattr(value, 'content_type', None)
    if content_type and content_type not in allowed_content_types:
        raise ValidationError(
            "Le fichier doit être une image JPEG ou PNG valide."
        )


ROLE_CHOICES = [
    ('admin',                'Administrateur'),
    ('DG',                   'Directeur général'),
    ('DA',                   'Direction administrative'),
    ('DE',                   'Direction enseignement'),
    ('AA',                   'Assistant administratif'),
    ('IT',                   'Informatique'),
    ('scolarite',            'Scolarité'),
    ('responsable_filiere',  'Responsable de filière'),
    ('jury_president',       'Président de jury'),
    ('etudiant',             'Étudiant'),
    ('enseignant',           'Enseignant'),
]


class CustomUser(AbstractUser):
    email            = models.EmailField(unique=True)
    role             = models.CharField(max_length=20, choices=ROLE_CHOICES, default='AA')
    name             = models.CharField(max_length=150, blank=True)
    avatar           = models.ImageField(upload_to='avatars/', null=True, blank=True, validators=[validate_avatar])
    doit_changer_mdp = models.BooleanField(default=False)

    # Delegation EDT : groupes que ce user peut gerer (emplois/suivi/vacation).
    # Signal unique pour autoriser la gestion EDT d'un departement, independant
    # du role. Admin/superuser passent outre. Vide = aucune delegation.
    managed_departements = models.ManyToManyField(
        'departement.Departement',
        blank=True,
        related_name='edt_managers',
        db_table='authentication_user_managed_departements',
        help_text="Groupes que ce user peut gerer (EDT/suivi/vacation). Vide = aucune delegation.",
    )

    class Meta:
        db_table = 'authentication_customuser'

    def __str__(self):
        return f'{self.username} ({self.role})'


class Module(models.Model):
    code  = models.CharField(max_length=50, unique=True)
    nom   = models.CharField(max_length=100)
    icone = models.CharField(max_length=50, blank=True)
    ordre = models.IntegerField(default=0)

    class Meta:
        db_table = 'authentication_module'
        ordering = ['ordre']

    def __str__(self):
        return self.code


class Action(models.Model):
    code  = models.CharField(max_length=50, unique=True)
    nom   = models.CharField(max_length=100)
    icone = models.CharField(max_length=50, blank=True)

    class Meta:
        db_table = 'authentication_action'

    def __str__(self):
        return self.code


class ModuleAction(models.Model):
    module = models.ForeignKey(Module, on_delete=models.CASCADE, related_name='actions')
    action = models.ForeignKey(Action, on_delete=models.CASCADE, related_name='modules')

    class Meta:
        db_table = 'authentication_moduleaction'
        unique_together = ('module', 'action')

    def __str__(self):
        return f'{self.module.code}:{self.action.code}'


class RoleDefault(models.Model):
    role          = models.CharField(max_length=20, choices=ROLE_CHOICES)
    module_action = models.ForeignKey(ModuleAction, on_delete=models.CASCADE, related_name='role_defaults')
    allowed       = models.BooleanField(default=False)

    class Meta:
        db_table = 'authentication_roledefault'
        unique_together = ('role', 'module_action')


class UserPermission(models.Model):
    user          = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='permissions_rbac')
    module_action = models.ForeignKey(ModuleAction, on_delete=models.CASCADE)
    allowed       = models.BooleanField(default=False)
    departement   = models.ForeignKey(
        'departement.Departement', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='user_permissions_rbac',
    )
    filiere       = models.ForeignKey(
        'scolarite.Filiere', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='user_permissions',
        help_text="Périmètre filière pour responsable_filiere / jury_president.",
    )

    class Meta:
        db_table = 'authentication_userpermission'
        unique_together = ('user', 'module_action')


SEMESTRE_CHOICES = [('Pairs', 'Pairs'), ('Impairs', 'Impairs')]


class UserContexte(models.Model):
    """Contexte de session persisté par utilisateur : année universitaire et semestre actifs.
    Découplé de l'authentification JWT pour permettre la mise à jour sans re-connexion.
    Créé automatiquement au premier login et mis à jour à chaque sélection."""
    user                = models.OneToOneField(
        CustomUser, on_delete=models.CASCADE, related_name='contexte',
    )
    annee_universitaire = models.CharField(max_length=9, blank=True, default='')
    semestre            = models.CharField(max_length=10, choices=SEMESTRE_CHOICES, default='Pairs')
    updated_at          = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'authentication_usercontexte'

    def __str__(self):
        return f'{self.user.username} — {self.annee_universitaire} / {self.semestre}'
