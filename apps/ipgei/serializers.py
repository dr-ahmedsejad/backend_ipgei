"""Serializers de l'API IPGEI."""
from decimal import Decimal

from rest_framework import serializers

from .models import (
    AbsenceSeance, Classe, Deliberation, DetailLigneDeliberation, Evaluation,
    GrilleType, HistoriqueClasse, InscriptionIPGEI, LigneDeliberation, Matiere,
    Note, ParametresIPGEI, PermutationEtudiant, PermutationProf, SeanceReelle,
    SeanceType, SemaineIPGEI, SemestreIPGEI, SousGroupeTP,
    NIVEAU_CHOICES, PCT_DEFAUT_AVEC_TP, PCT_DEFAUT_SANS_TP,
)


class ChampNiveauPrepa(serializers.Field):
    """
    Expose le niveau d'une classe comme la chaîne « MPSI » ou « MP ».

    Depuis que `Classe` hérite de `departement.Departement`, le niveau est une
    clé étrangère vers `parametres.Niveau` et non plus une chaîne. Ce champ
    conserve le contrat d'API d'origine — le client continue d'envoyer et de
    recevoir un code — sans quoi tous les écrans IPGEI seraient à reprendre.

    La ligne de référentiel est créée au besoin plutôt que la saisie rejetée :
    MPSI et MP sont une constante du cursus, pas une donnée à administrer.
    """

    def to_representation(self, valeur):
        return valeur.niveau if valeur is not None else ''

    def to_internal_value(self, donnee):
        from .socle import niveau_socle

        codes = [code for code, _ in NIVEAU_CHOICES]
        if donnee not in codes:
            raise serializers.ValidationError(
                f"Niveau invalide. Valeurs attendues : {', '.join(codes)}."
            )
        return niveau_socle(donnee)


def _refuser_case_mixte(voisines, instance, sous_groupe):
    """
    Une case horaire est à la classe entière OU à ses sous-groupes, jamais aux
    deux.

    Si toute la classe est en cours, aucun de ses groupes ne peut être ailleurs
    au même moment — et réciproquement. Rien ne l'interdisait, et l'écran ne
    montrait pas les séances de l'autre plan : on pouvait donc poser un cours en
    classe entière sur un créneau déjà occupé par un TP dédoublé, sans rien voir.
    """
    if instance is not None:
        voisines = voisines.exclude(pk=instance.pk)

    if sous_groupe is None:
        conflit = voisines.filter(sous_groupe__isnull=False).first()
        if conflit is not None:
            raise serializers.ValidationError({'sous_groupe': (
                f'Ce créneau est déjà occupé par un enseignement en sous-groupe '
                f'({conflit.sous_groupe.groupe}). La classe entière ne peut pas '
                f'avoir cours au même moment.'
            )})
    else:
        conflit = voisines.filter(sous_groupe__isnull=True).first()
        if conflit is not None:
            raise serializers.ValidationError({'sous_groupe': (
                'Ce créneau porte déjà un cours en classe entière. Un sous-groupe '
                'ne peut pas avoir cours au même moment — retirez d’abord la '
                'séance de la classe.'
            )})


def _champ_sous_groupe():
    """
    Sous-groupe de TP — facultatif : une séance sans sous-groupe concerne la
    classe entière.

    Le `default=None` est indispensable, pas cosmétique : `sous_groupe` entre
    dans la contrainte d'unicité de la case horaire, et le UniqueTogetherValidator
    que DRF en dérive rend obligatoire tout champ qu'il couvre — sauf s'il porte
    une valeur par défaut.
    """
    return serializers.PrimaryKeyRelatedField(
        queryset=SousGroupeTP.objects.all(),
        required=False, allow_null=True, default=None,
    )


class InstitutionPrincipaleDefault:
    """
    Valeur par defaut du champ `institution` : l'etablissement principal (IPGEI).

    Necessaire — et pas seulement pratique : les contraintes d'unicite des
    modeles incluent `institution`, d'ou DRF derive un UniqueTogetherValidator
    qui rend obligatoire chaque champ couvert. Un `required=False` seul ne suffit
    donc pas ; il faut une valeur par defaut pour que le client puisse omettre
    le champ.
    """
    requires_context = False

    def __call__(self):
        from apps.parametres.models import Institution
        return Institution.objects.filter(est_principale=True).first()

    def __repr__(self):
        return self.__class__.__name__


def _champ_institution():
    from apps.parametres.models import Institution
    return serializers.PrimaryKeyRelatedField(
        queryset=Institution.objects.all(),
        required=False,
        default=InstitutionPrincipaleDefault(),
    )


# ── Paramètres ───────────────────────────────────────────────────────────────
class ParametresIPGEISerializer(serializers.ModelSerializer):
    class Meta:
        model  = ParametresIPGEI
        fields = [
            'id', 'institution', 'seuil_validation', 'plafond_rattrapage',
            'nb_semaines_defaut', 'droit_redoublement_max',
        ]


# ── Calendrier ───────────────────────────────────────────────────────────────
class _ListeSemaines(serializers.ListSerializer):
    """
    Calcule les états de cohérence en lot avant de sérialiser.

    Sans cela, chaque semaine interrogeait la base deux fois : trente-neuf
    requêtes et près d'une demi-seconde pour un semestre.
    """
    def to_representation(self, data):
        semaines = list(data)
        # `classe` vient de la vue : l'état doit décrire la classe qu'on
        # regarde, pas son niveau. Une classe sans emploi du temps ne peut pas
        # hériter du verdict de sa voisine.
        self.child.context['etats_coherence'] = SemaineIPGEI.etats_en_lot(
            semaines, classe=self.context.get('classe_coherence'))
        return super().to_representation(semaines)


