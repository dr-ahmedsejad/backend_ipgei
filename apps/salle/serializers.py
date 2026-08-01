from rest_framework import serializers
from .models import Salle

class SalleSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Salle
        fields = '__all__'
