from rest_framework import serializers
from .models import Departement

class DepartementSerializer(serializers.ModelSerializer):
    niveau_nom   = serializers.CharField(source='niveau.niveau',          read_only=True)
    filiere_nom  = serializers.CharField(source='filiere.intitule_fr',    read_only=True, default=None)
    filiere_code = serializers.CharField(source='filiere.code',           read_only=True, default=None)
    groupe_display = serializers.SerializerMethodField()

    class Meta:
        model  = Departement
        fields = '__all__'
        extra_kwargs = {'institution': {'required': False, 'allow_null': True}}

    def get_groupe_display(self, obj) -> str:
        parts = []
        if obj.filiere:
            parts.append(obj.filiere.intitule_fr or '')
        if obj.groupe:
            parts.append(obj.groupe)
        return ' — '.join(p for p in parts if p) or obj.nom

    def create(self, validated_data):
        # Auto-fill institution depuis filiere.institution sinon institution principale
        # (Section 1bis institution_V1 — institution NOT NULL en base, transparente cote UI)
        if not validated_data.get('institution'):
            from apps.parametres.models import Institution
            filiere = validated_data.get('filiere')
            validated_data['institution'] = (
                (filiere.institution if filiere and filiere.institution_id else None)
                or Institution.objects.filter(est_principale=True).first()
            )
        return super().create(validated_data)
