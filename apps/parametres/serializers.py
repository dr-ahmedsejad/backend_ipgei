from rest_framework import serializers
from .models import Year, Niveau, Semestre, Seance, Creneau, Jour, Semaine, Paiement, Ramadan, Institution


class YearSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Year
        fields = '__all__'


class NiveauSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Niveau
        fields = '__all__'


class SemestreSerializer(serializers.ModelSerializer):
    niveau_nom            = serializers.CharField(source='niveau_semestre.niveau', read_only=True)
    decalage_pedagogique  = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model  = Semestre
        fields = '__all__'

    def get_decalage_pedagogique(self, obj):
        """Decalage en semaines applicable a ce semestre (selon le niveau lie).

        Convention : ne s'applique qu'au semestre Impair. Retourne 0 pour
        le semestre Pair ou si aucun dept du niveau n'a de decalage.
        """
        from apps.departement.semaine_helpers import decalage_for_code_semestre
        return decalage_for_code_semestre(obj.code_semestre, obj.type_semestre)


class SeanceSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Seance
        fields = '__all__'


class CreneauSerializer(serializers.ModelSerializer):
    """
    Creneau horaire.

    Sur IPGEI, la liste des creneaux est un miroir de SIGA (cf.
    `manage.py sync_creneaux_siga`) : libelle, ordre et statut ne se modifient
    donc pas ici. Seule la **duree** est saisie localement, creneau par creneau,
    car c'est elle qui sert de base au calcul de charge.
    """
    class Meta:
        model  = Creneau
        fields = '__all__'
        read_only_fields = ['creneau', 'ordre', 'is_actif', 'type_creneau']


class JourSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Jour
        fields = '__all__'


class SemaineSerializer(serializers.ModelSerializer):
    jour                 = serializers.CharField(source='jour_fk.jour', read_only=True)
    type_semaine_display = serializers.CharField(source='get_type_semaine_display', read_only=True)

    class Meta:
        model  = Semaine
        fields = '__all__'


class PaiementSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Paiement
        fields = '__all__'


class RamadanSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Ramadan
        fields = '__all__'


class InstitutionSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Institution
        fields = '__all__'

    def to_representation(self, instance):
        data    = super().to_representation(instance)
        request = self.context.get('request')
        if instance.logo:
            url = instance.logo.url
        else:
            from django.templatetags.static import static
            url = static('assets/img/logo_iss.png')
        data['logo_url'] = request.build_absolute_uri(url) if request else url
        return data


class GenerateSemainesSerializer(serializers.Serializer):
    annee_universitaire = serializers.CharField()
    date_debut          = serializers.DateField()
    date_fin            = serializers.DateField()
    type_semestre       = serializers.ChoiceField(choices=[('I', 'Impair'), ('P', 'Pair')])
    jours_actifs        = serializers.ListField(child=serializers.CharField(), required=False)


class AddBatchSemainesSerializer(serializers.Serializer):
    annee_universitaire = serializers.CharField()
    date_debut          = serializers.DateField()
    nombre_semaines     = serializers.IntegerField(min_value=1, max_value=52)
    type_semestre       = serializers.ChoiceField(choices=[('I', 'Impair'), ('P', 'Pair')])
