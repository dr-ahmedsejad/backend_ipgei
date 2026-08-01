from rest_framework import serializers
from .models import Banque

class BanqueSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Banque
        fields = '__all__'
