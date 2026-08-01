from rest_framework import serializers
from .models import Vacation, Surveillance


class SurveillanceSerializer(serializers.ModelSerializer):
    prof_nom  = serializers.CharField(source='prof.nom',        read_only=True)
    dept_nom  = serializers.CharField(source='departement.nom', read_only=True)

    class Meta:
        model  = Surveillance
        fields = '__all__'


class VacationSerializer(serializers.ModelSerializer):
    prof_nom     = serializers.CharField(source='prof.nom',    read_only=True)
    prof_type    = serializers.CharField(source='prof.type',   read_only=True, allow_null=True)
    em_intitule  = serializers.CharField(source='em.intitule', read_only=True, allow_null=True)
    type_label   = serializers.CharField(source='type.type_seance', read_only=True, allow_null=True)
    dept_noms    = serializers.SerializerMethodField()
    montant      = serializers.FloatField(read_only=True)

    class Meta:
        model  = Vacation
        fields = '__all__'

    def get_dept_noms(self, obj):
        # B-4 : utilise le prefetch_related('departements') du viewset au lieu
        # de declencher un nouveau SQL via values_list(). Pour 20 vacations
        # paginees → -20 SQL queries.
        return [d.nom for d in obj.departements.all()]


class VacationCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model   = Vacation
        exclude = ['taux_paiement']
        extra_kwargs = {
            # institution est NOT NULL en modele mais auto-derive du 1er departement
            # selectionne (ou Institution.est_principale en dernier recours).
            'institution': {'required': False, 'allow_null': True},
        }

    def validate(self, attrs):
        if not attrs.get('institution'):
            from apps.parametres.models import Institution
            departements = attrs.get('departements') or []
            inst = None
            if departements:
                first = departements[0]
                inst = getattr(first, 'institution', None)
            if not inst:
                inst = Institution.objects.filter(est_principale=True).first()
            attrs['institution'] = inst

        # Doublon : une vacation IDENTIQUE (même prof, type, EM, date, durée) existe
        # déjà → on REFUSE avec un message clair, au lieu de la réutiliser en silence
        # (ce qui affichait « ajoutée avec succès » sans rien ajouter).
        prof  = attrs.get('prof',  getattr(self.instance, 'prof',  None))
        date  = attrs.get('date',  getattr(self.instance, 'date',  None))
        em    = attrs.get('em',    getattr(self.instance, 'em',    None))
        type_ = attrs.get('type',  getattr(self.instance, 'type',  None))
        duree = attrs.get('duree', getattr(self.instance, 'duree', None))
        if prof and date and type_:
            qs = Vacation.objects.filter(prof=prof, date=date, em=em, type=type_, duree=duree)
            if self.instance is not None:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                date_txt   = date.strftime('%d/%m/%Y') if hasattr(date, 'strftime') else str(date)
                type_label = getattr(type_, 'type_seance', None) or 'séance'
                raise serializers.ValidationError(
                    f"Doublon : une vacation « {type_label} » de {duree}h pour ce professeur "
                    f"existe déjà le {date_txt}. Choisissez une autre date, ou modifiez la "
                    f"vacation existante."
                )
        return attrs

    def create(self, validated_data):
        """Création idempotente : si une vacation identique (meme prof, date,
        em, type, duree) existe deja, on la met juste a jour (M2M departements)
        au lieu d'en creer une 2eme. Protection contre les doubles POST
        (double-clic sur 'Enregistrer' cote frontend - race condition entre
        le click et la mise a `disabled` du bouton)."""
        departements = validated_data.pop('departements', None)
        existing = Vacation.objects.filter(
            prof=validated_data.get('prof'),
            date=validated_data.get('date'),
            em=validated_data.get('em'),
            type=validated_data.get('type'),
            duree=validated_data.get('duree'),
        ).first()
        if existing:
            # Reuse de la ligne existante : on aligne juste les depts si fournis
            if departements is not None:
                existing.departements.set(departements)
            return existing
        instance = super().create(validated_data)
        if departements is not None:
            instance.departements.set(departements)
        return instance


class EtatVacationSerializer(serializers.Serializer):
    annee_univ     = serializers.CharField()
    departement_id = serializers.IntegerField(required=False)
    prof_id        = serializers.IntegerField(required=False)
    mois           = serializers.IntegerField(required=False)


class AttestationSerializer(serializers.Serializer):
    prof_id        = serializers.IntegerField()
    annee_univ     = serializers.CharField()
    departement_id = serializers.IntegerField(required=False)
