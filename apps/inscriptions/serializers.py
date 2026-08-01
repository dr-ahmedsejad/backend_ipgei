from rest_framework import serializers

from apps.parametres.models import Year
from .models import (
    Preinscription,
    InscriptionAdministrative,
    InscriptionPedagogique,
    InscriptionElement,
    Derogation,
    GrilleFrais,
    CandidatBac,
)


# ── Préinscription ───────────────────────────────────────────────────────────────

class PreinscriptionSerializer(serializers.ModelSerializer):
    """Serializer complet (lecture + écriture admin)."""
    token       = serializers.UUIDField(source='numero_dossier', read_only=True)
    filiere_nom = serializers.CharField(source='filiere.intitule_fr', read_only=True, default=None)

    class Meta:
        model  = Preinscription
        fields = '__all__'
        read_only_fields = ('numero_dossier', 'date_soumission', 'examinee_par', 'date_examen')


class PreinscriptionPublicSerializer(serializers.ModelSerializer):
    """Serializer lecture seule pour suivi public par numero_dossier."""
    token = serializers.UUIDField(source='numero_dossier', read_only=True)

    class Meta:
        model  = Preinscription
        fields = (
            'token', 'numero_dossier', 'nom_fr', 'prenom_fr',
            'filiere', 'annee_univ', 'statut', 'motif_rejet', 'date_soumission',
        )


class PreinscriptionCreateSerializer(serializers.ModelSerializer):
    """
    Serializer pour soumission publique via le wizard frontend.
    Accepte les noms de champs du frontend et retourne `token` (= numero_dossier).
    """
    token             = serializers.UUIDField(source='numero_dossier', read_only=True)
    filiere_souhaitee = serializers.IntegerField(write_only=True, required=True)

    class Meta:
        model  = Preinscription
        fields = (
            'token',
            'nom_fr', 'nom_ar', 'prenom_fr', 'prenom_ar',
            'date_naissance', 'email', 'telephone',
            'filiere_souhaitee',
            'serie_bac', 'annee_bac', 'mention_bac',
            'motif',
            'piece_identite', 'releve_notes', 'photo',
        )
        extra_kwargs = {
            'nom_fr':         {'required': True},
            'prenom_fr':      {'required': True},
            'piece_identite': {'required': True},
        }

    def create(self, validated_data):
        from apps.scolarite.models import Filiere
        from apps.parametres.models import Institution

        filiere_id = validated_data.pop('filiere_souhaitee')
        try:
            filiere = Filiere.objects.get(pk=filiere_id)
        except Filiere.DoesNotExist:
            raise serializers.ValidationError({'filiere_souhaitee': 'Filière introuvable.'})

        annee = Year.objects.order_by('-annee').first()
        validated_data.setdefault('annee_univ', annee)
        # Institution dérivée de la filière sinon institution principale
        institution = filiere.institution or Institution.objects.filter(est_principale=True).first()
        validated_data.setdefault('institution', institution)
        return Preinscription.objects.create(filiere=filiere, **validated_data)


# ── Inscription administrative ────────────────────────────────────────────────────

