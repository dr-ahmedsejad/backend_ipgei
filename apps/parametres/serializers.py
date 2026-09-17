from rest_framework import serializers
from .models import Year, Niveau, Semestre, Seance, Creneau, Jour, Semaine, Paiement, Ramadan, Institution, JourFerieFixe


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
    # Facultatif à la saisie : un semestre appartient à une ANNÉE d'étude, pas à
    # un niveau. Demander « MPSI ou MP » dans un formulaire de semestre n'a plus
    # de sens depuis que plusieurs niveaux partagent une même année. Ce
    # rattachement — dont le socle se sert pour rejoindre les départements — se
    # déduit du code, comme le fait déjà `ipgei.socle.semestre_socle`.
    niveau_semestre       = serializers.PrimaryKeyRelatedField(
        queryset=Niveau.objects.all(), required=False,
    )
    # L'année d'étude : ce que le formulaire demande réellement. Un semestre
    # appartient à une année — 1re, 2e — et non à un niveau : S3 est suivi par
    # MP comme par MPI. Le rattachement technique en découle, calculé plus bas.
    annee       = serializers.SerializerMethodField(read_only=True)
    annee_input = serializers.IntegerField(
        required=False, min_value=1, max_value=10, write_only=True, source='annee',
    )

    class Meta:
        model  = Semestre
        fields = '__all__'
        # Le validateur d'unicité que DRF dérive de `uniq_semestre_code_niveau_type`
        # rendrait `niveau_semestre` obligatoire — c'est justement ce qu'on veut
        # éviter. L'unicité est donc vérifiée dans `validate`, une fois le niveau
        # déduit.
        validators = []

    def get_annee(self, obj) -> int:
        """Année d'étude du semestre, telle que l'écran l'affiche."""
        from apps.ipgei.socle import rang_de_rattachement
        return rang_de_rattachement(obj.code_semestre, obj.niveau_semestre)

    def validate(self, attrs):
        annee  = attrs.pop('annee', None)
        niveau = attrs.get('niveau_semestre')

        if niveau is None and self.instance is None:
            from apps.ipgei.socle import niveau_de_rattachement

            code = (attrs.get('code_semestre') or '').strip().upper()
            # L'année saisie prime ; à défaut on la déduit du code, ce qui
            # couvre S1..S4 sans rien demander.
            niveau = niveau_de_rattachement(code, annee)
            if niveau is None:
                raise serializers.ValidationError({'annee': (
                    f'Impossible de situer « {code or "ce code"} » dans le cursus : '
                    "indiquez l'année d'étude."
                )})
            attrs['niveau_semestre'] = niveau

        code = attrs.get('code_semestre', getattr(self.instance, 'code_semestre', None))
        type_semestre = attrs.get('type_semestre',
                                  getattr(self.instance, 'type_semestre', None))
        niveau = niveau or getattr(self.instance, 'niveau_semestre', None)
        doublons = Semestre.objects.filter(
            code_semestre=code, niveau_semestre=niveau, type_semestre=type_semestre,
        )
        if self.instance is not None:
            doublons = doublons.exclude(pk=self.instance.pk)
        if doublons.exists():
            raise serializers.ValidationError(
                f'Un semestre {code} de ce type existe déjà pour ce rattachement.'
            )
        return attrs

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
    Créneau horaire : libellé, plage, durée, ordre d'affichage et mise en
    service.

    Ces champs étaient verrouillés du temps où la grille n'était qu'un miroir de
    SIGA. Ils ne le sont plus : IPGEI administre sa propre grille — en créer un,
    l'ordonner, le retirer du service — et la durée reste ce qui alimente le
    calcul de la charge d'enseignement.
    """
    class Meta:
        model  = Creneau
        fields = '__all__' 


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


class JourFerieFixeSerializer(serializers.ModelSerializer):
    """
    Un férié fixe. La validation est celle du modèle — mois 1..12, jour borné
    sur une année bissextile pour que le 29 février reste valide, libellé
    requis — et l'unicité (jour, mois) est dite en clair plutôt que laissée à
    une erreur d'intégrité.
    """
    class Meta:
        model  = JourFerieFixe
        fields = ['id', 'jour', 'mois', 'libelle', 'actif']
        validators = []

    def validate(self, attrs):
        from django.core.exceptions import ValidationError as DjangoValidationError

        instance = JourFerieFixe(**{
            **({f: getattr(self.instance, f) for f in ('jour', 'mois', 'libelle', 'actif')}
               if self.instance else {}),
            **attrs,
        })
        try:
            instance.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)
        doublon = JourFerieFixe.objects.filter(jour=instance.jour, mois=instance.mois)
        if self.instance is not None:
            doublon = doublon.exclude(pk=self.instance.pk)
        if doublon.exists():
            raise serializers.ValidationError({'jour': (
                f'Un férié fixe existe déjà au {instance.jour:02d}/{instance.mois:02d}.')})
        return attrs


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
