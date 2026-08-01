from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth.password_validation import validate_password
from .models import CustomUser, Module, Action, ModuleAction, RoleDefault, UserPermission, UserContexte, ROLE_CHOICES, SEMESTRE_CHOICES


# ── JWT custom payload ────────────────────────────────────────────────────────
class SIGATokenObtainPairSerializer(TokenObtainPairSerializer):
    # Rendus optionnels : si fournis, ils mettent à jour UserContexte en base.
    # Si absents, le contexte précédent (en base) est retourné.
    # Cela découple le contexte métier du mécanisme d'authentification.
    annee_universitaire = serializers.CharField(required=False, write_only=True, allow_blank=True)
    semestres           = serializers.ChoiceField(
        choices=SEMESTRE_CHOICES,
        required=False, write_only=True,
    )

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token['username'] = user.username
        token['name']     = user.name or user.get_full_name()
        token['email']    = user.email
        token['role']     = user.role
        return token

    def validate(self, attrs):
        annee     = attrs.pop('annee_universitaire', None) or None
        semestres = attrs.pop('semestres', None)
        data = super().validate(attrs)
        user = self.user

        # La FK fk_user_ctx_annee_univ (UserContexte.annee_universitaire → annee.annee)
        # refuse les valeurs absentes de la table annee — y compris ''. Pour un nouveau
        # compte sans UserContexte, on doit donc fournir une année valide dès la
        # création (impossible de se rabattre sur le default='' du modèle).
        if not annee:
            from apps.parametres.models import Year
            default_year = (
                Year.objects.filter(est_active=True).order_by('-annee').first()
                or Year.objects.order_by('-annee').first()
            )
            annee_pour_defaults = default_year.annee if default_year else None
        else:
            annee_pour_defaults = annee

        if not annee_pour_defaults:
            raise serializers.ValidationError(
                "Aucune année universitaire n'est définie en base. "
                "Contactez l'administrateur."
            )

        # Persister le contexte si fourni, sinon lire depuis la base
        # Défaut 'Impairs' pour les nouveaux comptes (évite le défaut modèle 'Pairs')
        contexte, _ = UserContexte.objects.get_or_create(
            user=user,
            defaults={
                'annee_universitaire': annee_pour_defaults,
                'semestre':            semestres or 'Impairs',
            },
        )
        if annee:
            contexte.annee_universitaire = annee
        if semestres:
            contexte.semestre = semestres
        if annee or semestres:
            contexte.save()

        user_data = {
            'id':                   user.pk,
            'username':             user.username,
            'name':                 user.name or user.get_full_name(),
            'email':                user.email,
            'role':                 user.role,
            'avatar':               user.avatar.url if user.avatar else None,
            'annee_universitaire':  contexte.annee_universitaire,
            'semestre':             contexte.semestre,
            'doit_changer_mdp':     user.doit_changer_mdp,
        }

        # Champs supplémentaires pour le rôle étudiant
        if user.role == 'etudiant':
            etudiant = None
            try:
                etudiant = user.etudiant_profile
            except Exception:
                # Pas encore lié → chercher par CNI (username initial = CNI)
                try:
                    from apps.absence.models import Etudiant as EtudiantModel
                    etudiant = EtudiantModel.objects.filter(cni=user.username).first()
                except Exception:
                    pass
            if etudiant:
                user_data['etudiant_id'] = etudiant.pk
                user_data['matricule']   = etudiant.matricule
            else:
                user_data['etudiant_id'] = None
                user_data['matricule']   = None

        # Champs supplémentaires pour le rôle enseignant
        if user.role == 'enseignant':
            try:
                prof = user.prof_profile
                user_data['prof_id']   = prof.pk
                user_data['prof_nom']  = prof.nom
                user_data['prof_type'] = prof.type
            except Exception:
                user_data['prof_id']   = None
                user_data['prof_nom']  = None
                user_data['prof_type'] = None

        data['user'] = user_data
        return data


# ── Contexte utilisateur ──────────────────────────────────────────────────────
class UserContexteSerializer(serializers.ModelSerializer):
    class Meta:
        model  = UserContexte
        fields = ['annee_universitaire', 'semestre', 'updated_at']
        read_only_fields = ['updated_at']


