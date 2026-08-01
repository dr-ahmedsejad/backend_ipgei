from rest_framework import serializers
from apps.parametres.models import Institution
from .models import (
    SessionEvaluation, Note, ResultatElement,
    ResultatSemestre, PVDeliberation, LigneDeliberation,
    ParametreJury, RachatNote, ResultatModule,
    MembreJury, ObligationRattrapage, AnonymatSession,
)


class SessionEvaluationSerializer(serializers.ModelSerializer):
    # Champs calculés (lecture seule)
    annee_universitaire = serializers.CharField(source='annee_univ.annee', read_only=True, default=None)
    est_cloturee        = serializers.BooleanField(source='est_close', read_only=True)

    # Alias date frontend ↔ date_debut_saisie / date_cloture_saisie
    date_debut = serializers.DateField(source='date_debut_saisie',   required=False, allow_null=True)
    date_fin   = serializers.DateField(source='date_cloture_saisie', required=False, allow_null=True)

    # Institution declaree explicitement avec required=False pour permettre l'auto-fill
    # (sinon DRF detecte FK NOT NULL et impose required=True meme avec extra_kwargs)
    institution = serializers.PrimaryKeyRelatedField(
        queryset=Institution.objects.all(),
        required=False, allow_null=True,
    )

    class Meta:
        model  = SessionEvaluation
        fields = (
            'id', 'code', 'intitule',
            'annee_univ', 'annee_universitaire',
            'institution',
            'type_session', 'type_semestre',
            'date_debut', 'date_fin',
            'est_ouverte', 'est_cloturee',
            # Instantané du plafond rattrapage figé sur la session (lecture seule).
            'rattrapage_plafond_actif', 'rattrapage_plafond',
        )
        read_only_fields = ('est_close', 'cloturee_par', 'est_cloturee', 'annee_universitaire',
                            'rattrapage_plafond_actif', 'rattrapage_plafond')
        # Desactive UniqueTogetherValidator de DRF : il exige que TOUS les champs du tuple
        # (institution, annee_univ, type_session, type_semestre) soient presents,
        # ce qui empeche l'auto-fill de institution dans create(). La contrainte est
        # toujours appliquee par MySQL (IntegrityError convertie en 400 par DRF).
        validators = []

    def create(self, validated_data):
        if not validated_data.get('institution'):
            from apps.parametres.models import Institution
            validated_data['institution'] = Institution.objects.filter(est_principale=True).first()
        return super().create(validated_data)

    def validate(self, attrs):
        """Verifie manuellement l'unicite (institution, annee_univ, type_session, type_semestre)
        apres l'auto-fill de institution si absent."""
        from apps.parametres.models import Institution
        institution = attrs.get('institution') or Institution.objects.filter(est_principale=True).first()
        annee = attrs.get('annee_univ')
        type_session = attrs.get('type_session')
        type_semestre = attrs.get('type_semestre')
        if institution and annee and type_session and type_semestre:
            qs = SessionEvaluation.objects.filter(
                institution=institution, annee_univ=annee,
                type_session=type_session, type_semestre=type_semestre,
            )
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    'Une session existe deja pour cette combinaison '
                    '(institution, annee, type_session, type_semestre).'
                )
        return attrs


class NoteSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Note
        fields = '__all__'
        read_only_fields = ('saisie_par', 'date_saisie', 'date_modification')

    def validate(self, attrs):
        session = attrs.get('session') or self.instance and self.instance.session
        if session and session.est_close:
            raise serializers.ValidationError(
                "La session est clôturée. La saisie de notes est impossible."
            )
        return attrs


class ResultatElementSerializer(serializers.ModelSerializer):
    class Meta:
        model   = ResultatElement
        fields  = '__all__'
        read_only_fields = ('note_finale', 'est_valide', 'est_eliminatoire', 'code_statut', 'date_calcul')


class ResultatSemestreSerializer(serializers.ModelSerializer):
    class Meta:
        model   = ResultatSemestre
        fields  = '__all__'
        read_only_fields = ('moyenne', 'credits_valides', 'est_admis', 'code_statut', 'date_calcul')


class ResultatModuleSerializer(serializers.ModelSerializer):
    module_code    = serializers.CharField(source='module.code',        read_only=True)
    module_intitule = serializers.CharField(source='module.intitule_fr', read_only=True)
    module_credits  = serializers.IntegerField(source='module.credits',  read_only=True)

    class Meta:
        model  = ResultatModule
        fields = (
            'id', 'inscription_ped', 'module', 'module_code', 'module_intitule', 'module_credits',
            'session', 'moyenne', 'credits_valides', 'est_valide', 'a_eliminatoire',
            'code_statut', 'date_calcul',
        )
        read_only_fields = (
            'moyenne', 'credits_valides', 'est_valide', 'a_eliminatoire', 'code_statut', 'date_calcul',
        )