class SemaineIPGEISerializer(serializers.ModelSerializer):
    semestre_code = serializers.CharField(source='semestre.code', read_only=True)
    # État de cohérence entre l'emploi du temps de la semaine et son suivi :
    # « previsionnel », « aligne » ou « divergent ». Exposé pour que l'écran
    # comme le PDF puissent dire ce que vaut ce qu'ils montrent.
    etat_coherence    = serializers.SerializerMethodField()
    libelle_coherence = serializers.SerializerMethodField()

    def get_etat_coherence(self, obj) -> str:
        # Le lot prime quand il est disponible ; sinon calcul unitaire, pour
        # que le serializer reste correct hors liste.
        etats = self.context.get('etats_coherence')
        if etats:
            return etats.get(obj.pk)
        return obj.etat_coherence_pour(self.context.get('classe_coherence'))

    def get_libelle_coherence(self, obj) -> str:
        return {
            SemaineIPGEI.ETAT_PREVISIONNEL: 'Prévisionnel — suivi non généré',
            SemaineIPGEI.ETAT_ALIGNE:       'Suivi généré et à jour',
            SemaineIPGEI.ETAT_DIVERGENT:    'Emploi du temps modifié après la génération du suivi',
        }[self.get_etat_coherence(obj)]

    class Meta:
        model       = SemaineIPGEI
        list_serializer_class = _ListeSemaines
        fields = [
            'id', 'semestre', 'semestre_code', 'numero', 'date_debut', 'date_fin',
            'type_semaine', 'description',
            'suivi_genere_le', 'etat_coherence', 'libelle_coherence',
        ]


class SemestreIPGEISerializer(serializers.ModelSerializer):
    institution   = _champ_institution()
    niveau        = serializers.CharField(read_only=True)
    type_semestre = serializers.CharField(read_only=True)
    nb_semaines_generees = serializers.SerializerMethodField()

    class Meta:
        model  = SemestreIPGEI
        fields = [
            'id', 'institution', 'code', 'annee_universitaire', 'date_debut', 'date_fin',
            'nb_semaines', 'est_cloture', 'niveau', 'type_semestre', 'nb_semaines_generees',
        ]

    def get_nb_semaines_generees(self, obj) -> int:
        return obj.semaines.count()

    def validate(self, attrs):
        debut = attrs.get('date_debut', getattr(self.instance, 'date_debut', None))
        fin   = attrs.get('date_fin',   getattr(self.instance, 'date_fin', None))
        if debut and fin and fin <= debut:
            raise serializers.ValidationError({'date_fin': 'La date de fin doit suivre la date de début.'})
        return attrs


# ── Matières ─────────────────────────────────────────────────────────────────
class MatiereSerializer(serializers.ModelSerializer):
    institution   = _champ_institution()
    niveau = serializers.CharField(read_only=True)
    # Total calcule : c'est la somme CM+TD+TP, jamais une valeur saisie.
    volume_horaire = serializers.IntegerField(read_only=True)

    class Meta:
        model  = Matiere
        fields = [
            'id', 'institution', 'code', 'intitule', 'intitule_ar', 'code_semestre',
            'niveau', 'coefficient', 'volume_cm', 'volume_td', 'volume_tp',
            'volume_horaire', 'has_tp',
            'pct_ds', 'pct_tp', 'pct_exam', 'ordre', 'actif',
        ]

    def validate(self, attrs):
        def valeur(champ, defaut=None):
            if champ in attrs:
                return attrs[champ]
            return getattr(self.instance, champ, defaut)

        has_tp = valeur('has_tp', False)
        # Sur une création sans pondération explicite, on applique le défaut du
        # cadrage (30/70 sans TP, 20/10/70 avec TP) plutôt que d'exiger la saisie.
        defauts = PCT_DEFAUT_AVEC_TP if has_tp else PCT_DEFAUT_SANS_TP
        if self.instance is None:
            for champ, defaut in defauts.items():
                attrs.setdefault(champ, defaut)

        pct_ds   = Decimal(valeur('pct_ds',   defauts['pct_ds']))
        pct_tp   = Decimal(valeur('pct_tp',   defauts['pct_tp']))
        pct_exam = Decimal(valeur('pct_exam', defauts['pct_exam']))

        if not has_tp and pct_tp != Decimal('0'):
            raise serializers.ValidationError(
                {'pct_tp': 'Sans TP, la part TP doit valoir 0 %.'}
            )
        total = pct_ds + pct_tp + pct_exam
        if total != Decimal('100'):
            raise serializers.ValidationError(
                f'La pondération doit totaliser 100 % (actuellement {total} %).'
            )
        return attrs


class MatiereSelectSerializer(serializers.ModelSerializer):
    """Version légère pour les listes déroulantes."""
    class Meta:
        model  = Matiere
        fields = ['id', 'code', 'intitule', 'code_semestre', 'coefficient', 'has_tp',
                  'volume_cm', 'volume_td', 'volume_tp']


