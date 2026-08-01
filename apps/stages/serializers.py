from rest_framework import serializers
from .models import ConventionStage, EvaluationStage, DerogationMedicale


class ConventionStageSerializer(serializers.ModelSerializer):
    etudiant_nom          = serializers.CharField(source='etudiant.nom', read_only=True)
    etudiant_matricule    = serializers.CharField(source='etudiant.matricule', read_only=True)
    tuteur_academique_nom = serializers.SerializerMethodField()

    def get_tuteur_academique_nom(self, obj):
        return str(obj.tuteur_academique) if obj.tuteur_academique else None

    class Meta:
        model  = ConventionStage
        fields = '__all__'


class EvaluationStageSerializer(serializers.ModelSerializer):
    jury_noms = serializers.SerializerMethodField()

    def get_jury_noms(self, obj):
        return [str(p) for p in obj.jury.all()]

    class Meta:
        model  = EvaluationStage
        fields = '__all__'


class DerogationMedicaleSerializer(serializers.ModelSerializer):
    etudiant_nom = serializers.CharField(source='etudiant.nom', read_only=True)

    class Meta:
        model  = DerogationMedicale
        fields = '__all__'
        # statut / decision_motif sont posés par les actions @approuver / @refuser
        # (views.py) → non modifiables via CRUD pour ne pas court-circuiter le workflow.
        read_only_fields = ['statut', 'decision_motif']
