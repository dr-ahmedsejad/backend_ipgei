from decimal import Decimal
from rest_framework import serializers
from .models import Module, ElementModule


class EMPlanificationSerializer(serializers.Serializer):
    """Sérialiseur léger pour afficher les EMs rattachés à un Module LMD."""
    id          = serializers.IntegerField(read_only=True)
    code_em     = serializers.CharField(read_only=True)
    intitule    = serializers.CharField(read_only=True)
    CM          = serializers.IntegerField(read_only=True)
    TD          = serializers.IntegerField(read_only=True)
    TP          = serializers.IntegerField(read_only=True)
    PR          = serializers.IntegerField(read_only=True)
    has_tp      = serializers.BooleanField(read_only=True)
    credits     = serializers.IntegerField(read_only=True, allow_null=True)
    coefficient = serializers.IntegerField(read_only=True, allow_null=True)
    departement_nom = serializers.CharField(source='departement.nom', read_only=True)


class ElementModuleSerializer(serializers.ModelSerializer):
    module_code     = serializers.CharField(source='module.code',       read_only=True)
    module_intitule = serializers.CharField(source='module.intitule_fr', read_only=True)
    poids_valides   = serializers.SerializerMethodField()

    class Meta:
        model  = ElementModule
        fields = (
            'id', 'module', 'module_code', 'module_intitule',
            'code', 'intitule_fr', 'intitule_ar',
            'credits', 'coefficient',
            'poids_cc', 'poids_tp', 'poids_exam', 'poids_valides',
            'seuil_eliminatoire', 'ordre',
        )

    def get_poids_valides(self, obj):
        return obj.poids_cc + obj.poids_tp + obj.poids_exam == Decimal('1')

    def validate(self, data):
        cc   = data.get('poids_cc',   getattr(self.instance, 'poids_cc',   Decimal('0')))
        tp   = data.get('poids_tp',   getattr(self.instance, 'poids_tp',   Decimal('0')))
        exam = data.get('poids_exam', getattr(self.instance, 'poids_exam', Decimal('0')))
        if cc + tp + exam != Decimal('1'):
            raise serializers.ValidationError(
                {'poids_exam': 'La somme des poids CC+TP+Exam doit être égale à 1.'}
            )

        # Art. 8 Arrêté 562 / Art. 13 Décret 2018-070 : au plus 3 éléments par
        # module. Bloque uniquement l'AJOUT (création, ou rattachement à un
        # autre module) — l'édition d'un élément existant d'un module legacy
        # déjà surchargé reste possible. Exception : un admin peut dépasser.
        from apps.evaluations.services.coherence_maquette import (
            MAX_ELEMENTS_PAR_MODULE, user_peut_depasser_max_elements,
        )
        request = self.context.get('request')
        is_admin = user_peut_depasser_max_elements(getattr(request, 'user', None))
        module = data.get('module')
        ajout = (
            self.instance is None
            or (module is not None and module.pk != self.instance.module_id)
        )
        module_cible = module or getattr(self.instance, 'module', None)
        if ajout and not is_admin and module_cible is not None:
            qs = module_cible.elements.all()
            if self.instance is not None:
                qs = qs.exclude(pk=self.instance.pk)
            nb = qs.count()
            if nb >= MAX_ELEMENTS_PAR_MODULE:
                raise serializers.ValidationError({
                    'module': (
                        f'Le module {module_cible.code} a déjà {nb} éléments — '
                        f'maximum {MAX_ELEMENTS_PAR_MODULE} (Art. 8 Arrêté 562 / '
                        f'Art. 13 Décret 2018-070).'
                    ),
                })
        return data