# ── Classes & sous-groupes ───────────────────────────────────────────────────
class SousGroupeTPSerializer(serializers.ModelSerializer):
    classe_nom       = serializers.CharField(source='classe.nom', read_only=True)
    matieres_detail  = MatiereSelectSerializer(source='matieres', many=True, read_only=True)
    effectif         = serializers.SerializerMethodField()
    # Le libellé (G1, G2…) est stocké dans le champ hérité `groupe` — c'est ce
    # champ que le socle lit pour distinguer deux moitiés d'un TP dédoublé.
    libelle          = serializers.CharField(source='groupe', max_length=20)

    class Meta:
        model  = SousGroupeTP
        fields = ['id', 'classe', 'classe_nom', 'libelle', 'matieres', 'matieres_detail', 'effectif']

    def get_effectif(self, obj) -> int:
        return obj.inscriptions.filter(actif=True).count()

    def validate(self, attrs):
        """
        Unicité (classe, libellé).

        Elle était portée par une contrainte de modèle, impossible à conserver
        depuis l'héritage : `classe` est un champ local et `groupe` un champ du
        parent, or Django refuse de les mêler dans une même contrainte. La base
        garde le dernier mot via `cle_unicite` ; cette vérification sert à
        rendre une 400 lisible plutôt qu'une erreur d'intégrité.
        """
        classe  = attrs.get('classe',  getattr(self.instance, 'classe',  None))
        libelle = attrs.get('groupe',  getattr(self.instance, 'groupe', None))
        doublons = SousGroupeTP.objects.filter(classe=classe, groupe=libelle)
        if self.instance is not None:
            doublons = doublons.exclude(pk=self.instance.pk)
        if doublons.exists():
            raise serializers.ValidationError(
                {'libelle': f"Le sous-groupe « {libelle} » existe déjà dans cette classe."}
            )
        return attrs


class ClasseSerializer(serializers.ModelSerializer):
    institution         = _champ_institution()
    niveau              = ChampNiveauPrepa()
    # Suffixe de classe (A, B…), porté par le champ hérité `groupe`.
    libelle             = serializers.CharField(source='groupe', max_length=20)
    nom                 = serializers.CharField(read_only=True)
    effectif            = serializers.SerializerMethodField()
    sous_groupes        = SousGroupeTPSerializer(many=True, read_only=True)
    professeur_principal_nom = serializers.CharField(
        source='professeur_principal.nom', read_only=True, default='',
    )

    class Meta:
        model  = Classe
        fields = [
            'id', 'institution', 'niveau', 'libelle', 'nom', 'annee_universitaire',
            'capacite', 'professeur_principal', 'professeur_principal_nom',
            'actif', 'effectif', 'sous_groupes', 'date_creation',
        ]

    def get_effectif(self, obj) -> int:
        # `effectif_annote` vient du queryset de la vue (un seul COUNT groupe) ;
        # la propriete du modele sert de repli hors de ce contexte.
        annote = getattr(obj, 'effectif_annote', None)
        return annote if annote is not None else obj.effectif

    def validate(self, attrs):
        """
        Unicité (institution, année, niveau, libellé) — même raison que sur
        `SousGroupeTPSerializer` : la contrainte ne peut plus vivre sur le
        modèle, `cle_unicite` la garantit en base et ce contrôle la rend lisible.
        """
        def valeur(champ, defaut_attr=None):
            return attrs.get(champ, getattr(self.instance, defaut_attr or champ, None))

        doublons = Classe.objects.filter(
            institution=valeur('institution'),
            annee_universitaire=valeur('annee_universitaire'),
            niveau=valeur('niveau'),
            groupe=valeur('groupe'),
        )
        if self.instance is not None:
            doublons = doublons.exclude(pk=self.instance.pk)
        if doublons.exists():
            raise serializers.ValidationError(
                {'libelle': "Cette classe existe déjà pour ce niveau et cette année."}
            )
        return attrs


class ClasseSelectSerializer(serializers.ModelSerializer):
    nom     = serializers.CharField(read_only=True)
    niveau  = ChampNiveauPrepa(read_only=True)
    libelle = serializers.CharField(source='groupe', read_only=True)

    class Meta:
        model  = Classe
        fields = ['id', 'nom', 'niveau', 'libelle', 'annee_universitaire']


# ── Inscriptions ─────────────────────────────────────────────────────────────
class NouvelEtudiantSerializer(serializers.Serializer):
    """Identité saisie lors d'une inscription d'un étudiant encore inconnu."""
    matricule      = serializers.CharField(max_length=50)
    nom            = serializers.CharField(max_length=200)
    prenom_fr      = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    nom_ar         = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    genre          = serializers.ChoiceField(choices=[('M', 'Masculin'), ('F', 'Féminin')], default='M')
    date_naissance = serializers.DateField(required=False, allow_null=True)
    lieu_naissance_fr = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    cni            = serializers.CharField(max_length=20, required=False, allow_blank=True, allow_null=True)
    telephone      = serializers.CharField(max_length=20, required=False, allow_blank=True, default='')
    email          = serializers.EmailField(required=False, allow_blank=True, default='')
    nbac           = serializers.CharField(max_length=20, required=False, allow_blank=True, allow_null=True)
    serie_bac      = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')
    moyenne_bac    = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, allow_null=True,
        min_value=Decimal('0'), max_value=Decimal('20'),
    )

    def validate_matricule(self, valeur):
        from apps.absence.models import Etudiant
        valeur = valeur.strip()
        if Etudiant.objects.filter(matricule__iexact=valeur).exists():
            raise serializers.ValidationError(
                f'Le matricule {valeur} est déjà attribué. Utilisez la recherche pour '
                'inscrire cet étudiant plutôt que d\'en créer un nouveau.'
            )
        return valeur


