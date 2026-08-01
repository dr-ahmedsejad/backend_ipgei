from rest_framework import serializers
from .models import DocumentOfficiel, RegistreDiplome


class DocumentOfficielSerializer(serializers.ModelSerializer):
    etudiant_nom        = serializers.CharField(source='etudiant.nom', read_only=True)
    etudiant_matricule  = serializers.CharField(source='etudiant.matricule', read_only=True)
    genere_par_nom      = serializers.SerializerMethodField()
    semestre_code       = serializers.SerializerMethodField()

    def get_genere_par_nom(self, obj):
        return str(obj.genere_par) if obj.genere_par else None

    def get_semestre_code(self, obj):
        return obj.semestre.code_semestre if obj.semestre else None

    class Meta:
        model  = DocumentOfficiel
        fields = '__all__'


class DocumentVerificationSerializer(serializers.Serializer):
    """Sérialiseur PUBLIC pour /verifier (AllowAny).

    Expose de quoi COMPARER le document au registre — photo + identité + NNI +
    données PROPRES au type :
      • attestation d'inscription → niveau
      • relevé de notes (semestre) → semestre / moyenne / crédits / décision
      • diplôme → mention + N° de diplôme
    Aucune donnée technique (ni hash, ni chemin de fichier, ni auteur, ni token) :
    c'est la comparaison visuelle (photo + nom + infos) qui défait la fraude
    papier (faux fabriqué, QR copié sur un autre papier)."""
    numero_serie        = serializers.CharField(read_only=True)
    type_document       = serializers.CharField(read_only=True)
    type_libelle        = serializers.CharField(source='get_type_document_display', read_only=True)
    est_valide          = serializers.BooleanField(read_only=True)
    date_generation     = serializers.DateTimeField(read_only=True)
    annee_universitaire = serializers.SerializerMethodField()
    etudiant_nom        = serializers.SerializerMethodField()
    etudiant_matricule  = serializers.CharField(source='etudiant.matricule', read_only=True)
    photo_url           = serializers.SerializerMethodField()
    nni                 = serializers.SerializerMethodField()
    filiere             = serializers.SerializerMethodField()
    # Attestation d'inscription
    niveau              = serializers.SerializerMethodField()
    # Relevé de notes (semestre)
    semestre            = serializers.SerializerMethodField()
    moyenne_semestre    = serializers.SerializerMethodField()
    credits_obtenus     = serializers.SerializerMethodField()
    credits_total       = serializers.SerializerMethodField()
    decision            = serializers.SerializerMethodField()
    # Diplôme
    numero_diplome      = serializers.SerializerMethodField()
    mention             = serializers.SerializerMethodField()

    # ── Helpers ──────────────────────────────────────────────────
    @staticmethod
    def _is_diplome(obj):
        return obj.type_document in ('diplome', 'attestation_diplome')

    def _registre(self, obj):
        """RegistreDiplome correspondant (diplôme). ROBUSTE à une année document
        erronée : filtre par année si elle matche, sinon retombe sur le dernier
        diplôme de l'étudiant → l'année d'obtention affichée vient du registre."""
        if not self._is_diplome(obj):
            return None
        if not hasattr(self, '_reg_cache'):
            from .models import RegistreDiplome
            base = (RegistreDiplome.objects
                    .filter(etudiant=obj.etudiant).select_related('filiere'))
            reg = None
            if obj.annee_universitaire:
                reg = base.filter(annee_universitaire=obj.annee_universitaire).first()
            self._reg_cache = reg or base.order_by('-date_delivrance').first()
        return self._reg_cache

    def _inscription(self, obj):
        """InscriptionAdministrative de l'ANNEE du document (memoized). Donne la
        filiere / le niveau PROPRES a l'annee (tronc commun en S1/S2 vs
        specialisation en L3) — MEME source que le releve/attestation PDF."""
        if not hasattr(self, '_insc_cache'):
            from apps.inscriptions.models import InscriptionAdministrative
            qs = InscriptionAdministrative.objects.filter(etudiant=obj.etudiant)
            if obj.annee_universitaire:
                qs = qs.filter(annee_univ__annee=obj.annee_universitaire)
            self._insc_cache = (qs.select_related('filiere', 'annee_univ')
                                .order_by('-annee_univ__annee').first())
        return self._insc_cache

    def _releve(self, obj):
        """Résultat CONSOLIDÉ du semestre du relevé (mémoïsé, moteur du relevé).
        None si le doc n'est pas un relevé-semestre, {} si le calcul échoue."""
        if obj.type_document != 'releve_semestre' or not obj.semestre:
            return None
        if not hasattr(self, '_releve_cache'):
            try:
                from .services import calculer_resultat_semestre_consolide
                from apps.parametres.models import Year
                # obj.annee_universitaire est une CHAÎNE ('2025-2026') ; le moteur
                # consolidé attend l'OBJET Year (comme le relevé PDF). Sans cette
                # résolution, l'appel lève et moyenne/crédits/décision restent nuls.
                annee_obj = Year.objects.filter(annee=obj.annee_universitaire).first()
                self._releve_cache = calculer_resultat_semestre_consolide(
                    obj.etudiant, obj.semestre, annee_obj) or {}
            except Exception:
                self._releve_cache = {}
        return self._releve_cache

    # ── Identité (tous documents) ────────────────────────────────
    def get_etudiant_nom(self, obj):
        e = obj.etudiant
        prenom = (getattr(e, 'prenom_fr', '') or '').strip()
        nom    = (getattr(e, 'nom_fr', '') or e.nom or '').strip()
        return (f'{prenom} {nom}').strip() or e.nom

    def get_photo_url(self, obj):
        # Sert la photo via l'endpoint PUBLIC token-gated /verifier/{token}/photo/
        # (AllowAny) et NON le chemin /media/ protege par auth_request : la page de
        # verification n'est pas connectee, /media/ renverrait 401. Le token (UUID
        # aleatoire) empeche l'enumeration des photos.
        photo = getattr(obj.etudiant, 'photo', None)
        if not photo:
            return None
        path = f'/api/v1/documents/officiels/verifier/{obj.token_verification}/photo/'
        request = self.context.get('request')
        return request.build_absolute_uri(path) if request else path

    def get_nni(self, obj):
        return getattr(obj.etudiant, 'cni', '') or None

    def get_annee_universitaire(self, obj):
        reg = self._registre(obj)   # diplôme → année d'obtention (registre = autorité)
        if reg and reg.annee_universitaire:
            return reg.annee_universitaire
        return obj.annee_universitaire or None

    def get_filiere(self, obj):
        # Diplome : filiere de FIN DE CYCLE (registre = autorite).
        reg = self._registre(obj)
        if reg and reg.filiere:
            return reg.filiere.intitule_fr
        # Autres docs : filiere DE L'ANNEE du document (inscription admin) → reflete
        # le tronc commun (S1/S2 = « Statistiques ») vs la specialisation (L3 = SDID),
        # exactement comme le releve/attestation PDF. Repli : filiere globale.
        insc = self._inscription(obj)
        f = getattr(insc, 'filiere', None) or getattr(obj.etudiant, 'filiere', None)
        return getattr(f, 'intitule_fr', None) if f else None

    # ── Attestation d'inscription ────────────────────────────────
    def get_niveau(self, obj):
        """MÊME source que l'attestation PDF (InscriptionAdministrative.niveau de
        l'annee + NIVEAU_LABELS) → l'écran affiche exactement le niveau du papier."""
        if obj.type_document != 'attestation_inscription':
            return None
        from .services import NIVEAU_LABELS
        niveau = getattr(self._inscription(obj), 'niveau', None)
        if not niveau:
            return None
        return NIVEAU_LABELS.get(niveau, f'Niveau {niveau}')

    # ── Relevé de notes (semestre) ───────────────────────────────
    def get_semestre(self, obj):
        if obj.type_document != 'releve_semestre' or not obj.semestre:
            return None
        from .services import SEMESTRE_FR_LABELS
        code = getattr(obj.semestre, 'code_semestre', '') or ''
        return SEMESTRE_FR_LABELS.get(code, code or None)

    def get_moyenne_semestre(self, obj):
        r = self._releve(obj)
        if not r:
            return None
        m = r.get('moyenne_semestre')
        return round(float(m), 2) if m is not None else None

    def get_credits_obtenus(self, obj):
        r = self._releve(obj)
        return None if r is None else r.get('credits_valides')

    def get_credits_total(self, obj):
        r = self._releve(obj)
        return None if r is None else r.get('credits_total')

    def get_decision(self, obj):
        r = self._releve(obj)
        if not r:
            return None
        return 'Semestre validé' if r.get('est_admis') else 'Semestre non validé'

    # ── Diplôme ──────────────────────────────────────────────────
    def get_numero_diplome(self, obj):
        reg = self._registre(obj)
        return (reg.numero_diplome or None) if reg else None

    def get_mention(self, obj):
        reg = self._registre(obj)
        return (reg.mention or None) if reg else None