class ModuleSerializer(serializers.ModelSerializer):
    elements          = ElementModuleSerializer(many=True, read_only=True)
    elements_count    = serializers.IntegerField(source='elements.count',            read_only=True)
    ems_count         = serializers.IntegerField(source='ems_planification.count',   read_only=True)
    filiere_code      = serializers.CharField(source='filiere.code',                 read_only=True)
    filiere_intitule  = serializers.CharField(source='filiere.intitule_fr',          read_only=True)
    semestre_nom      = serializers.CharField(source='semestre.semestre',            read_only=True)
    credits_coherents = serializers.SerializerMethodField()
    ems_credits_total = serializers.SerializerMethodField()
    ems_planification = EMPlanificationSerializer(many=True, read_only=True)
    # Code déclaré explicitement pour retirer le UniqueValidator automatique (qui
    # renvoie un message générique « module avec ce code existe déjà »). L'unicité
    # est contrôlée dans validate() avec un message qui NOMME la filière où le
    # code est déjà utilisé.
    code              = serializers.CharField(max_length=20)

    class Meta:
        model  = Module
        fields = (
            'id', 'code', 'intitule_fr', 'intitule_ar',
            'semestre', 'semestre_nom',
            'filiere', 'filiere_code', 'filiere_intitule',
            'credits', 'coefficient', 'seuil_compensation',
            'actif',
            'elements_count', 'ems_count', 'credits_coherents',
            'ems_credits_total',
            'elements',
            'ems_planification',
            'institution',
        )
        extra_kwargs = {
            'institution': {'required': False, 'allow_null': True},
        }

    @staticmethod
    def _ems_credits_total(obj):
        """Somme des credits des EM de planification — mise en cache sur l'instance."""
        if not hasattr(obj, '_ems_credits_total_cache'):
            obj._ems_credits_total_cache = sum(
                (e.credits or 0) for e in obj.ems_planification.all()
            )
        return obj._ems_credits_total_cache

    def get_credits_coherents(self, obj):
        total = self._ems_credits_total(obj)
        return obj.credits > 0 and total == obj.credits

    def get_ems_credits_total(self, obj):
        return self._ems_credits_total(obj)

    def validate(self, attrs):
        # Defense-in-depth : Institution principale par defaut. A remplacer par
        # request.user.institution quand le scoping multi-institution sera complet.
        if not attrs.get('institution'):
            from apps.parametres.models import Institution
            attrs['institution'] = Institution.objects.filter(est_principale=True).first()

        # Unicité du code module (unique en base, toutes filières confondues).
        # Message explicite qui NOMME la filière où le code est déjà utilisé —
        # à la création comme à la modification (on s'exclut soi-même). Sans ça,
        # l'UI n'affichait qu'un « ce code existe déjà » sans dire où.
        code = (attrs.get('code') or getattr(self.instance, 'code', '') or '').strip()
        if code:
            qs = Module.objects.filter(code__iexact=code).select_related('filiere')
            if self.instance is not None:
                qs = qs.exclude(pk=self.instance.pk)
            conflit = qs.first()
            if conflit is not None:
                fil = conflit.filiere
                fil_txt = f"« {fil.intitule_fr} » ({fil.code})" if fil else "—"
                raise serializers.ValidationError({
                    'code': (
                        f"Le code « {code} » est déjà utilisé par le module "
                        f"« {conflit.intitule_fr} » de la filière {fil_txt}. "
                        f"Choisissez un autre code."
                    )
                })
        return attrs


class ModuleListSerializer(serializers.ModelSerializer):
    """Sérialiseur léger pour les listes (sans éléments imbriqués)."""
    filiere_code    = serializers.CharField(source='filiere.code',       read_only=True)
    filiere_intitule = serializers.CharField(source='filiere.intitule_fr', read_only=True)
    semestre_nom    = serializers.CharField(source='semestre.semestre',  read_only=True)
    elements_count  = serializers.IntegerField(source='elements.count', read_only=True)
    ems_count       = serializers.IntegerField(source='ems_planification.count', read_only=True)
    credits_coherents = serializers.SerializerMethodField()
    ems_credits_total = serializers.SerializerMethodField()

    class Meta:
        model  = Module
        fields = (
            'id', 'code', 'intitule_fr', 'intitule_ar',
            'semestre', 'semestre_nom',
            'filiere', 'filiere_code', 'filiere_intitule',
            'credits', 'coefficient', 'seuil_compensation',
            'actif', 'elements_count', 'ems_count', 'credits_coherents',
            'ems_credits_total',
            'institution',
        )

    @staticmethod
    def _ems_credits_total(obj):
        if not hasattr(obj, '_ems_credits_total_cache'):
            obj._ems_credits_total_cache = sum(
                (e.credits or 0) for e in obj.ems_planification.all()
            )
        return obj._ems_credits_total_cache

    def get_credits_coherents(self, obj):
        total = self._ems_credits_total(obj)
        return obj.credits > 0 and total == obj.credits

    def get_ems_credits_total(self, obj):
        return self._ems_credits_total(obj)