class InscriptionCompleteSerializer(serializers.Serializer):
    """
    Inscription en une passe : on reçoit soit un étudiant déjà au référentiel
    (`etudiant`), soit son identité à créer (`nouvel_etudiant`).

    Les deux cas sont traités par le même endpoint et dans la même transaction :
    créer la fiche étudiant puis échouer sur le rattachement laisserait sinon un
    étudiant orphelin, invisible et impossible à réinscrire (matricule pris).
    """
    etudiant          = serializers.IntegerField(required=False, allow_null=True)
    nouvel_etudiant   = NouvelEtudiantSerializer(required=False)
    classe            = serializers.IntegerField()
    sous_groupe       = serializers.IntegerField(required=False, allow_null=True)
    numero_ordre      = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, attrs):
        if not attrs.get('etudiant') and not attrs.get('nouvel_etudiant'):
            raise serializers.ValidationError(
                'Indiquez un étudiant existant ou les informations du nouvel étudiant.'
            )
        if attrs.get('etudiant') and attrs.get('nouvel_etudiant'):
            raise serializers.ValidationError(
                'Choisissez soit un étudiant existant, soit une création — pas les deux.'
            )
        return attrs


class HistoriqueClasseSerializer(serializers.ModelSerializer):
    classe_avant_nom = serializers.CharField(source='classe_avant.nom', read_only=True)
    classe_apres_nom = serializers.CharField(source='classe_apres.nom', read_only=True)
    decide_par_nom   = serializers.CharField(source='decide_par.name', read_only=True, default='')

    class Meta:
        model  = HistoriqueClasse
        fields = [
            'id', 'inscription', 'classe_avant', 'classe_avant_nom',
            'classe_apres', 'classe_apres_nom', 'motif', 'decide_par',
            'decide_par_nom', 'date_effet',
        ]


class InscriptionIPGEISerializer(serializers.ModelSerializer):
    etudiant_nom       = serializers.CharField(source='etudiant.nom_display', read_only=True)
    etudiant_matricule = serializers.CharField(source='etudiant.matricule', read_only=True)
    classe_nom         = serializers.CharField(source='classe.nom', read_only=True)
    sous_groupe_libelle = serializers.CharField(source='sous_groupe.libelle', read_only=True, default='')
    niveau             = serializers.CharField(read_only=True)
    statut_display     = serializers.CharField(source='get_statut_display', read_only=True)

    class Meta:
        model  = InscriptionIPGEI
        fields = [
            'id', 'etudiant', 'etudiant_nom', 'etudiant_matricule', 'classe', 'classe_nom',
            'sous_groupe', 'sous_groupe_libelle', 'annee_universitaire', 'numero_ordre',
            'niveau', 'statut', 'statut_display', 'nb_redoublements',
            'date_inscription', 'actif',
        ]

    def validate(self, attrs):
        classe      = attrs.get('classe',      getattr(self.instance, 'classe', None))
        sous_groupe = attrs.get('sous_groupe', getattr(self.instance, 'sous_groupe', None))
        if sous_groupe and classe and sous_groupe.classe_id != classe.id:
            raise serializers.ValidationError(
                {'sous_groupe': "Ce sous-groupe n'appartient pas à la classe choisie."}
            )
        # L'année de l'inscription suit celle de la classe : les laisser diverger
        # ferait sortir l'étudiant des délibérations de sa propre promotion.
        if classe is not None:
            attrs.setdefault('annee_universitaire', classe.annee_universitaire)
            if attrs.get('annee_universitaire') != classe.annee_universitaire:
                raise serializers.ValidationError(
                    {'annee_universitaire': "L'année doit être celle de la classe "
                                            f'({classe.annee_universitaire}).'}
                )
        return attrs


# ── Notes ────────────────────────────────────────────────────────────────────
class EvaluationSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Evaluation
        fields = [
            'id', 'note', 'type_evaluation', 'numero', 'valeur',
            'date_evaluation', 'saisie_par', 'date_saisie',
        ]
        read_only_fields = ['saisie_par', 'date_saisie']

    def validate(self, attrs):
        note = attrs.get('note', getattr(self.instance, 'note', None))
        if note is not None and note.verrouillee:
            raise serializers.ValidationError('Note verrouillée par une délibération validée.')
        if note is not None and note.semestre.est_cloture:
            raise serializers.ValidationError('Semestre clôturé — saisie impossible.')
        return attrs