class DiplomeVerificationSerializer(serializers.Serializer):
    """
    Vérification PUBLIQUE d'un DIPLÔME au scan du QR (AllowAny) — contenu SIMPLE
    et propre au diplôme (groupe, établissement, identité, libellé du diplôme,
    NNI, matricule, date d'obtention). Distinct de DocumentVerificationSerializer
    (photo + données), CONSERVÉ pour un branchement ultérieur.

    Le drapeau `is_diplome` permet au frontend de choisir la mise en page.
    """
    is_diplome     = serializers.SerializerMethodField()
    est_valide     = serializers.BooleanField(read_only=True)
    groupe         = serializers.SerializerMethodField()
    institution    = serializers.SerializerMethodField()
    type_libelle   = serializers.SerializerMethodField()
    nom_complet    = serializers.SerializerMethodField()
    diplome        = serializers.SerializerMethodField()
    nni            = serializers.SerializerMethodField()
    matricule      = serializers.CharField(source='etudiant.matricule', read_only=True)
    date_obtention = serializers.SerializerMethodField()

    def _registre(self, obj):
        from .models import RegistreDiplome
        qs = RegistreDiplome.objects.filter(etudiant=obj.etudiant)
        if obj.annee_universitaire:
            qs = qs.filter(annee_universitaire=obj.annee_universitaire)
        return (qs.select_related('filiere', 'filiere__filiere_parent')
                  .order_by('-date_delivrance').first())

    def get_is_diplome(self, obj):
        return True

    def get_groupe(self, obj):
        return getattr(obj.institution, 'groupe_fr', '') or ''

    def get_institution(self, obj):
        inst = obj.institution
        return (getattr(inst, 'nom_complet_fr', '') or getattr(inst, 'nom_fr', '')
                or getattr(inst, 'nom', '') or '')

    def get_type_libelle(self, obj):
        return 'Attestation de diplôme'

    def get_nom_complet(self, obj):
        e = obj.etudiant
        prenom = (getattr(e, 'prenom_fr', '') or '').strip()
        nom    = (getattr(e, 'nom_fr', '') or e.nom or '').strip()
        return f'{prenom} {nom}'.strip() or e.nom

    def get_diplome(self, obj):
        from .services import _libelle_diplome
        reg = self._registre(obj)
        fil = getattr(reg, 'filiere', None) or getattr(obj.etudiant, 'filiere', None)
        return _libelle_diplome(fil)

    def get_nni(self, obj):
        return getattr(obj.etudiant, 'cni', '') or ''

    def get_date_obtention(self, obj):
        reg = self._registre(obj)
        d = getattr(reg, 'date_delivrance', None)
        return d.strftime('%d/%m/%Y') if d else ''


class RegistreDiplomeSerializer(serializers.ModelSerializer):
    etudiant_nom        = serializers.CharField(source='etudiant.nom', read_only=True)
    etudiant_matricule  = serializers.CharField(source='etudiant.matricule', read_only=True)
    filiere_nom         = serializers.CharField(source='filiere.intitule_fr', read_only=True)

    class Meta:
        model  = RegistreDiplome
        fields = '__all__'
