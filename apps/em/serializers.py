from rest_framework import serializers
from .models import EM


class EMSerializer(serializers.ModelSerializer):
    departement_nom     = serializers.CharField(source='departement.nom',                   read_only=True)
    departement_annee   = serializers.CharField(source='departement.annee_universitaire',  read_only=True, default='')
    groupe              = serializers.CharField(source='departement.groupe',               read_only=True, default='')
    # filiere_id / filiere_nom : identité STABLE de l'EM. Chaîne de priorité robuste
    # au departement désormais NULL : EM.filiere → module_lmd.filiere → departement.filiere.
    filiere_id          = serializers.SerializerMethodField()
    filiere_nom         = serializers.SerializerMethodField()
    # Section 1ter institution_V1 — chaine stable EM → Module → Filiere (independante de l'annee)
    module_filiere_id   = serializers.IntegerField(source='module_lmd.filiere_id',          read_only=True, default=None)
    module_filiere_nom  = serializers.CharField(source='module_lmd.filiere.intitule_fr',    read_only=True, default=None)

    def _filiere_obj(self, obj):
        if obj.filiere_id and obj.filiere:
            return obj.filiere
        if obj.module_lmd_id and obj.module_lmd and obj.module_lmd.filiere_id:
            return obj.module_lmd.filiere
        if obj.departement_id and obj.departement and obj.departement.filiere_id:
            return obj.departement.filiere
        return None

    def get_filiere_id(self, obj):
        f = self._filiere_obj(obj)
        return f.id if f else None

    def get_filiere_nom(self, obj):
        f = self._filiere_obj(obj)
        return (f.intitule_fr if f else None)
    semestre_nom        = serializers.CharField(source='semestre.semestre',            read_only=True)
    # Niveau du semestre de l'EM (utilise pour filtrer les groupes par niveau cote frontend)
    niveau_id           = serializers.IntegerField(source='semestre.niveau_semestre_id', read_only=True, default=None)
    niveau_nom          = serializers.CharField(source='semestre.niveau_semestre.niveau', read_only=True, default=None)
    module_lmd_code     = serializers.CharField(source='module_lmd.code',             read_only=True, default=None)
    module_lmd_intitule = serializers.CharField(source='module_lmd.intitule_fr',      read_only=True, default=None)

    class Meta:
        model  = EM
        fields = (
            'id', 'code_em', 'intitule',
            'CM', 'TD', 'TP', 'PR',
            'credits', 'coefficient',
            'seuil_eliminatoire',
            'has_tp',
            'departement', 'departement_nom', 'departement_annee', 'groupe',
            'filiere', 'filiere_id', 'filiere_nom',
            'module_filiere_id', 'module_filiere_nom',
            'semestre', 'semestre_nom',
            'niveau_id', 'niveau_nom',
            'module_lmd', 'module_lmd_code', 'module_lmd_intitule',
            'institution',
        )
        extra_kwargs = {
            'institution': {'required': False, 'allow_null': True},
        }

    def validate(self, attrs):
        # Defense-in-depth : injecter l'institution principale si absente.
        # Quand le scoping multi-institution sera complet, remplacer par
        # request.user.institution.
        if not attrs.get('institution'):
            from apps.parametres.models import Institution
            attrs['institution'] = Institution.objects.filter(est_principale=True).first()

        # Art. 8 Arrêté 562 / Art. 13 Décret 2018-070 : au plus 3 éléments par
        # module. Bloque uniquement l'AJOUT à un module (création ou
        # rattachement) — l'édition d'un EM existant d'un module legacy déjà
        # surchargé reste possible. Exception : un admin peut dépasser le plafond.
        from apps.evaluations.services.coherence_maquette import (
            MAX_ELEMENTS_PAR_MODULE, user_peut_depasser_max_elements,
        )
        request = self.context.get('request')
        is_admin = user_peut_depasser_max_elements(getattr(request, 'user', None))
        module_lmd = attrs.get('module_lmd')
        ajout = (
            module_lmd is not None
            and (self.instance is None or module_lmd.pk != self.instance.module_lmd_id)
        )
        if ajout and not is_admin:
            qs = EM.objects.filter(module_lmd=module_lmd)
            if self.instance is not None:
                qs = qs.exclude(pk=self.instance.pk)
            nb = qs.count()
            if nb >= MAX_ELEMENTS_PAR_MODULE:
                raise serializers.ValidationError({
                    'module_lmd': (
                        f'Le module {module_lmd.code} a déjà {nb} EM — maximum '
                        f'{MAX_ELEMENTS_PAR_MODULE} éléments par module '
                        f'(Art. 8 Arrêté 562 / Art. 13 Décret 2018-070).'
                    ),
                })
        return attrs