class NoteSerializer(serializers.ModelSerializer):
    evaluations        = EvaluationSerializer(many=True, read_only=True)
    matiere_code       = serializers.CharField(source='matiere.code', read_only=True)
    matiere_intitule   = serializers.CharField(source='matiere.intitule', read_only=True)
    matiere_has_tp     = serializers.BooleanField(source='matiere.has_tp', read_only=True)
    matiere_coefficient = serializers.DecimalField(
        source='matiere.coefficient', max_digits=4, decimal_places=2, read_only=True,
    )
    etudiant_nom       = serializers.CharField(source='inscription.etudiant.nom_display', read_only=True)
    etudiant_matricule = serializers.CharField(source='inscription.etudiant.matricule', read_only=True)
    semestre_code      = serializers.CharField(source='semestre.code', read_only=True)

    class Meta:
        model  = Note
        fields = [
            'id', 'inscription', 'matiere', 'semestre',
            'matiere_code', 'matiere_intitule', 'matiere_has_tp', 'matiere_coefficient',
            'etudiant_nom', 'etudiant_matricule', 'semestre_code',
            'note_tp', 'note_rattrapage', 'moy_ds', 'moy_exam', 'moyenne', 'note_retenue',
            'pct_ds', 'pct_tp', 'pct_exam', 'coefficient', 'plafond_rattrapage_applique',
            'verrouillee', 'date_calcul', 'evaluations',
        ]
        read_only_fields = [
            'moy_ds', 'moy_exam', 'moyenne', 'note_retenue',
            'pct_ds', 'pct_tp', 'pct_exam', 'coefficient',
            'plafond_rattrapage_applique', 'verrouillee', 'date_calcul',
        ]

    def validate(self, attrs):
        instance = self.instance
        if instance is not None and instance.verrouillee:
            raise serializers.ValidationError('Note verrouillée par une délibération validée.')
        semestre = attrs.get('semestre', getattr(instance, 'semestre', None))
        matiere  = attrs.get('matiere',  getattr(instance, 'matiere', None))
        if semestre is not None and semestre.est_cloture:
            raise serializers.ValidationError('Semestre clôturé — saisie impossible.')
        if semestre is not None and matiere is not None and matiere.code_semestre != semestre.code:
            raise serializers.ValidationError(
                {'matiere': f'La matière {matiere.code} appartient au semestre '
                            f'{matiere.code_semestre}, pas à {semestre.code}.'}
            )
        return attrs


class SaisieEvaluationSerializer(serializers.Serializer):
    """Une case de la grille de saisie collective."""
    type_evaluation = serializers.ChoiceField(choices=Evaluation.TYPE_CHOICES)
    numero          = serializers.IntegerField(min_value=1, max_value=20)
    valeur          = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('20'),
        allow_null=True,
    )


class SaisieNoteSerializer(serializers.Serializer):
    """Ligne de la saisie collective : un étudiant dans une matière."""
    inscription     = serializers.IntegerField()
    note_tp         = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('20'),
        allow_null=True, required=False,
    )
    note_rattrapage = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('20'),
        allow_null=True, required=False,
    )
    evaluations     = SaisieEvaluationSerializer(many=True, required=False)


class SaisieCollectiveSerializer(serializers.Serializer):
    """Saisie d'une matière pour toute une classe, en une requête."""
    classe   = serializers.IntegerField()
    matiere  = serializers.IntegerField()
    semestre = serializers.IntegerField()
    lignes   = SaisieNoteSerializer(many=True)


# ── Délibération ─────────────────────────────────────────────────────────────
class DetailLigneDeliberationSerializer(serializers.ModelSerializer):
    matiere_code     = serializers.CharField(source='matiere.code', read_only=True, default='')
    matiere_intitule = serializers.CharField(source='matiere.intitule', read_only=True, default='')
    semestre_code    = serializers.CharField(source='semestre.code', read_only=True)

    class Meta:
        model  = DetailLigneDeliberation
        fields = [
            'id', 'matiere', 'matiere_code', 'matiere_intitule',
            'semestre', 'semestre_code', 'note_retenue', 'coefficient',
        ]


class LigneDeliberationSerializer(serializers.ModelSerializer):
    etudiant_nom       = serializers.CharField(source='inscription.etudiant.nom_display', read_only=True)
    etudiant_matricule = serializers.CharField(source='inscription.etudiant.matricule', read_only=True)
    classe_nom         = serializers.CharField(source='inscription.classe.nom', read_only=True)
    decision_display   = serializers.CharField(source='get_decision_display', read_only=True)
    decision_auto_display = serializers.CharField(source='get_decision_auto_display', read_only=True)
    est_ajustee        = serializers.BooleanField(read_only=True)
    nb_redoublements   = serializers.IntegerField(source='inscription.nb_redoublements', read_only=True)
    details            = DetailLigneDeliberationSerializer(many=True, read_only=True)

    class Meta:
        model  = LigneDeliberation
        fields = [
            'id', 'deliberation', 'inscription', 'etudiant_nom', 'etudiant_matricule',
            'classe_nom', 'moyenne_generale', 'total_coefficients', 'rang',
            'decision_auto', 'decision_auto_display', 'decision', 'decision_display',
            'est_ajustee', 'motif_ajustement', 'observations', 'nb_redoublements', 'details',
        ]
        read_only_fields = [
            'deliberation', 'inscription', 'moyenne_generale', 'total_coefficients',
            'rang', 'decision_auto',
        ]

    def validate(self, attrs):
        instance = self.instance
        if instance and instance.deliberation.est_verrouillee:
            raise serializers.ValidationError('Délibération validée — modification interdite.')
        decision = attrs.get('decision', getattr(instance, 'decision', ''))
        auto     = getattr(instance, 'decision_auto', '')
        motif    = attrs.get('motif_ajustement', getattr(instance, 'motif_ajustement', ''))
        # Un écart avec la proposition automatique doit être motivé : c'est ce
        # qui rend la décision du jury défendable en cas de contestation.
        if decision and auto and decision != auto and not (motif or '').strip():
            raise serializers.ValidationError(
                {'motif_ajustement': 'Motif obligatoire pour déroger à la décision calculée.'}
            )
        return attrs


class LigneDeliberationListSerializer(LigneDeliberationSerializer):
    """Même chose sans le détail par matière (listes de jury)."""
    class Meta(LigneDeliberationSerializer.Meta):
        fields = [f for f in LigneDeliberationSerializer.Meta.fields if f != 'details']


