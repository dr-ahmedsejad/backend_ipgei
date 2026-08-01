from rest_framework import serializers
from .models import Etudiant, Presence, SeuilAbsence


class EtudiantSerializer(serializers.ModelSerializer):
    departement_nom = serializers.CharField(source='departement.nom',       read_only=True)
    filiere_nom     = serializers.CharField(source='filiere.intitule_fr',   read_only=True, default=None)
    filiere_code    = serializers.CharField(source='filiere.code',          read_only=True, default=None)
    niveau_nom      = serializers.CharField(source='departement.niveau.niveau', read_only=True, default=None)
    # Niveau courant de l'étudiant = niveau de sa dernière inscription administrative
    # (ex. « L1 »). Source fiable, contrairement à departement.niveau souvent vide
    # sur les classes legacy. Repli sur le niveau du département si pas d'inscription.
    niveau          = serializers.SerializerMethodField()
    # Inscription administrative la PLUS RÉCENTE (filière + niveau + année réels).
    # Source fiable pour « l'inscription actuelle » : le FK statique Etudiant.filiere
    # reste la filière de 1re année (tronc commun, ex. LPSTAT) et departement.niveau
    # est souvent vide sur les classes legacy → sans ça le dossier affiche L1/tronc
    # commun à vie même pour un étudiant passé en SEA/SDID L3.
    inscription_actuelle = serializers.SerializerMethodField()
    # Diplômé ⇔ présent au registre des diplômes (RegistreDiplome). Le champ brut
    # Etudiant.statut n'est jamais basculé à 'diplome' à l'attribution → on le dérive.
    est_diplome     = serializers.SerializerMethodField()
    statut_effectif = serializers.SerializerMethodField()

    class Meta:
        model  = Etudiant
        fields = '__all__'

    def _is_diplome(self, obj):
        """Utilise l'annotation Exists (_est_diplome) posée par EtudiantViewSet →
        pas de requête. Repli (serializer utilisé ailleurs) : une requête ponctuelle,
        mise en cache sur l'instance."""
        if not hasattr(obj, '_is_diplome_cache'):
            val = getattr(obj, '_est_diplome', None)
            if val is None:
                from apps.documents.models import RegistreDiplome
                val = RegistreDiplome.objects.filter(etudiant=obj).exists()
            obj._is_diplome_cache = bool(val)
        return obj._is_diplome_cache

    def get_est_diplome(self, obj):
        return self._is_diplome(obj)

    def get_statut_effectif(self, obj):
        """Statut d'AFFICHAGE : 'diplome' si diplômé, sinon le statut stocké."""
        return 'diplome' if self._is_diplome(obj) else obj.statut

    def _latest_ia(self, obj):
        """Dernière inscription admin, mise en cache sur l'instance : 1 requête par
        étudiant, partagée par get_niveau et get_inscription_actuelle (pas de N+1
        supplémentaire par rapport à l'existant)."""
        if not hasattr(obj, '_latest_ia_cache'):
            obj._latest_ia_cache = (
                obj.inscriptions_admin
                .select_related('filiere', 'filiere__departement_academique', 'annee_univ')
                .order_by('-annee_univ__annee')
                .first()
            )
        return obj._latest_ia_cache

    def get_niveau(self, obj):
        ia = self._latest_ia(obj)
        if ia and ia.niveau:
            return f'L{ia.niveau}'
        dep = getattr(obj, 'departement', None)
        return getattr(getattr(dep, 'niveau', None), 'niveau', None) if dep else None

    def get_inscription_actuelle(self, obj):
        ia = self._latest_ia(obj)
        if not ia:
            return None
        da = getattr(ia.filiere, 'departement_academique', None) if ia.filiere else None
        return {
            'filiere_nom':                 getattr(ia.filiere, 'intitule_fr', None),
            'filiere_code':                getattr(ia.filiere, 'code', None),
            'niveau':                      f'L{ia.niveau}' if ia.niveau else None,
            'annee_universitaire':         getattr(ia.annee_univ, 'annee', None),
            'departement_academique_nom':  getattr(da, 'intitule_fr', None),
            'departement_academique_code': getattr(da, 'code', None),
        }


class PresenceSerializer(serializers.ModelSerializer):
    etudiant_nom       = serializers.CharField(source='etudiant.nom',       read_only=True)
    etudiant_matricule = serializers.CharField(source='etudiant.matricule', read_only=True)
    statut_label       = serializers.CharField(source='get_statut_display', read_only=True)

    # Champs du suivi lié — informations de la séance
    suivi_jour         = serializers.CharField(source='suivi.jour_fk.jour',          read_only=True, allow_null=True)
    suivi_date         = serializers.DateField( source='suivi.date_suivie',          read_only=True, allow_null=True)
    suivi_semaine      = serializers.IntegerField(source='suivi.numero_semaine',     read_only=True, allow_null=True)
    suivi_prof         = serializers.CharField(source='suivi.prof.nom',              read_only=True, allow_null=True)
    suivi_em           = serializers.CharField(source='suivi.em.intitule',           read_only=True, allow_null=True)
    suivi_salle        = serializers.CharField(source='suivi.salle.nom',             read_only=True, allow_null=True)
    suivi_departement  = serializers.CharField(source='suivi.departement.nom',       read_only=True, allow_null=True)
    suivi_creneau      = serializers.CharField(source='suivi.creneau_fk.creneau',    read_only=True, allow_null=True)
    suivi_type         = serializers.CharField(source='suivi.type_seance_fk.type_seance', read_only=True, allow_null=True)

    class Meta:
        model  = Presence
        fields = '__all__'


class PresenceBulkSerializer(serializers.Serializer):
    suivi_id  = serializers.IntegerField()
    presences = serializers.ListField(child=serializers.DictField())


class SeuilAbsenceSerializer(serializers.ModelSerializer):
    class Meta:
        model  = SeuilAbsence
        fields = '__all__'


class ImportEtudiantsSerializer(serializers.Serializer):
    departement_id = serializers.IntegerField()
    fichier        = serializers.FileField()


class RapportAbsenceSerializer(serializers.Serializer):
    departement_id      = serializers.IntegerField(required=False)
    annee_universitaire = serializers.CharField()
    mois                = serializers.IntegerField(required=False, min_value=1, max_value=12)