class InscriptionAdministrativeSerializer(serializers.ModelSerializer):
    etudiant_matricule  = serializers.CharField(source='etudiant.matricule', read_only=True)
    etudiant_nom        = serializers.CharField(source='etudiant.nom', read_only=True)
    filiere_nom         = serializers.CharField(source='filiere.intitule_fr', read_only=True)
    # Type de diplôme de la filière (LP/M/ING/Doctorat) — sert au front à afficher
    # le niveau préfixé (L1, E1, M1, D1) plutôt qu'un simple « 1 ».
    filiere_type_diplome = serializers.CharField(source='filiere.type_diplome', read_only=True)
    annee_universitaire = serializers.StringRelatedField(source='annee_univ', read_only=True)
    # Montant dû calculé depuis la grille tarifaire — lecture seule, jamais saisi.
    # None si aucun tarif défini (le front affiche alors un message d'invite).
    montant_du          = serializers.SerializerMethodField()

    def get_montant_du(self, obj):
        from .models import GrilleFrais
        montant = GrilleFrais.montant_pour(obj)
        return str(montant) if montant is not None else None

    # Le frontend peut envoyer l'ID (entier) ou la chaîne de l'année.
    # On expose les deux : en écriture via PK, en lecture via StringRelatedField.
    class Meta:
        model  = InscriptionAdministrative
        fields = [
            'id',
            'etudiant', 'etudiant_matricule', 'etudiant_nom',
            'filiere', 'filiere_nom', 'filiere_type_diplome',
            'annee_univ', 'annee_universitaire',
            'institution',
            'niveau', 'numero_inscription', 'statut',
            'est_payee', 'montant_frais', 'montant_du', 'date_paiement',
            'recu_paiement', 'date_inscription', 'validee_par',
        ]
        read_only_fields = [
            'numero_inscription', 'validee_par', 'date_inscription',
            # Statut de paiement : posé UNIQUEMENT par l'action @payer (views.py),
            # jamais via un PATCH/POST direct → empêche de marquer payé sans payer.
            'est_payee', 'recu_paiement', 'date_paiement',
        ]
        extra_kwargs = {'institution': {'required': False}}

    def create(self, validated_data):
        # Institution dérivée auto si absente : filiere.institution sinon principale
        if not validated_data.get('institution'):
            from apps.parametres.models import Institution
            filiere = validated_data.get('filiere')
            validated_data['institution'] = (
                (filiere.institution if filiere else None)
                or Institution.objects.filter(est_principale=True).first()
            )
        return super().create(validated_data)


# ── Inscription pédagogique ───────────────────────────────────────────────────────

class InscriptionPedagogiqueSerializer(serializers.ModelSerializer):
    semestre_code      = serializers.CharField(source='semestre.code_semestre', read_only=True)
    semestre_label     = serializers.CharField(source='semestre.semestre', read_only=True)
    annee_univ_label   = serializers.CharField(source='inscription_admin.annee_univ.annee', read_only=True)
    etudiant_nom       = serializers.CharField(source='inscription_admin.etudiant.nom', read_only=True)
    etudiant_matricule = serializers.CharField(source='inscription_admin.etudiant.matricule', read_only=True)
    # Compteur d'éléments inscrits (affiché dans la liste). La liste affichait 0
    # partout car le sérialiseur ne renvoyait aucun champ d'éléments. Utilise
    # l'annotation `nb_elements` du viewset (Count, pas de N+1) ; repli .count().
    nb_elements        = serializers.SerializerMethodField()

    class Meta:
        model  = InscriptionPedagogique
        fields = [
            'id',
            'inscription_admin', 'etudiant_nom', 'etudiant_matricule',
            'semestre', 'semestre_code', 'semestre_label', 'annee_univ_label',
            'est_redoublant', 'est_dette', 'validee_par', 'date_inscription',
            'nb_elements',
        ]
        read_only_fields = ['validee_par', 'date_inscription']

    def get_nb_elements(self, obj) -> int:
        n = getattr(obj, 'nb_elements', None)
        if n is not None:
            return n
        return obj.inscriptions_elements.count()


# ── Inscription élément (dettes) ──────────────────────────────────────────────────

class InscriptionElementSerializer(serializers.ModelSerializer):
    em_code     = serializers.CharField(source='em.code_em',  read_only=True, default=None)
    em_intitule = serializers.CharField(source='em.intitule', read_only=True, default=None)
    # Code / intitulé unifiés attendus par le front. Une InscriptionElement peut
    # être liée via `element` (modules.ElementModule, LMD) OU via `em` (em.EM,
    # planification). Sans ces champs, les colonnes Code/Intitulé restaient vides
    # quand l'inscription était liée par `em` (element=NULL).
    element_code = serializers.SerializerMethodField()
    element_nom  = serializers.SerializerMethodField()

    class Meta:
        model  = InscriptionElement
        fields = [
            'id',
            'inscription_ped',
            'element', 'element_code', 'element_nom',
            'em', 'em_code', 'em_intitule',
            'est_dette', 'annee_dette',
        ]

    def get_element_code(self, obj):
        if obj.element_id and obj.element:
            return obj.element.code
        if obj.em_id and obj.em:
            return obj.em.code_em
        return None

    def get_element_nom(self, obj):
        if obj.element_id and obj.element:
            return obj.element.intitule_fr
        if obj.em_id and obj.em:
            return obj.em.intitule
        return None


