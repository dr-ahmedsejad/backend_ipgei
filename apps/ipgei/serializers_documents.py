"""Serializer de consultation du registre des documents IPGEI."""
from rest_framework import serializers

from apps.documents.models import DocumentOfficiel


class DocumentIPGEISerializer(serializers.ModelSerializer):
    type_libelle       = serializers.CharField(source='get_type_document_display', read_only=True)
    etudiant_nom       = serializers.CharField(source='etudiant.nom_display', read_only=True)
    etudiant_matricule = serializers.CharField(source='etudiant.matricule', read_only=True)
    genere_par_nom     = serializers.CharField(source='genere_par.name', read_only=True, default='')
    a_pdf              = serializers.SerializerMethodField()

    class Meta:
        model  = DocumentOfficiel
        fields = [
            'id', 'numero_serie', 'type_document', 'type_libelle',
            'etudiant', 'etudiant_nom', 'etudiant_matricule',
            'annee_universitaire', 'est_valide', 'date_generation',
            'premiere_generation', 'genere_par', 'genere_par_nom', 'a_pdf',
            # Qui signe la pièce, recopié à l'émission : c'est la seule trace
            # permettant, des mois plus tard, de savoir au nom de qui elle est
            # partie — le paramétrage, lui, aura changé.
            'signataire_nom', 'signataire_titre',
        ]
        # Le token de verification et le hash ne sortent jamais de l'API de
        # gestion : le token circule uniquement dans le QR du PDF.
        read_only_fields = fields

    def get_a_pdf(self, obj) -> bool:
        return bool(obj.fichier_pdf)
