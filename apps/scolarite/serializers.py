from rest_framework import serializers
from .models import DepartementAcademique, Filiere, ParametresPonderation


class DepartementAcademiqueSerializer(serializers.ModelSerializer):
    filieres_count   = serializers.IntegerField(source='filieres.count', read_only=True)
    institution_nom  = serializers.CharField(source='institution.nom',   read_only=True, default=None)

    class Meta:
        model  = DepartementAcademique
        fields = (
            'id', 'code', 'intitule_fr', 'intitule_ar',
            'institution', 'institution_nom',
            'responsable', 'actif', 'filieres_count',
        )


class FiliereSerializer(serializers.ModelSerializer):
    # Override `code` pour neutraliser l'UniqueValidator auto de DRF (message anglais peu clair)
    # et le remplacer par notre validate_code (message français explicite avec nom de la filière en conflit).
    code = serializers.CharField(max_length=20, validators=[])

    departement_academique_nom = serializers.CharField(
        source='departement_academique.intitule_fr', read_only=True, default=None,
    )
    filiere_parent_code = serializers.CharField(
        source='filiere_parent.code', read_only=True, default=None,
    )
    filiere_parent_nom = serializers.CharField(
        source='filiere_parent.intitule_fr', read_only=True, default=None,
    )
    label_niveaux       = serializers.CharField(read_only=True)
    credits_couvert     = serializers.IntegerField(read_only=True)

    class Meta:
        model  = Filiere
        fields = '__all__'
        read_only_fields = ('date_creation', 'date_modification')

    def validate_code(self, value):
        """Message explicite en français pour la contrainte d'unicité du code."""
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError("Le code de la filière est obligatoire.")
        qs = Filiere.objects.filter(code__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            existing = qs.first()
            raise serializers.ValidationError(
                f"Le code « {value} » est déjà utilisé par la filière "
                f"« {existing.intitule_fr} » (#{existing.id})."
            )
        return value


class FiliereListSerializer(serializers.ModelSerializer):
    """Serializer leger pour les listes de selection (dropdowns, filtres)."""
    departement_academique_nom = serializers.CharField(
        source='departement_academique.intitule_fr', read_only=True, default=None,
    )

    class Meta:
        model  = Filiere
        fields = (
            'id', 'code', 'intitule_fr', 'intitule_ar',
            'type_diplome', 'est_active',
            'departement_academique', 'departement_academique_nom',
        )


class ParametresPonderationSerializer(serializers.ModelSerializer):
    class Meta:
        model  = ParametresPonderation
        fields = ('id', 'coeff_cc', 'coeff_exam', 'coeff_tp',
                  'rattrapage_plafond_actif', 'rattrapage_plafond')
