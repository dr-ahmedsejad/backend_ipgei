from rest_framework import serializers
from .models import Reclamation, PeriodeReclamation


class ReclamationSerializer(serializers.ModelSerializer):
    etudiant_nom       = serializers.CharField(source='etudiant.nom',       read_only=True)
    etudiant_matricule = serializers.CharField(source='etudiant.matricule', read_only=True)
    traitee_par_nom    = serializers.CharField(source='traitee_par.name',   read_only=True, default=None)
    em_code            = serializers.SerializerMethodField()
    em_intitule        = serializers.SerializerMethodField()

    def get_em_code(self, obj):
        if obj.inscription_element and obj.inscription_element.em:
            return obj.inscription_element.em.code_em
        if obj.presence and obj.presence.suivi and obj.presence.suivi.em:
            return obj.presence.suivi.em.code_em
        return None

    def get_em_intitule(self, obj):
        if obj.inscription_element and obj.inscription_element.em:
            return obj.inscription_element.em.intitule
        if obj.presence and obj.presence.suivi and obj.presence.suivi.em:
            return obj.presence.suivi.em.intitule
        return None

    class Meta:
        model  = Reclamation
        fields = [
            'id', 'etudiant', 'etudiant_nom', 'etudiant_matricule',
            'type_reclamation', 'statut',
            'presence', 'inscription_element', 'session_evaluation',
            'motif', 'justificatif',
            'em_code', 'em_intitule',
            'reponse', 'traitee_par', 'traitee_par_nom',
            'date_soumission', 'date_traitement',
        ]
        read_only_fields = [
            'id', 'etudiant', 'statut', 'reponse',
            'traitee_par', 'date_soumission', 'date_traitement',
        ]


class ReclamationCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Reclamation
        fields = [
            'type_reclamation', 'presence',
            'inscription_element', 'session_evaluation',
            'motif', 'justificatif',
        ]


class ReclamationTraiterSerializer(serializers.Serializer):
    statut  = serializers.ChoiceField(choices=['en_cours', 'acceptee', 'rejetee'])
    reponse = serializers.CharField(required=False, allow_blank=True, default='')


class PeriodeReclamationSerializer(serializers.ModelSerializer):
    annee_univ_label  = serializers.CharField(source='annee_univ.annee', read_only=True)
    institution_nom   = serializers.CharField(source='institution.nom',  read_only=True)
    filiere_nom       = serializers.CharField(source='filiere.intitule_fr', read_only=True, default=None)
    cree_par_nom      = serializers.CharField(source='cree_par.name',    read_only=True, default=None)
    statut_temporel   = serializers.CharField(read_only=True)
    est_en_cours      = serializers.BooleanField(read_only=True)
    type_session_label  = serializers.CharField(source='get_type_session_display',  read_only=True)
    type_semestre_label = serializers.CharField(source='get_type_semestre_display', read_only=True)

    class Meta:
        model  = PeriodeReclamation
        fields = [
            'id', 'annee_univ', 'annee_univ_label',
            'type_session', 'type_session_label',
            'type_semestre', 'type_semestre_label',
            'institution', 'institution_nom',
            'filiere', 'filiere_nom',
            'niveau',
            'date_ouverture', 'date_fermeture', 'actif',
            'motif',
            'cree_par', 'cree_par_nom',
            'statut_temporel', 'est_en_cours',
            'date_creation', 'date_modification',
        ]
        read_only_fields = ['id', 'cree_par', 'date_creation', 'date_modification']

    def validate(self, attrs):
        do = attrs.get('date_ouverture') or getattr(self.instance, 'date_ouverture', None)
        df = attrs.get('date_fermeture') or getattr(self.instance, 'date_fermeture', None)
        if do and df and df <= do:
            raise serializers.ValidationError({
                'date_fermeture': 'La date de fermeture doit etre posterieure a la date d ouverture.',
            })
        return attrs