# ── Dérogations ────────────────────────────────────────────────────────────────

class DerogationSerializer(serializers.ModelSerializer):
    etudiant_matricule = serializers.CharField(source='etudiant.matricule',  read_only=True)
    etudiant_nom       = serializers.CharField(source='etudiant.nom_fr',     read_only=True)
    annee_label        = serializers.CharField(source='annee_univ.annee',    read_only=True)
    type_label         = serializers.CharField(source='get_type_derogation_display', read_only=True)
    decide_par_nom     = serializers.CharField(source='decide_par.get_full_name',    read_only=True, default='')
    statut_label       = serializers.CharField(source='get_statut_display', read_only=True)

    class Meta:
        model  = Derogation
        fields = [
            'id',
            'etudiant', 'etudiant_matricule', 'etudiant_nom',
            'annee_univ', 'annee_label',
            'institution',
            'type_derogation', 'type_label',
            'motif',
            'justificatif',
            'date_decision',
            'decide_par', 'decide_par_nom',
            'statut', 'statut_label',
            'date_creation', 'date_modification',
        ]
        read_only_fields = ['decide_par', 'date_creation', 'date_modification']
        extra_kwargs = {'institution': {'required': False}}

    def create(self, validated_data):
        if not validated_data.get('institution'):
            from apps.parametres.models import Institution
            etu = validated_data.get('etudiant')
            inst = None
            if etu and etu.departement_id:
                inst = etu.departement.institution
            validated_data['institution'] = inst or Institution.objects.filter(est_principale=True).first()
        return super().create(validated_data)

    def validate(self, attrs):
        # Année blanche → justificatif obligatoire
        type_d = attrs.get('type_derogation') or getattr(self.instance, 'type_derogation', None)
        if type_d == 'annee_blanche':
            justif = attrs.get('justificatif') or (self.instance and self.instance.justificatif)
            if not justif:
                raise serializers.ValidationError({
                    'justificatif': "Un justificatif est obligatoire pour une année blanche (Art. 23 / Art. 29).",
                })
        return attrs


# ── Grille tarifaire des frais d'inscription ─────────────────────────────────────

class _InstitutionPrincipaleDefault:
    """
    Défaut serveur pour l'institution : l'institution principale (mono-institution,
    phase 1 — cf. InstitutionScopedMixin). Fournir une valeur ici satisfait le
    UniqueTogetherValidator sans que le client envoie le champ.
    """
    requires_context = False

    def __call__(self):
        from apps.parametres.models import Institution
        return Institution.objects.filter(est_principale=True).first()


class GrilleFraisSerializer(serializers.ModelSerializer):
    annee_univ_label   = serializers.CharField(source='annee_univ.annee', read_only=True)
    type_diplome_label = serializers.CharField(source='get_type_diplome_display', read_only=True)
    # Résolue côté serveur (institution principale), jamais saisie par le client.
    # HiddenField → toujours présente dans validated_data (satisfait unique_together).
    institution        = serializers.HiddenField(default=_InstitutionPrincipaleDefault())

    class Meta:
        model  = GrilleFrais
        fields = [
            'id', 'institution',
            'annee_univ', 'annee_univ_label',
            'type_diplome', 'type_diplome_label',
            'niveau', 'montant', 'actif',
            'date_creation', 'date_modification',
        ]
        read_only_fields = ['date_creation', 'date_modification']


# ── Candidat BAC (vivier des bacheliers) ─────────────────────────────────────────

class CandidatBacSerializer(serializers.ModelSerializer):
    """Lecture seule : les candidats sont créés uniquement via l'import Excel."""
    annee_univ_label = serializers.CharField(source='annee_univ.annee', read_only=True)

    class Meta:
        model  = CandidatBac
        fields = [
            'id', 'annee_univ', 'annee_univ_label', 'institution',
            'nni', 'num_bac', 'nom_fr', 'nom_ar', 'date_naissance',
            'lieu_naissance', 'sexe', 'serie', 'moyenne', 'mention', 'wilaya',
            'inscrit', 'etudiant', 'date_import',
        ]
        read_only_fields = fields
