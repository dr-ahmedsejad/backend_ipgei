from rest_framework import serializers
from apps.absence.models import Etudiant, Presence
from apps.reclamations.models import Reclamation
from apps.reclamations.serializers import ReclamationCreateSerializer, ReclamationSerializer


class ProfilEtudiantSerializer(serializers.ModelSerializer):
    # Le modèle Filiere n'a pas de champ `nom` — on compose depuis `code` + `intitule_fr`.
    filiere_nom      = serializers.SerializerMethodField()
    departement_nom  = serializers.CharField(source='departement.nom', read_only=True, default=None)

    def get_filiere_nom(self, obj):
        if not obj.filiere_id or not obj.filiere:
            return None
        f = obj.filiere
        return f'{f.code} — {f.intitule_fr}' if f.intitule_fr else f.code

    class Meta:
        model  = Etudiant
        fields = [
            'id', 'matricule', 'nom', 'nom_fr', 'nom_ar',
            'prenom_fr', 'prenom_ar',
            'genre', 'date_naissance', 'cni',
            'telephone', 'email',
            'adresse_fr', 'adresse_ar',
            'photo', 'filiere', 'filiere_nom',
            'departement', 'departement_nom',
            'statut', 'serie_bac', 'date_creation',
        ]
        read_only_fields = [
            'id', 'matricule', 'nom', 'nom_fr', 'nom_ar',
            'prenom_fr', 'prenom_ar',
            'genre', 'date_naissance', 'cni',
            'filiere', 'filiere_nom',
            'departement', 'departement_nom',
            'statut', 'serie_bac', 'date_creation',
        ]


class NoteEtudiantSerializer(serializers.Serializer):
    """Sérialise un InscriptionElement avec ses notes CC/TP/EXAM et son résultat."""

    id          = serializers.IntegerField()
    em          = serializers.IntegerField(source='em_id')
    em_libelle  = serializers.SerializerMethodField()
    note_cc     = serializers.SerializerMethodField()
    note_tp     = serializers.SerializerMethodField()
    note_exam   = serializers.SerializerMethodField()
    note_finale = serializers.SerializerMethodField()
    valide      = serializers.SerializerMethodField()
    credits     = serializers.SerializerMethodField()
    semestre    = serializers.SerializerMethodField()
    annee_univ  = serializers.SerializerMethodField()

    def _notes_map(self, obj):
        """Cache {type_note: valeur} depuis le prefetch_related('notes')."""
        cache = getattr(obj, '_notes_cache', None)
        if cache is None:
            obj._notes_cache = {n.type_note: float(n.valeur) for n in obj.notes.all()}
        return obj._notes_cache

    def get_em_libelle(self, obj):
        return obj.em.intitule if obj.em else None

    def get_note_cc(self, obj):
        return self._notes_map(obj).get('CC')

    def get_note_tp(self, obj):
        return self._notes_map(obj).get('TP')

    def get_note_exam(self, obj):
        return self._notes_map(obj).get('EXAM')

    def get_note_finale(self, obj):
        try:
            r = obj.resultats.order_by('-id').first()
            return float(r.note_finale) if r else None
        except Exception:
            return None

    def _acquis_map(self, obj):
        """Map {code_em: acquis} CONSOLIDÉE (compensation/capitalisation, y compris
        cross-année) par (étudiant, semestre, année), mémoïsée sur l'instance du
        sérialiseur — 1 calcul de consolidation par semestre, pas par EM.
        Source unique de vérité (même moteur que le relevé officiel)."""
        try:
            ia    = obj.inscription_ped.inscription_admin
            etu   = ia.etudiant
            annee = ia.annee_univ
            sem   = obj.em.semestre if obj.em else None
        except Exception:
            return {}
        if not sem or not annee:
            return {}
        cache = getattr(self, '_acquis_cache', None)
        if cache is None:
            cache = self._acquis_cache = {}
        key = (etu.id, sem.id, annee.id)
        if key not in cache:
            from apps.documents.services import calculer_resultat_semestre_consolide
            m = {}
            try:
                res = calculer_resultat_semestre_consolide(etu, sem, annee)
                for mod in res.get('modules', []):
                    for e in mod.get('elements', []):
                        m[e.get('code')] = bool(e.get('est_valide')) or e.get('decision') == 'Validé'
            except Exception:
                m = {}
            cache[key] = m
        return cache[key]

    def get_valide(self, obj):
        # Statut CONSOLIDÉ (compensation/capitalisation cross-année) — cohérent
        # avec le relevé officiel. Repli sur le ResultatElement brut si l'EM
        # n'apparaît pas dans la consolidation.
        cmap = self._acquis_map(obj)
        if obj.em and obj.em.code_em in cmap:
            return cmap[obj.em.code_em]
        try:
            r = obj.resultats.order_by('-id').first()
            return r.est_valide if r else False
        except Exception:
            return False

    def get_credits(self, obj):
        return obj.em.credits if obj.em else 0

    def get_semestre(self, obj):
        try:
            return obj.inscription_ped.semestre.semestre
        except Exception:
            return None

    def get_annee_univ(self, obj):
        try:
            return obj.inscription_ped.inscription_admin.annee_univ.annee
        except Exception:
            return None


class AbsenceEtudiantSerializer(serializers.ModelSerializer):
    statut_label  = serializers.CharField(source='get_statut_display',           read_only=True)
    suivi_jour    = serializers.CharField(source='suivi.jour_fk.jour',           read_only=True, default=None)
    suivi_em      = serializers.CharField(source='suivi.em.intitule',            read_only=True, default=None)
    suivi_prof    = serializers.CharField(source='suivi.prof.nom',               read_only=True, default=None)
    suivi_semaine = serializers.IntegerField(source='suivi.numero_semaine',      read_only=True)
    suivi_creneau = serializers.CharField(source='suivi.creneau_fk.creneau',     read_only=True, default=None)
    suivi_type    = serializers.CharField(source='suivi.type_seance_fk.type_seance', read_only=True, default=None)

    class Meta:
        model  = Presence
        fields = [
            'id', 'statut', 'statut_label', 'commentaire', 'date_modification',
            'suivi_jour', 'suivi_creneau', 'suivi_type', 'suivi_em', 'suivi_prof', 'suivi_semaine',
        ]
