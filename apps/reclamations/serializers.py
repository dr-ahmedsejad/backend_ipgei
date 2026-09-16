from rest_framework import serializers
from .models import (
    PeriodeReclamation, Reclamation, ReclamationSeance, SaisieNoteEnLigne,
)


class ReclamationSerializer(serializers.ModelSerializer):
    etudiant_nom       = serializers.CharField(source='etudiant.nom',       read_only=True)
    etudiant_matricule = serializers.CharField(source='etudiant.matricule', read_only=True)
    traitee_par_nom    = serializers.CharField(source='traitee_par.name',   read_only=True, default=None)
    # Les clés gardent leur nom — `em_*` — pour ne pas retoucher des écrans qui
    # marchent : le socle dit « EM », l'IPGEI dit « matière », et la
    # réclamation d'absence continue de lire le suivi.
    em_code            = serializers.SerializerMethodField()
    em_intitule        = serializers.SerializerMethodField()
    semestre_code      = serializers.CharField(source='note_ipgei.semestre.code',
                                               read_only=True, default=None)
    classe_nom         = serializers.CharField(source='note_ipgei.inscription.classe.nom',
                                               read_only=True, default=None)

    def get_em_code(self, obj):
        if obj.note_ipgei_id and obj.note_ipgei.matiere:
            return obj.note_ipgei.matiere.code
        if obj.presence and obj.presence.suivi and obj.presence.suivi.em:
            return obj.presence.suivi.em.code_em
        return None

    def get_em_intitule(self, obj):
        if obj.note_ipgei_id and obj.note_ipgei.matiere:
            return obj.note_ipgei.matiere.intitule
        if obj.presence and obj.presence.suivi and obj.presence.suivi.em:
            return obj.presence.suivi.em.intitule
        return None

    class Meta:
        model  = Reclamation
        fields = [
            'id', 'etudiant', 'etudiant_nom', 'etudiant_matricule',
            'type_reclamation', 'statut',
            'presence', 'note_ipgei', 'semestre_code', 'classe_nom',
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
            'note_ipgei',
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


class ReclamationSeanceSerializer(serializers.ModelSerializer):
    """
    Une réclamation de séance, telle que les deux écrans la lisent.

    Tout est déjà dans la ligne — libellés compris, puisqu'ils y ont été
    recopiés à la création. Aucun `select_related` n'est donc nécessaire, et
    surtout aucune jointure vers une table miroir : c'est ce qui permet à la
    boîte de réception de rester lisible sur une instance où le suivi vient
    d'être remplacé par un dump.
    """
    statut_display = serializers.CharField(source='get_statut_display',
                                           read_only=True)

    class Meta:
        model  = ReclamationSeance
        fields = [
            'id', 'prof_id', 'prof_nom',
            'annee_universitaire', 'type_semestre', 'numero_semaine',
            'em_id', 'em_code', 'em_intitule', 'jour', 'creneau',
            'date_seance', 'commentaire_origine', 'pointage_id',
            'motif', 'statut', 'statut_display', 'reponse',
            'traite_par_nom', 'date_soumission', 'date_traitement',
        ]
        read_only_fields = ['prof_id', 'prof_nom', 'statut', 'reponse',
                            'traite_par_nom', 'date_soumission',
                            'date_traitement']


class SaisieNoteEnLigneSerializer(serializers.ModelSerializer):
    """
    Un brouillon de note, tel que la direction le voit.

    Aucune jointure : les libellés ont été recopiés à la saisie. C'est ce qui
    permet à cet écran de rester lisible sur une instance dont les tables
    d'évaluation viennent d'être remplacées par un dump.
    """
    statut_display = serializers.CharField(source='get_statut_display',
                                           read_only=True)

    class Meta:
        model  = SaisieNoteEnLigne
        fields = [
            'id', 'prof_id', 'prof_nom',
            'note_ipgei_id', 'type_note', 'numero',
            'matiere_id', 'classe_id', 'semestre_id', 'annee_universitaire',
            'etudiant_matricule', 'etudiant_nom',
            'matiere_code', 'matiere_intitule', 'classe_nom', 'semestre_code',
            'valeur',
            'statut', 'statut_display', 'date_saisie', 'date_modification',
            'exporte_le',
        ]
        read_only_fields = fields