class MembreJurySerializer(serializers.ModelSerializer):
    user_display = serializers.CharField(source='user.get_full_name', read_only=True)

    class Meta:
        model  = MembreJury
        fields = ('id', 'pv', 'user', 'user_display', 'role', 'signature_at')
        read_only_fields = ('signature_at',)


class ObligationRattrapageSerializer(serializers.ModelSerializer):
    element_code    = serializers.CharField(
        source='inscription_element.element.code', read_only=True,
    )
    element_intitule = serializers.CharField(
        source='inscription_element.element.intitule_fr', read_only=True,
    )

    class Meta:
        model  = ObligationRattrapage
        fields = (
            'id', 'ligne', 'inscription_element',
            'element_code', 'element_intitule',
            'type_obligation', 'code_statut_initial', 'motif',
        )


class LigneDeliberationSerializer(serializers.ModelSerializer):
    etudiant_nom       = serializers.CharField(
        source='inscription_admin.etudiant.nom', read_only=True,
    )
    etudiant_matricule = serializers.CharField(
        source='inscription_admin.etudiant.matricule', read_only=True,
    )
    etudiant_genre     = serializers.CharField(
        source='inscription_admin.etudiant.genre', read_only=True, default='',
    )
    obligations = ObligationRattrapageSerializer(many=True, read_only=True)
    credits_cycle = serializers.SerializerMethodField()

    def _niveau_fin_si_diplome(self, pv):
        """
        niveau_fin de la filière SI ce PV est l'année de diplôme (fin de cycle),
        None sinon. MÊME règle que le blocage / est_annee_diplome : PV annuel,
        dernier niveau, hors tronc commun (filière sans filles). Mémoïsé par PV
        sur l'instance du sérialiseur (many=True → une seule évaluation par PV).
        """
        cache = self.__dict__.setdefault('_diplome_cache', {})
        if pv.id not in cache:
            fil = pv.filiere
            nf  = (fil.niveau_fin or 3) if fil else None
            est = bool(
                fil and pv.type_pv == 'annuel'
                and pv.niveau == nf and not fil.filieres_filles.exists()
            )
            cache[pv.id] = nf if est else None
        return cache[pv.id]

    def get_credits_cycle(self, obj):
        """
        Total CONSOLIDÉ des crédits du cursus (/180) — affiché UNIQUEMENT sur le
        PV de fin de cycle (année de diplôme), None sinon. Même moteur que le
        relevé (DeliberationAnnuelleService._credits_capitalises_niveau →
        calculer_resultat_semestre_consolide : max SN/SR + compensation + report),
        et JAMAIS le décompte stocké (périmé). Cf. blocage diplôme Art. 25 / Art. 8.
        """
        niveau_fin = self._niveau_fin_si_diplome(obj.pv)
        if niveau_fin is None:
            return None
        from apps.evaluations.services.deliberation_annuelle import (
            DeliberationAnnuelleService,
        )
        ia = obj.inscription_admin
        return sum(
            DeliberationAnnuelleService._credits_capitalises_niveau(
                ia.etudiant, ia.annee_univ, n, institution=ia.institution,
            )
            for n in range(1, niveau_fin + 1)
        )

    class Meta:
        model  = LigneDeliberation
        fields = (
            'id', 'pv', 'inscription_admin',
            'etudiant_nom', 'etudiant_matricule', 'etudiant_genre',
            'decision', 'decision_annuelle',
            'moyenne_annuelle', 'credits_annuels', 'taux_capitalisation',
            'credits_cycle',
            'verrou_passage', 'code_statut',
            'obligations',
        )


