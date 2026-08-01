from rest_framework import serializers
from .models import Suivie, SuiviePointage, ChargeInstitution


class SuivieSerializer(serializers.ModelSerializer):
    prof_nom           = serializers.CharField(source='prof.nom',                  read_only=True, allow_null=True)
    em_code            = serializers.CharField(source='em.code_em',                read_only=True, allow_null=True)
    em_intitule        = serializers.CharField(source='em.intitule',               read_only=True, allow_null=True)
    dept_nom           = serializers.CharField(source='departement.nom',           read_only=True, allow_null=True)
    salle_nom          = serializers.CharField(source='salle.nom',                 read_only=True, allow_null=True)
    semestre_nom       = serializers.CharField(source='semestre.semestre',         read_only=True, allow_null=True)
    creneau_label      = serializers.CharField(source='creneau_fk.creneau',        read_only=True, allow_null=True)
    type_seance_label  = serializers.CharField(source='type_seance_fk.type_seance', read_only=True, allow_null=True)
    jour_label         = serializers.CharField(source='jour_fk.jour',              read_only=True, allow_null=True)

    class Meta:
        model  = Suivie
        fields = '__all__'


class SuivieCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model   = Suivie
        exclude = ['taux_paiement', 'duree_creneau']  # auto-renseignes par save()


class SuiviePointageSerializer(serializers.ModelSerializer):
    prof_nom           = serializers.CharField(source='prof.nom',                  read_only=True, allow_null=True)
    em_code            = serializers.CharField(source='em.code_em',                read_only=True, allow_null=True)
    em_intitule        = serializers.CharField(source='em.intitule',               read_only=True, allow_null=True)
    salle_nom          = serializers.CharField(source='salle.nom',                 read_only=True, allow_null=True)
    semestre_nom       = serializers.CharField(source='semestre.semestre',         read_only=True, allow_null=True)
    creneau_label      = serializers.CharField(source='creneau_fk.creneau',        read_only=True, allow_null=True)
    type_seance_label  = serializers.CharField(source='type_seance_fk.type_seance', read_only=True, allow_null=True)
    jour_label         = serializers.CharField(source='jour_fk.jour',              read_only=True, allow_null=True)
    dept_noms          = serializers.SerializerMethodField()

    # Mode d'affichage cellule : True -> uniquement le type centre (Sport, Instruction militaire, ...)
    type_seance_is_special = serializers.BooleanField(source='type_seance_fk.is_special', read_only=True, default=False)

    class Meta:
        model  = SuiviePointage
        fields = '__all__'
        # taux_paiement est calculé côté serveur à la génération du suivi ;
        # reclamation_* passent par l'action @reclamer → non modifiables via CRUD
        # (empêche la falsification du montant payable).
        read_only_fields = ['taux_paiement', 'reclamation_statut', 'reclamation_motif']

    def get_dept_noms(self, obj):
        """Liste triee des noms de departements via la M2M canonique."""
        try:
            return sorted(d.nom for d in obj.departements.all() if d.nom)
        except Exception:
            return []


class ChargeInstitutionSerializer(serializers.ModelSerializer):
    institution_nom = serializers.CharField(source='institution.acronyme', read_only=True)
    prof_nom        = serializers.CharField(source='prof.nom',             read_only=True)

    class Meta:
        model  = ChargeInstitution
        fields = '__all__'


class RemplissageSerializer(serializers.Serializer):
    annee_universitaire = serializers.CharField()
    semestre_id         = serializers.IntegerField(required=False)
    departement_id      = serializers.IntegerField(required=False)


class AvancementSemestreSerializer(serializers.Serializer):
    annee_universitaire = serializers.CharField()
    departement_id      = serializers.IntegerField(required=False)