# ── User ──────────────────────────────────────────────────────────────────────
class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model  = CustomUser
        fields = ['id', 'username', 'name', 'email', 'role', 'avatar', 'is_active', 'date_joined']
        read_only_fields = ['id', 'date_joined']


class UserCreateSerializer(serializers.ModelSerializer):
    password  = serializers.CharField(write_only=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True, label='Confirmer le mot de passe')

    class Meta:
        model  = CustomUser
        fields = ['username', 'name', 'email', 'role', 'password', 'password2']

    def validate(self, attrs):
        if attrs['password'] != attrs.pop('password2'):
            raise serializers.ValidationError({'password2': 'Les mots de passe ne correspondent pas.'})
        return attrs

    def create(self, validated_data):
        return CustomUser.objects.create_user(**validated_data)


class UserUpdateSerializer(serializers.ModelSerializer):
    """
    Mise à jour d'un utilisateur PAR UN ADMIN (UserViewSet, permission IsAdmin).
    Expose `role` à dessein : seul l'admin doit pouvoir changer les rôles.
    NE PAS utiliser pour le self-service profil (cf. ProfilUpdateSerializer).
    """
    class Meta:
        model  = CustomUser
        fields = ['name', 'email', 'role', 'avatar']


class ProfilUpdateSerializer(serializers.ModelSerializer):
    """
    Mise à jour du profil par l'utilisateur lui-même (ProfilView, IsAuthenticated).
    N'expose volontairement PAS `role` : sans ça, tout utilisateur connecté
    pourrait se promouvoir admin via `PATCH /auth/profil/ {"role":"admin"}`
    (élévation de privilège). Le changement de rôle reste réservé à l'admin
    via UserViewSet/UserUpdateSerializer.
    """
    class Meta:
        model  = CustomUser
        fields = ['name', 'email', 'avatar']


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, validators=[validate_password])

    def validate_old_password(self, value):
        if not self.context['request'].user.check_password(value):
            raise serializers.ValidationError('Mot de passe actuel incorrect.')
        return value


# ── RBAC ──────────────────────────────────────────────────────────────────────
class ModuleSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Module
        fields = ['id', 'code', 'nom', 'icone', 'ordre']


class ActionSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Action
        fields = ['id', 'code', 'nom', 'icone']


class ModuleActionSerializer(serializers.ModelSerializer):
    module = ModuleSerializer(read_only=True)
    action = ActionSerializer(read_only=True)

    class Meta:
        model  = ModuleAction
        fields = '__all__'


class RoleDefaultSerializer(serializers.ModelSerializer):
    module_action = ModuleActionSerializer(read_only=True)

    class Meta:
        model  = RoleDefault
        fields = ['id', 'role', 'module_action', 'allowed']
        read_only_fields = fields


class UserPermissionSerializer(serializers.ModelSerializer):
    module_action = ModuleActionSerializer(read_only=True)

    class Meta:
        model  = UserPermission
        # Affichage seul : les écritures RBAC passent par les endpoints toggle
        # (admin-only). On verrouille pour éviter tout mass-assignment de `allowed`.
        fields = ['id', 'user', 'module_action', 'allowed', 'departement', 'filiere']
        read_only_fields = fields


class PermissionToggleSerializer(serializers.Serializer):
    module_action_id = serializers.IntegerField()
    allowed          = serializers.BooleanField()
    user_id          = serializers.IntegerField(required=False)
    role             = serializers.CharField(required=False)


# ── RBAC toggle serializers ───────────────────────────────────────────────────
class UserToggleSerializer(serializers.Serializer):
    """Valide les entrées de UserToggleView : IDs entiers positifs, état parmi les valeurs autorisées."""
    user_id = serializers.IntegerField(min_value=1)
    ma_id   = serializers.IntegerField(min_value=1)
    state   = serializers.ChoiceField(choices=['on', 'off', 'role'])


class RoleToggleSerializer(serializers.Serializer):
    """Valide les entrées de RoleToggleView : rôle parmi les choix définis, ID entier positif."""
    role  = serializers.ChoiceField(choices=[r[0] for r in ROLE_CHOICES])
    ma_id = serializers.IntegerField(min_value=1)