class DeliberationSerializer(serializers.ModelSerializer):
    institution      = _champ_institution()
    semestre_code    = serializers.CharField(source='semestre.code', read_only=True, default='')
    statut_display   = serializers.CharField(source='get_statut_display', read_only=True)
    validee_par_nom  = serializers.CharField(source='validee_par.name', read_only=True, default='')
    nb_lignes        = serializers.SerializerMethodField()
    est_verrouillee  = serializers.BooleanField(read_only=True)

    class Meta:
        model  = Deliberation
        fields = [
            'id', 'institution', 'libelle', 'niveau', 'annee_universitaire', 'portee',
            'semestre', 'semestre_code', 'seuil_validation', 'plafond_rattrapage',
            'statut', 'statut_display', 'est_verrouillee', 'observations',
            'date_creation', 'date_calcul', 'date_validation',
            'validee_par', 'validee_par_nom', 'nb_lignes',
        ]
        read_only_fields = ['statut', 'date_calcul', 'date_validation', 'validee_par']

    def get_nb_lignes(self, obj) -> int:
        return obj.lignes.count()

    def validate(self, attrs):
        portee   = attrs.get('portee',   getattr(self.instance, 'portee', None))
        semestre = attrs.get('semestre', getattr(self.instance, 'semestre', None))
        if portee == Deliberation.PORTEE_SEMESTRE and semestre is None:
            raise serializers.ValidationError(
                {'semestre': 'Une délibération de semestre doit cibler un semestre.'}
            )
        if portee == Deliberation.PORTEE_ANNUELLE:
            attrs['semestre'] = None
        return attrs


# ── EDT ──────────────────────────────────────────────────────────────────────
def _exiger_matiere(type_seance, matiere):
    """
    La matière n'est facultative que pour une séance spéciale.

    Le modèle l'autorise à être vide — sport et instruction militaire bloquent
    un créneau sans relever de la maquette. Mais rendre la colonne nullable a
    aussi levé l'exigence pour les autres types : sans cette règle, un CM
    pouvait être enregistré sans matière et disparaître de l'avancement.
    """
    if matiere is None and not (type_seance and getattr(type_seance, 'is_special', False)):
        raise serializers.ValidationError(
            {'matiere': 'La matière est obligatoire pour ce type de séance.'}
        )


class SeanceTypeSerializer(serializers.ModelSerializer):
    sous_groupe      = _champ_sous_groupe()
    # Libellé du référentiel (« CM », « TP », « Sport »…) et caractère spécial :
    # une séance spéciale n'a ni enseignant ni salle, l'écran doit le savoir
    # pour masquer ces champs plutôt que les proposer en vain.
    type_seance_libelle = serializers.CharField(
        source='type_seance.type_seance', read_only=True, default='')
    type_seance_special = serializers.BooleanField(
        source='type_seance.is_special', read_only=True, default=False)
    jour_libelle     = serializers.CharField(source='jour.jour', read_only=True)
    creneau_libelle  = serializers.CharField(source='creneau.creneau', read_only=True)
    matiere_code     = serializers.CharField(source='matiere.code', read_only=True, default='')
    prof_nom         = serializers.CharField(source='prof.nom', read_only=True, default='')
    salle_nom        = serializers.CharField(source='salle.nom', read_only=True, default='')
    sous_groupe_libelle = serializers.CharField(source='sous_groupe.libelle', read_only=True, default='')

    class Meta:
        model  = SeanceType
        fields = [
            'id', 'grille', 'jour', 'jour_libelle', 'creneau', 'creneau_libelle',
            'matiere', 'matiere_code', 'prof', 'prof_nom', 'salle', 'salle_nom',
            'sous_groupe', 'sous_groupe_libelle',
            'type_seance', 'type_seance_libelle', 'type_seance_special',
        ]

    def validate(self, attrs):
        grille      = attrs.get('grille',      getattr(self.instance, 'grille', None))
        sous_groupe = attrs.get('sous_groupe', getattr(self.instance, 'sous_groupe', None))
        jour        = attrs.get('jour',        getattr(self.instance, 'jour', None))
        creneau     = attrs.get('creneau',     getattr(self.instance, 'creneau', None))

        _exiger_matiere(attrs.get('type_seance', getattr(self.instance, 'type_seance', None)),
                        attrs.get('matiere',     getattr(self.instance, 'matiere', None)))

        if grille and sous_groupe and sous_groupe.classe_id != grille.classe_id:
            raise serializers.ValidationError(
                {'sous_groupe': "Ce sous-groupe n'appartient pas à la classe de la grille."}
            )

        _refuser_case_mixte(
            SeanceType.objects.filter(grille=grille, jour=jour, creneau=creneau),
            self.instance, sous_groupe,
        )
        return attrs


class GrilleTypeSerializer(serializers.ModelSerializer):
    classe_nom = serializers.CharField(source='classe.nom', read_only=True)
    nb_seances = serializers.SerializerMethodField()
    seances    = SeanceTypeSerializer(many=True, read_only=True)

    class Meta:
        model  = GrilleType
        fields = [
            'id', 'classe', 'classe_nom', 'type_semestre', 'libelle',
            'actif', 'nb_seances', 'seances', 'date_creation',
        ]

    def get_nb_seances(self, obj) -> int:
        return obj.seances.count()


class GrilleTypeListSerializer(GrilleTypeSerializer):
    class Meta(GrilleTypeSerializer.Meta):
        fields = [f for f in GrilleTypeSerializer.Meta.fields if f != 'seances']