class PVDeliberationSerializer(serializers.ModelSerializer):
    lignes         = LigneDeliberationSerializer(many=True, read_only=True)
    membres_jury   = MembreJurySerializer(many=True, read_only=True)
    filiere_nom          = serializers.CharField(source='filiere.intitule_fr',   read_only=True, default='')
    filiere_code         = serializers.CharField(source='filiere.code',          read_only=True, default='')
    filiere_type_diplome = serializers.CharField(source='filiere.type_diplome',  read_only=True, default='LP')
    session_code         = serializers.CharField(source='session.code',          read_only=True, default='')
    session_label        = serializers.CharField(source='session.intitule',      read_only=True, default='')
    session_type         = serializers.CharField(source='session.type_session',  read_only=True, default='')
    session_type_semestre = serializers.CharField(source='session.type_semestre', read_only=True, default='')
    annee_label          = serializers.CharField(source='annee_univ.annee',      read_only=True, default='')
    annee_effective      = serializers.SerializerMethodField()
    est_annee_diplome    = serializers.SerializerMethodField()
    president_nom        = serializers.CharField(source='president_jury.get_full_name', read_only=True, default='')

    def get_annee_effective(self, obj):
        """Année universitaire effective : directe (PV annuel) ou via la session (PV semestriel)."""
        if obj.annee_univ_id and obj.annee_univ:
            return obj.annee_univ.annee
        if obj.session_id and obj.session and obj.session.annee_univ_id:
            return obj.session.annee_univ.annee
        return ''

    def get_est_annee_diplome(self, obj):
        """
        True si ce PV est l'ANNÉE D'OBTENTION DU DIPLÔME (fin de cycle) — MÊME
        règle que le blocage diplôme (_bloquer_admis_non_eligible_diplome) :
          - PV annuel ;
          - niveau == filiere.niveau_fin (dernier niveau du cursus) ;
          - la filière n'a PAS de filières filles. Un tronc commun (filière AYANT
            des filles, ex. LPSTAT L1) n'est PAS une année de diplôme : le diplôme
            se joue chez la fille. À l'inverse, une filière AYANT un PARENT mais
            sans filles (la fille elle-même, ex. SDID L3) EST bien une année de
            diplôme à son niveau_fin — elle n'a pas de filles.
        Sert au libellé « Diplômé(e) » au lieu de « Passage de droit » côté UI.
        """
        fil = obj.filiere
        if not fil or obj.type_pv != 'annuel':
            return False
        niveau_fin = fil.niveau_fin or 3
        return obj.niveau == niveau_fin and not fil.filieres_filles.exists()

    class Meta:
        model  = PVDeliberation
        fields = (
            'id', 'type_pv', 'session', 'annee_univ', 'semestre_code',
            'filiere', 'filiere_nom', 'filiere_code', 'filiere_type_diplome',
            'institution',
            'niveau', 'president_jury', 'president_nom',
            'session_code', 'session_label', 'session_type', 'session_type_semestre',
            'annee_label', 'annee_effective', 'est_annee_diplome',
            'est_clos', 'lignes', 'membres_jury',
        )
        read_only_fields = ('est_clos',)
        extra_kwargs = {'institution': {'required': False}}

    def create(self, validated_data):
        if not validated_data.get('institution'):
            from apps.parametres.models import Institution
            filiere = validated_data.get('filiere')
            validated_data['institution'] = (
                (filiere.institution if filiere else None)
                or Institution.objects.filter(est_principale=True).first()
            )
        return super().create(validated_data)


class ParametreJurySerializer(serializers.ModelSerializer):
    class Meta:
        model  = ParametreJury
        fields = '__all__'


class RachatNoteSerializer(serializers.ModelSerializer):
    etudiant_nom        = serializers.CharField(
        source='ligne.inscription_admin.etudiant.nom', read_only=True,
    )
    etudiant_matricule  = serializers.CharField(
        source='ligne.inscription_admin.etudiant.matricule', read_only=True,
    )
    decidee_par_display = serializers.CharField(
        source='decidee_par.get_full_name', read_only=True,
    )

    class Meta:
        model  = RachatNote
        fields = [
            'id', 'pv', 'ligne',
            'etudiant_nom', 'etudiant_matricule',
            'ancienne_valeur', 'nouvelle_valeur', 'motif',
            'decidee_par', 'decidee_par_display',
            'date_decision',
        ]
        read_only_fields = ['date_decision', 'decidee_par']

    def create(self, validated_data):
        validated_data['decidee_par'] = self.context['request'].user
        return super().create(validated_data)


class AnonymatSessionSerializer(serializers.ModelSerializer):
    etudiant_nom       = serializers.CharField(
        source='inscription_admin.etudiant.nom', read_only=True,
    )
    etudiant_matricule = serializers.CharField(
        source='inscription_admin.etudiant.matricule', read_only=True,
    )

    class Meta:
        model  = AnonymatSession
        fields = (
            'id', 'session', 'inscription_admin',
            'etudiant_nom', 'etudiant_matricule',
            'numero_anonymat', 'genere_le',
        )
        read_only_fields = ('numero_anonymat', 'genere_le')
