"""Sérialiseurs du module Cours (inbox FK-free : em/auteur en snapshot).

Les alias `em` (=em_id) et `auteur` (=auteur_id) sont conservés pour ne pas casser
le frontend existant, qui lisait ces clés.
"""
from rest_framework import serializers

from .models import Cours, CoursPieceJointe, CoursReponse


class CoursPieceJointeSerializer(serializers.ModelSerializer):
    fichier_url = serializers.SerializerMethodField()

    class Meta:
        model = CoursPieceJointe
        fields = ['id', 'type', 'url', 'fichier_url', 'libelle']

    def get_fichier_url(self, obj):
        if not obj.fichier:
            return ''
        request = self.context.get('request')
        url = obj.fichier.url
        return request.build_absolute_uri(url) if request else url


class CoursReponseSerializer(serializers.ModelSerializer):
    auteur            = serializers.IntegerField(source='auteur_id', read_only=True)  # alias compat
    parent_auteur_nom = serializers.SerializerMethodField()

    class Meta:
        model = CoursReponse
        fields = ['id', 'cours', 'parent', 'contenu', 'auteur', 'auteur_id', 'auteur_nom',
                  'auteur_role', 'parent_auteur_nom', 'date_creation', 'date_modification']
        read_only_fields = ['cours', 'auteur_id', 'auteur_nom', 'auteur_role']

    def get_parent_auteur_nom(self, obj):
        return obj.parent.auteur_nom if obj.parent_id else ''


class CoursSerializer(serializers.ModelSerializer):
    em          = serializers.IntegerField(source='em_id', read_only=True)      # alias compat
    auteur      = serializers.IntegerField(source='auteur_id', read_only=True)  # alias compat
    pieces      = CoursPieceJointeSerializer(many=True, read_only=True)
    reponses    = CoursReponseSerializer(many=True, read_only=True)
    nb_reponses = serializers.SerializerMethodField()

    class Meta:
        model = Cours
        fields = [
            'id', 'em', 'em_id', 'em_code', 'em_intitule', 'annee_universitaire',
            'titre', 'contenu', 'auteur', 'auteur_id', 'auteur_nom', 'auteur_role',
            'pieces', 'reponses', 'nb_reponses',
            'date_creation', 'date_modification',
        ]
        read_only_fields = ['auteur_id', 'auteur_nom', 'auteur_role', 'annee_universitaire']

    def get_nb_reponses(self, obj):
        return obj.reponses.count()