class SeanceReelleSerializer(serializers.ModelSerializer):
    sous_groupe      = _champ_sous_groupe()
    # Libellé du référentiel (« CM », « TP », « Sport »…) et caractère spécial :
    # une séance spéciale n'a ni enseignant ni salle, l'écran doit le savoir
    # pour masquer ces champs plutôt que les proposer en vain.
    type_seance_libelle = serializers.CharField(
        source='type_seance.type_seance', read_only=True, default='')
    type_seance_special = serializers.BooleanField(
        source='type_seance.is_special', read_only=True, default=False)
    classe_nom       = serializers.CharField(source='classe.nom', read_only=True)
    jour_libelle     = serializers.CharField(source='jour.jour', read_only=True)
    creneau_libelle  = serializers.CharField(source='creneau.creneau', read_only=True)
    creneau_ordre    = serializers.IntegerField(source='creneau.ordre', read_only=True)
    matiere_code     = serializers.CharField(source='matiere.code', read_only=True, default='')
    matiere_intitule = serializers.CharField(source='matiere.intitule', read_only=True, default='')
    prof_nom         = serializers.CharField(source='prof.nom', read_only=True, default='')
    prof_initial_nom = serializers.CharField(source='prof_initial.nom', read_only=True, default='')
    salle_nom        = serializers.CharField(source='salle.nom', read_only=True, default='')
    sous_groupe_libelle = serializers.CharField(source='sous_groupe.libelle', read_only=True, default='')
    semaine_numero   = serializers.IntegerField(source='semaine.numero', read_only=True)
    nb_absents       = serializers.SerializerMethodField()

    class Meta:
        model  = SeanceReelle
        fields = [
            'id', 'classe', 'classe_nom', 'semaine', 'semaine_numero',
            'jour', 'jour_libelle', 'creneau', 'creneau_libelle', 'creneau_ordre',
            'matiere', 'matiere_code', 'matiere_intitule',
            'prof', 'prof_nom', 'prof_initial', 'prof_initial_nom',
            'salle', 'salle_nom', 'sous_groupe', 'sous_groupe_libelle',
            'type_seance', 'type_seance_libelle', 'type_seance_special',
            'date', 'origine', 'annulee', 'observations',
            'nb_absents',
        ]
        read_only_fields = ['prof_initial']
        # DRF dérive un validateur d'unicité de chaque UniqueConstraint du
        # modèle — mais en IGNORANT sa condition. La contrainte partielle qui
        # protège les cases de classe entière (`sous_groupe IS NULL`) se
        # transformait donc en interdiction générale, et ajouter le second
        # groupe d'un TP dédoublé était refusé. L'unicité reste garantie par la
        # base ; `validate()` ci-dessous la formule en messages lisibles.
        validators = []

    def validate(self, attrs):
        """
        Deux règles sur une même case horaire.

        L'une contre le doublon, l'autre contre le mélange : une case appartient
        à la classe entière OU à ses sous-groupes, jamais aux deux.
        """
        def valeur(champ):
            return attrs.get(champ, getattr(self.instance, champ, None))

        _exiger_matiere(valeur('type_seance'), valeur('matiere'))

        sous_groupe = valeur('sous_groupe')
        voisines = SeanceReelle.objects.filter(
            classe=valeur('classe'), semaine=valeur('semaine'),
            jour=valeur('jour'), creneau=valeur('creneau'),
        )
        if self.instance is not None:
            voisines = voisines.exclude(pk=self.instance.pk)

        if voisines.filter(sous_groupe=sous_groupe).exists():
            cible = f'le sous-groupe {sous_groupe.groupe}' if sous_groupe else 'la classe entière'
            raise serializers.ValidationError(
                {'creneau': f'Une séance existe déjà sur ce créneau pour {cible}.'}
            )

        _refuser_case_mixte(voisines, self.instance, sous_groupe)
        return attrs

    def get_nb_absents(self, obj) -> int:
        # `nb_absents_annote` vient du queryset de la vue (un seul COUNT groupe).
        # Sans lui, afficher une semaine de 30 seances declencherait 30 requetes.
        annote = getattr(obj, 'nb_absents_annote', None)
        return annote if annote is not None else obj.absences.count()


class DupliquerGrilleSerializer(serializers.Serializer):
    """Duplication de la grille type sur une semaine ou un lot de N semaines."""
    semestre       = serializers.IntegerField()
    semaine_debut  = serializers.IntegerField(
        required=False, allow_null=True,
        help_text='Semaine de départ. Vide = tout le semestre.',
    )
    nb_semaines    = serializers.IntegerField(required=False, min_value=1, max_value=40)
    ecraser        = serializers.BooleanField(default=False)


class EditionLotSerializer(serializers.Serializer):
    """Applique la modification d'une séance à N semaines consécutives."""
    nb_semaines = serializers.IntegerField(min_value=1, max_value=40, default=1)
    prof        = serializers.IntegerField(required=False, allow_null=True)
    salle       = serializers.IntegerField(required=False, allow_null=True)
    matiere     = serializers.IntegerField(required=False, allow_null=True)
    annulee     = serializers.BooleanField(required=False)


