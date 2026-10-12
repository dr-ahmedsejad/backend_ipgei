from rest_framework import serializers
from .models import Prof, ProfTypeHistory


class ProfSerializer(serializers.ModelSerializer):
    banque_nom = serializers.CharField(source='banque.nom', read_only=True, allow_null=True)

    class Meta:
        model  = Prof
        fields = '__all__'
        # `actif` est piloté UNIQUEMENT par les actions archiver/restaurer (views.py),
        # jamais par la création/édition. En lecture seule ici pour :
        #  (1) qu'un nouveau prof soit ACTIF par défaut (model default=True) ;
        #  (2) éviter le piège DRF BooleanField : absent d'une requête multipart/
        #      form-data (le formulaire en envoie pour le CV/diplôme), il serait
        #      interprété False -> prof archivé à tort.
        read_only_fields = ('actif',)

    def validate(self, attrs):
        """Validation metier : les champs obligatoires en BD (NOT NULL legacy)
        doivent etre verifies au niveau du serializer pour eviter IntegrityError 500.
        Renvoie 400 avec un message clair par champ manquant."""
        errors = {}

        # Champs NOT NULL en BD gesafped26 (schema legacy)
        # Note : sur l'instance existante (update), on accepte que ces champs restent inchanges
        is_update = self.instance is not None

        if not is_update or 'telephone' in attrs:
            if not attrs.get('telephone'):
                errors['telephone'] = 'Le téléphone est requis.'

        if not is_update or 'banque' in attrs:
            if not attrs.get('banque'):
                errors['banque'] = 'La banque est requise.'

        # Le COURRIEL est obligatoire, et pas pour la forme : il devient
        # l'adresse du compte enseignant, et `CustomUser.email` est UNIQUE.
        # Une chaîne vide n'y est pas un NULL — PostgreSQL l'indexe comme une
        # valeur ordinaire —, si bien qu'un seul professeur sans adresse
        # empêche la création du compte de TOUS les suivants. C'est ce qui
        # s'est produit : un compte de test à courriel vide a privé vingt-sept
        # enseignants d'accès pendant trois mois, en silence.
        #
        # Aucun domaine n'est imposé : une adresse personnelle convient.
        if not is_update or 'email' in attrs:
            courriel = (attrs.get('email') or '').strip()
            if not courriel:
                errors['email'] = ('Le courriel est requis : il sert '
                                   "d'adresse au compte de l'enseignant.")
            else:
                # Unicité insensible à la casse. La même adresse sur deux
                # fiches ne se verrait qu'au moment où le second compte
                # échouerait — c'est-à-dire nulle part.
                doublon = Prof.objects.filter(email__iexact=courriel)
                if self.instance is not None:
                    doublon = doublon.exclude(pk=self.instance.pk)
                autre = doublon.first()
                if autre:
                    errors['email'] = (f'Ce courriel est déjà celui de '
                                       f'{autre.nom}. Chaque enseignant doit '
                                       'avoir le sien.')

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class ProfListSerializer(serializers.ModelSerializer):
    """Serializer léger pour les listes et <select>."""
    banque_nom = serializers.CharField(source='banque.nom', read_only=True, allow_null=True)
    a_un_compte = serializers.SerializerMethodField()

    def get_a_un_compte(self, obj):
        return obj.user_id is not None

    class Meta:
        model  = Prof
        # `NNI` est affiche en premiere colonne de la liste et sert de critere
        # de recherche : l'omettre laissait la colonne vide a l'ecran.
        fields = ['id', 'NNI', 'nom', 'type', 'genre', 'grade', 'banque', 'banque_nom',
                  'email', 'telephone', 'cv', 'diplome', 'actif', 'a_un_compte']


class ProfStatsSerializer(serializers.Serializer):
    total          = serializers.IntegerField()
    vacataires     = serializers.IntegerField()
    permanents     = serializers.IntegerField()
    contractuels   = serializers.IntegerField()
    hommes         = serializers.IntegerField()
    femmes         = serializers.IntegerField()


class ProfTypeHistorySerializer(serializers.ModelSerializer):
    prof_nom = serializers.CharField(source='prof.nom', read_only=True)

    class Meta:
        model  = ProfTypeHistory
        fields = ['id', 'prof', 'prof_nom', 'type', 'date_debut', 'date_fin',
                  'motif', 'cree_par', 'cree_le']
        read_only_fields = ['cree_le']

    def validate(self, attrs):
        """Validation metier :
        1. date_fin (si fournie) >= date_debut
        2. Pas de chevauchement avec une autre periode du meme prof
           (deux periodes pour le meme prof ne peuvent pas se chevaucher).
        """
        from django.db.models import Q
        prof       = attrs.get('prof')   or (self.instance.prof if self.instance else None)
        date_debut = attrs.get('date_debut') or (self.instance.date_debut if self.instance else None)
        date_fin   = attrs.get('date_fin', self.instance.date_fin if self.instance else None)

        if date_debut is None:
            raise serializers.ValidationError({'date_debut': 'requis.'})
        if date_fin is not None and date_fin < date_debut:
            raise serializers.ValidationError({'date_fin': 'doit etre >= date_debut.'})

        if prof is None:
            return attrs

        # Chevauchement : une autre periode du meme prof recoupe [date_debut, date_fin]
        overlapping = ProfTypeHistory.objects.filter(prof=prof)
        if self.instance is not None:
            overlapping = overlapping.exclude(pk=self.instance.pk)

        if date_fin is None:
            # Periode ouverte : recouvre tout depuis date_debut → bloque toute periode commencant apres date_debut
            #                   ET toute periode encore ouverte (date_fin IS NULL)
            conflict = overlapping.filter(
                Q(date_fin__isnull=True) |       # autre periode ouverte = chevauchement direct
                Q(date_fin__gte=date_debut)      # autre periode finit apres notre debut
            )
        else:
            # Periode fermee : chevauchement si autre.date_debut <= notre.date_fin ET (autre.date_fin >= notre.date_debut OU autre ouverte)
            conflict = overlapping.filter(date_debut__lte=date_fin).filter(
                Q(date_fin__isnull=True) | Q(date_fin__gte=date_debut)
            )

        if conflict.exists():
            first = conflict.first()
            raise serializers.ValidationError({
                'non_field_errors': [
                    f'Chevauchement avec une autre periode du meme prof : '
                    f'#{first.id} type={first.type} {first.date_debut} -> {first.date_fin or "en cours"}.'
                ]
            })
        return attrs