# ── Permutations ─────────────────────────────────────────────────────────────
class PermutationProfSerializer(serializers.ModelSerializer):
    seance_a_detail = SeanceReelleSerializer(source='seance_a', read_only=True)
    seance_b_detail = SeanceReelleSerializer(source='seance_b', read_only=True)
    statut_display  = serializers.CharField(source='get_statut_display', read_only=True)
    demande_par_nom = serializers.CharField(source='demande_par.name', read_only=True, default='')
    valide_par_nom  = serializers.CharField(source='valide_par.name', read_only=True, default='')

    class Meta:
        model  = PermutationProf
        fields = [
            'id', 'seance_a', 'seance_b', 'seance_a_detail', 'seance_b_detail',
            'nb_semaines', 'statut', 'statut_display', 'action_directe', 'motif',
            'motif_refus', 'demande_par', 'demande_par_nom', 'accord_par',
            'valide_par', 'valide_par_nom', 'seances_impactees',
            'date_demande', 'date_accord', 'date_validation', 'date_application',
        ]
        read_only_fields = [
            'statut', 'demande_par', 'accord_par', 'valide_par', 'seances_impactees',
            'date_accord', 'date_validation', 'date_application',
        ]

    def validate(self, attrs):
        a = attrs.get('seance_a', getattr(self.instance, 'seance_a', None))
        b = attrs.get('seance_b', getattr(self.instance, 'seance_b', None))
        if a and b:
            if a.pk == b.pk:
                raise serializers.ValidationError('Les deux séances doivent être distinctes.')
            if a.classe_id != b.classe_id:
                raise serializers.ValidationError(
                    'La permutation ne concerne que deux séances de la même classe.'
                )
            if a.creneau_id != b.creneau_id:
                raise serializers.ValidationError(
                    'Le créneau est conservé : les deux séances doivent partager le même créneau.'
                )
        return attrs


class PermutationEtudiantSerializer(serializers.ModelSerializer):
    etudiant_nom       = serializers.CharField(source='inscription.etudiant.nom_display', read_only=True)
    etudiant_matricule = serializers.CharField(source='inscription.etudiant.matricule', read_only=True)
    classe_source_nom  = serializers.CharField(source='classe_source.nom', read_only=True)
    classe_cible_nom   = serializers.CharField(source='classe_cible.nom', read_only=True)
    statut_display     = serializers.CharField(source='get_statut_display', read_only=True)
    demande_par_nom    = serializers.CharField(source='demande_par.name', read_only=True, default='')

    class Meta:
        model  = PermutationEtudiant
        fields = [
            'id', 'inscription', 'etudiant_nom', 'etudiant_matricule',
            'classe_source', 'classe_source_nom', 'classe_cible', 'classe_cible_nom',
            'sous_groupe_cible', 'statut', 'statut_display', 'action_directe',
            'motif', 'motif_refus', 'demande_par', 'demande_par_nom', 'accord_par',
            'valide_par', 'date_demande', 'date_accord', 'date_validation', 'date_application',
        ]
        read_only_fields = [
            'statut', 'classe_source', 'demande_par', 'accord_par', 'valide_par',
            'date_accord', 'date_validation', 'date_application',
        ]

    def validate(self, attrs):
        inscription = attrs.get('inscription', getattr(self.instance, 'inscription', None))
        cible       = attrs.get('classe_cible', getattr(self.instance, 'classe_cible', None))
        sg_cible    = attrs.get('sous_groupe_cible', getattr(self.instance, 'sous_groupe_cible', None))
        if inscription and cible:
            if inscription.classe_id == cible.id:
                raise serializers.ValidationError(
                    {'classe_cible': "L'étudiant est déjà dans cette classe."}
                )
            if inscription.classe.niveau_code != cible.niveau_code:
                raise serializers.ValidationError(
                    {'classe_cible': 'Un changement de classe reste au même niveau (MPSI ou MP).'}
                )
            if inscription.classe.annee_universitaire != cible.annee_universitaire:
                raise serializers.ValidationError(
                    {'classe_cible': 'La classe cible doit être de la même année universitaire.'}
                )
        if sg_cible and cible and sg_cible.classe_id != cible.id:
            raise serializers.ValidationError(
                {'sous_groupe_cible': "Ce sous-groupe n'appartient pas à la classe cible."}
            )
        return attrs


# ── Absences ─────────────────────────────────────────────────────────────────
class AbsenceSeanceSerializer(serializers.ModelSerializer):
    etudiant_nom       = serializers.CharField(source='inscription.etudiant.nom_display', read_only=True)
    etudiant_matricule = serializers.CharField(source='inscription.etudiant.matricule', read_only=True)
    statut_display     = serializers.CharField(source='get_statut_display', read_only=True)
    matiere_code       = serializers.CharField(source='seance.matiere.code', read_only=True, default='')
    date_seance        = serializers.DateField(source='seance.date', read_only=True)
    creneau_libelle    = serializers.CharField(source='seance.creneau.creneau', read_only=True)

    class Meta:
        model  = AbsenceSeance
        fields = [
            'id', 'seance', 'inscription', 'etudiant_nom', 'etudiant_matricule',
            'statut', 'statut_display', 'justificatif', 'matiere_code',
            'date_seance', 'creneau_libelle', 'saisie_par', 'date_saisie',
        ]
        read_only_fields = ['saisie_par', 'date_saisie']


class LigneSaisieAbsenceSerializer(serializers.Serializer):
    inscription  = serializers.IntegerField()
    statut       = serializers.ChoiceField(
        choices=AbsenceSeance.STATUT_CHOICES, default=AbsenceSeance.STATUT_ABSENT,
    )
    justificatif = serializers.CharField(required=False, allow_blank=True, default='')


class SaisieAbsencesSerializer(serializers.Serializer):
    """
    Feuille d'appel « par exception » : on n'envoie que les non-présents.
    Une liste vide signifie donc « classe au complet ».
    """
    absents = LigneSaisieAbsenceSerializer(many=True)
