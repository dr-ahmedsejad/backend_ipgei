"""Serializers de l'API IPGEI."""
from decimal import Decimal

from rest_framework import serializers
from django.db import models

from django.core.exceptions import ValidationError as DjangoValidationError

from .models import (
    AbsenceSeance, Classe, Deliberation, DetailLigneDeliberation, EmploiArchive,
    EpreuveClasse, EpreuveIPGEI, Evaluation,
    GrilleType, HistoriqueClasse, InscriptionIPGEI, LigneDeliberation, Matiere,
    MembreJuryIPGEI, NiveauIPGEI,
    Note, ParametresIPGEI, PermutationEtudiant, PermutationProf,
    PonderationClasse, SeanceReelle,
    SeanceType, SemaineIPGEI, SemestreIPGEI, SessionEvaluationIPGEI,
    SignataireIPGEI, SousGroupeTP,
    NIVEAU_CHOICES, PCT_DEFAUT_AVEC_TP, PCT_DEFAUT_SANS_TP,
)


class ChampNiveauPrepa(serializers.Field):
    """
    Expose le niveau d'une classe comme la chaîne « MPSI » ou « MP ».

    Depuis que `Classe` hérite de `departement.Departement`, le niveau est une
    clé étrangère vers `parametres.Niveau` et non plus une chaîne. Ce champ
    conserve le contrat d'API d'origine — le client continue d'envoyer et de
    recevoir un code — sans quoi tous les écrans IPGEI seraient à reprendre.

    Les codes acceptés viennent du référentiel `NiveauIPGEI`, non d'une liste
    figée : sans cela, un niveau ajouté par la scolarité — MPI — serait refusé
    à la création de sa première classe. Repli sur le cursus d'origine tant que
    le référentiel est vide.

    La ligne du socle est créée au besoin plutôt que la saisie rejetée : elle
    n'est qu'un miroir technique, pas une donnée à administrer deux fois.
    """

    def to_representation(self, valeur):
        return valeur.niveau if valeur is not None else ''

    def to_internal_value(self, donnee):
        from .socle import niveau_socle

        codes = list(
            NiveauIPGEI.objects.filter(actif=True).values_list('code', flat=True)
        ) or [code for code, _ in NIVEAU_CHOICES]
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
            'id', 'institution', 'seuil_validation', 'seuil_validation_semestre',
            'plafond_rattrapage', 'nb_semaines_defaut', 'droit_redoublement_max',
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
    jours_feries      = serializers.SerializerMethodField()

    def get_etat_coherence(self, obj) -> str:
        # Le lot prime quand il est disponible ; sinon calcul unitaire, pour
        # que le serializer reste correct hors liste.
        etats = self.context.get('etats_coherence')
        if etats:
            return etats.get(obj.pk)
        return obj.etat_coherence_pour(self.context.get('classe_coherence'))

    def get_jours_feries(self, obj) -> list:
        """
        Les jours FERMÉS de la semaine — ce que l'emploi du temps doit griser.

        Lu sur le calendrier du socle, qui fait foi : la semaine IPGEI reste
        « de cours » quand un seul de ses jours est férié. Deux requêtes par
        semaine ; une liste de semestre en compte une vingtaine, sans rien que
        l'écran ne sente.
        """
        from . import feries

        return [
            {'jour': jour_id, 'jour_libelle': feries.JOURS_FR[etat.date.weekday()],
             'date': etat.date.isoformat(), 'libelle': etat.libelle,
             'etat': etat.etat}
            for jour_id, etat in sorted(feries.etats_de_la_semaine(obj).items(),
                                        key=lambda e: e[1].date)
            if etat.ferme
        ]

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
            'jours_feries',
        ]


class SemestreIPGEISerializer(serializers.ModelSerializer):
    institution   = _champ_institution()
    # Le semestre relève d'une année d'étude, pas d'un niveau : `libelle_annee`
    # dit ce qu'il est, `niveaux` qui le suit. `niveau` reste exposé pour les
    # écrans qui ne l'ont pas encore quitté.
    rang          = serializers.IntegerField(read_only=True)
    libelle_annee = serializers.CharField(read_only=True)
    niveaux       = serializers.ListField(read_only=True, child=serializers.CharField())
    type_semestre = serializers.CharField(read_only=True)
    nb_semaines_generees = serializers.SerializerMethodField()
    # Ce que le calendrier CONSTATE, et non ce qu'on lui prête : la premiere et
    # la derniere semaine reprises. La derniere n'est pas une fin — on peut en
    # ajouter d'autres apres les seize, et c'est le cas quand un rattrapage
    # s'intercale. L'ecran l'affiche comme un constat, pas comme un terme.
    calendrier = serializers.SerializerMethodField()

    class Meta:
        model  = SemestreIPGEI
        fields = [
            'id', 'institution', 'code', 'annee_universitaire', 'date_debut', 'date_fin',
            'nb_semaines', 'est_cloture', 'rang', 'libelle_annee', 'niveaux',
            'type_semestre', 'nb_semaines_generees', 'calendrier',
        ]

    def get_nb_semaines_generees(self, obj) -> int:
        return obj.semaines.count()

    def get_calendrier(self, obj) -> dict:
        bornes = obj.semaines.aggregate(
            premiere=models.Min('date_debut'), derniere=models.Max('date_fin'))
        return {
            'premiere_semaine': bornes['premiere'],
            'derniere_semaine': bornes['derniere'],
        }

    def validate(self, attrs):
        debut = attrs.get('date_debut', getattr(self.instance, 'date_debut', None))
        fin   = attrs.get('date_fin',   getattr(self.instance, 'date_fin', None))
        if debut and fin and fin <= debut:
            raise serializers.ValidationError({'date_fin': 'La date de fin doit suivre la date de début.'})
        return attrs


# ── Matières ─────────────────────────────────────────────────────────────────
class PonderationClasseSerializer(serializers.ModelSerializer):
    """
    Une dérogation : ce qu'une classe fait autrement — pondération, coefficient.

    Le `motif` est obligatoire au modèle et le reste ici. Une dérogation sans
    raison écrite devient, deux ans plus tard, une anomalie que personne n'ose
    corriger — c'est ce qui nous a coûté une demi-journée sur la colle de MP A.
    """
    matiere_code     = serializers.CharField(source='matiere.code', read_only=True)
    matiere_intitule = serializers.CharField(source='matiere.intitule', read_only=True)
    classe_nom       = serializers.CharField(source='classe.nom', read_only=True)
    semestre_code    = serializers.CharField(source='matiere.code_semestre', read_only=True)
    #: Le coefficient GÉNÉRAL, pour que l'écran affiche « 14 → 12 » sans avoir à
    #: recharger la matière.
    coefficient_matiere = serializers.DecimalField(
        source='matiere.coefficient', max_digits=4, decimal_places=2, read_only=True)

    class Meta:
        model  = PonderationClasse
        fields = [
            'id', 'matiere', 'matiere_code', 'matiere_intitule',
            'classe', 'classe_nom', 'semestre_code',
            'pct_ds', 'pct_tp', 'pct_exam', 'has_tp',
            'coefficient', 'coefficient_matiere', 'motif', 'cree_le',
        ]
        read_only_fields = ['cree_le']

    def validate(self, attrs):
        # `full_clean` plutôt qu'une copie des règles : le modèle porte déjà la
        # somme à 100, la cohérence has_tp / pct_tp, le refus d'une dérogation
        # qui ne déroge pas, et le contrôle de niveau. Les redire ici les
        # ferait diverger.
        instance = self.instance or PonderationClasse()
        for champ, valeur in attrs.items():
            setattr(instance, champ, valeur)
        try:
            instance.full_clean(exclude=['cree_le'])
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.message_dict or e.messages)
        return attrs


class MatiereSerializer(serializers.ModelSerializer):
    institution   = _champ_institution()
    niveau = serializers.CharField(read_only=True)
    # Le niveau se saisit dès lors qu'il s'administre : deux niveaux d'un même
    # rang — MP et MPI — ont chacun leur maquette sur les mêmes semestres, et le
    # code du semestre ne suffit plus à trancher.
    niveau_ref      = serializers.PrimaryKeyRelatedField(
        queryset=NiveauIPGEI.objects.all(), required=False, allow_null=True,
    )
    niveau_ref_code = serializers.CharField(source='niveau_ref.code', read_only=True, default='')
    # Total calcule : c'est la somme CM+TD+TP, jamais une valeur saisie.
    volume_horaire = serializers.IntegerField(read_only=True)
    #: Enseignement partagé — voir `Matiere.partage_avec`. L'écran de saisie
    #: s'en sert pour griser les colonnes DS et TP d'une matière rattachée.
    partage_avec_code     = serializers.CharField(
        source='partage_avec.code', read_only=True, default='')
    partage_avec_intitule = serializers.CharField(
        source='partage_avec.intitule', read_only=True, default='')

    class Meta:
        model  = Matiere
        fields = [
            'id', 'institution', 'code', 'intitule', 'intitule_ar', 'code_semestre',
            'niveau', 'niveau_ref', 'niveau_ref_code',
            'coefficient', 'volume_cm', 'volume_td', 'volume_tp',
            'volume_horaire', 'has_tp', 'departement',
            'partage_avec', 'partage_avec_code', 'partage_avec_intitule',
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

        # ── Enseignement partagé ────────────────────────────────────────────
        # Deux lignes de maquette pour un seul cours : elles doivent porter sur
        # le même semestre et le même niveau, sinon « le même enseignement » ne
        # veut rien dire. Une seule profondeur : A → B, jamais A → B → C, sans
        # quoi il faudrait décider où le devoir est vraiment composé.
        porteuse = valeur('partage_avec')
        if porteuse is not None:
            if self.instance is not None and porteuse.pk == self.instance.pk:
                raise serializers.ValidationError({'partage_avec':
                    'Une matière ne peut pas partager son enseignement avec elle-même.'})
            if porteuse.partage_avec_id is not None:
                raise serializers.ValidationError({'partage_avec':
                    f'{porteuse.code} est déjà rattachée à une autre matière. '
                    'Rattachez-vous directement à la matière porteuse.'})
            if self.instance is not None and self.instance.matieres_rattachees.exists():
                raise serializers.ValidationError({'partage_avec':
                    "D'autres matières sont déjà rattachées à celle-ci : elle est "
                    'porteuse, elle ne peut pas être rattachée à son tour.'})
            if porteuse.code_semestre != valeur('code_semestre'):
                raise serializers.ValidationError({'partage_avec':
                    f'{porteuse.code} relève du semestre {porteuse.code_semestre} : '
                    'un même enseignement se donne sur un seul semestre.'})
            niveau_demande = valeur('niveau_ref')
            if niveau_demande is not None and porteuse.niveau_ref_id != niveau_demande.pk:
                raise serializers.ValidationError({'partage_avec':
                    f'{porteuse.code} ne relève pas du même niveau.'})

        # Le niveau et le semestre doivent s'accorder : une matière de MPI — 2e
        # année — ne peut pas relever de S1. À défaut de niveau saisi, on prend
        # celui que le semestre désigne, pour que rien ne change quand un seul
        # niveau existe par rang.
        code_semestre = valeur('code_semestre')
        niveau = valeur('niveau_ref')
        if niveau is None and self.instance is None and code_semestre:
            candidats = [
                n for n in NiveauIPGEI.objects.filter(actif=True)
                if code_semestre in n.codes_semestres
            ]
            if len(candidats) == 1:
                attrs['niveau_ref'] = candidats[0]
            elif len(candidats) > 1:
                raise serializers.ValidationError({'niveau_ref': (
                    f'Plusieurs niveaux suivent {code_semestre} '
                    f'({", ".join(n.code for n in candidats)}) : précisez lequel.'
                )})
        elif niveau is not None and code_semestre and code_semestre not in niveau.codes_semestres:
            raise serializers.ValidationError({'code_semestre': (
                f'{niveau.code} suit {" et ".join(niveau.codes_semestres)}, pas {code_semestre}.'
            )})
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
    #: Classe d'attente d'un niveau : elle reçoit les inscrits avant leur
    #: répartition et ne se planifie pas. Les écrans doivent pouvoir l'écarter
    #: de leurs listes sans deviner à l'absence de suffixe.
    est_conteneur = serializers.BooleanField(read_only=True)

    class Meta:
        model  = Classe
        fields = ['id', 'nom', 'niveau', 'libelle', 'est_conteneur',
                  'annee_universitaire']


# ── Inscriptions ─────────────────────────────────────────────────────────────
class NouvelEtudiantSerializer(serializers.Serializer):
    """Identité saisie lors d'une inscription d'un étudiant encore inconnu."""
    #: Laissé VIDE, le serveur l'attribue — le prochain libre de la promotion,
    #: choisi au moment de l'écriture. C'est la voie sûre : un numéro proposé à
    #: l'ouverture du formulaire peut être pris pendant la saisie, celui-ci non.
    matricule      = serializers.CharField(max_length=50, required=False,
                                           allow_blank=True, default='')
    # `nom` porte le nom COMPLET, prénoms inclus — c'est la forme du référentiel
    # officiel, celle du fichier MESRS, et celle que l'attestation imprime.
    # `prenom_fr` reste accepté pour les appels qui séparent encore les deux.
    nom            = serializers.CharField(max_length=200)
    prenom_fr      = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    nom_ar         = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    genre          = serializers.ChoiceField(choices=[('M', 'Masculin'), ('F', 'Féminin')], default='M')
    date_naissance = serializers.DateField(required=False, allow_null=True)
    lieu_naissance_fr = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    lieu_naissance_ar = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
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
        valeur = (valeur or '').strip()
        if not valeur:
            return valeur            # le serveur l'attribuera

        # On NOMME le titulaire : « déjà attribué » laisse la secrétaire deviner
        # si c'est la même personne — le cas courant, une réinscription saisie
        # par la mauvaise voie — ou une collision qui demande un autre numéro.
        occupant = Etudiant.objects.filter(matricule__iexact=valeur).first()
        if occupant is not None:
            raise serializers.ValidationError(
                f'Le matricule {valeur} est déjà attribué à '
                f'{occupant.nom_display}. Si c\'est la même personne, inscrivez-la '
                'par « Étudiant déjà enregistré » ; sinon, choisissez un autre '
                'numéro ou utilisez « Générer ».'
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
    #: Classe d'arrivée. Facultative : à la rentrée on ignore encore combien
    #: de classes ouvrir, et `niveau` suffit — l'inscrit rejoint alors la
    #: classe d'attente du niveau, d'où on l'affectera ensuite.
    classe            = serializers.IntegerField(required=False, allow_null=True)
    niveau            = serializers.CharField(required=False, allow_blank=True)
    annee_universitaire = serializers.CharField(required=False, allow_blank=True)
    sous_groupe       = serializers.IntegerField(required=False, allow_null=True)
    numero_ordre      = serializers.IntegerField(required=False, allow_null=True)
    #: Inscrit hors promotion. Il ne redouble pas — il n'est pas étudiant —,
    #: donc le contrôle de réinscription ne le concerne pas, et il ne paie rien.
    auditeur_libre    = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        if not attrs.get('classe') and not attrs.get('niveau'):
            raise serializers.ValidationError(
                'Indiquez une classe, ou un niveau pour inscrire en attente '
                "d'affectation."
            )
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
    #: Vrai tant que l'inscrit est dans la classe d'attente de son niveau :
    #: inscrit, doté de sa maquette et de ses frais, mais sans groupe de cours.
    en_attente_affectation = serializers.SerializerMethodField()
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
            'niveau', 'en_attente_affectation', 'statut', 'statut_display',
            'nb_redoublements', 'date_inscription', 'actif', 'auditeur_libre',
            'montant_frais', 'est_payee', 'date_paiement', 'recu_paiement',
        ]

    def get_en_attente_affectation(self, obj) -> bool:
        """L'inscrit est-il encore dans la classe d'attente de son niveau ?"""
        return bool(obj.classe_id) and obj.classe.est_conteneur

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
def _controler_session(semestre, *, rattrapage: bool):
    """
    Relaie le contrôle de fenêtre de saisie en erreur DRF.

    La règle vit dans `services` — l'admin Django et les commandes de gestion
    passent par elle aussi. Ici on ne fait que traduire l'exception.
    """
    from django.core.exceptions import ValidationError as ErreurDjango

    from . import services
    try:
        services.controler_saisie(semestre, rattrapage=rattrapage)
    except ErreurDjango as erreur:
        raise serializers.ValidationError(getattr(erreur, 'messages', None) or str(erreur))


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
        if note is not None:
            # Un DS ou un examen relève toujours de la session normale.
            _controler_session(note.semestre, rattrapage=False)
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
        if instance is not None:
            from django.core.exceptions import ValidationError as ErreurDjango

            from . import fraudes
            try:
                for champ, composante in (('note_tp', 'tp'), ('note_rattrapage', 'rattrapage')):
                    if champ in attrs:
                        fraudes.controler_saisie_sanctionnee(instance, composante, None,
                                                             attrs[champ])
            except ErreurDjango as erreur:
                raise serializers.ValidationError(erreur.messages)
        semestre = attrs.get('semestre', getattr(instance, 'semestre', None))
        matiere  = attrs.get('matiere',  getattr(instance, 'matiere', None))
        if semestre is not None:
            # Chaque campagne a sa fenêtre : le rattrapage se saisit après la
            # clôture des notes normales, pas en même temps. Créer une ligne
            # vide n'est pas une saisie et ne réclame aucune session ouverte —
            # c'est ce que fait la grille au premier affichage.
            if 'note_rattrapage' in attrs:
                _controler_session(semestre, rattrapage=True)
            if 'note_tp' in attrs:
                _controler_session(semestre, rattrapage=False)
            if not attrs.keys() & {'note_rattrapage', 'note_tp'} and semestre.est_cloture:
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


class NiveauIPGEISerializer(serializers.ModelSerializer):
    """Niveau du cursus. Le rang commande les semestres et le tarif appliqué."""
    institution      = _champ_institution()
    libelle_rang     = serializers.CharField(read_only=True)
    codes_semestres  = serializers.ListField(read_only=True)
    nb_classes       = serializers.SerializerMethodField()
    nb_matieres      = serializers.SerializerMethodField()

    class Meta:
        model  = NiveauIPGEI
        fields = [
            'id', 'institution', 'code', 'libelle', 'rang', 'libelle_rang',
            'codes_semestres', 'redoublement_autorise', 'decisions_possibles',
            'actif', 'ordre', 'nb_classes', 'nb_matieres',
        ]

    def get_nb_classes(self, obj) -> int:
        from apps.departement.models import Departement
        return Departement.objects.filter(
            classe_ipgei__isnull=False, niveau__niveau=obj.code,
        ).count()

    def get_nb_matieres(self, obj) -> int:
        return obj.matieres.count()

    def validate_code(self, valeur):
        valeur = (valeur or '').strip().upper()
        if not valeur:
            raise serializers.ValidationError('Le code est requis.')
        return valeur

    def validate_decisions_possibles(self, valeur):
        connues = {code for code, _ in LigneDeliberation.DECISION_CHOICES}
        inconnues = [d for d in (valeur or []) if d not in connues]
        if inconnues:
            raise serializers.ValidationError(
                f'Décision(s) inconnue(s) : {", ".join(inconnues)}.'
            )
        return valeur or []

    def validate(self, attrs):
        rang = attrs.get('rang', getattr(self.instance, 'rang', 1))
        redouble = attrs.get('redoublement_autorise',
                             getattr(self.instance, 'redoublement_autorise', False))
        # La règle du cursus prime sur la saisie : pas de redoublement en
        # première année, pas même par dérogation.
        if rang == 1 and redouble:
            raise serializers.ValidationError(
                {'redoublement_autorise': 'Le redoublement est interdit en première année.'}
            )
        return attrs


class SignataireIPGEISerializer(serializers.ModelSerializer):
    """
    Personne habilitée à signer relevés et attestations.

    Aucune image : la signature reste manuscrite. Ce qui est paramétré ici, ce
    sont le nom et la fonction IMPRIMÉS au-dessus du trait, pour qu'en l'absence
    du chef de scolarité un autre puisse signer sous son propre nom.
    """
    institution = _champ_institution()
    libelle     = serializers.SerializerMethodField()

    class Meta:
        model  = SignataireIPGEI
        fields = ['id', 'institution', 'nom_fr', 'nom_ar', 'titre_fr', 'titre_ar',
                  'par_defaut', 'actif', 'ordre', 'libelle']

    def get_libelle(self, obj) -> str:
        return f'{obj.nom_fr} — {obj.titre_fr}'

    def validate_nom_fr(self, valeur):
        valeur = (valeur or '').strip()
        if not valeur:
            raise serializers.ValidationError('Le nom est requis.')
        return valeur

    def validate_titre_fr(self, valeur):
        valeur = (valeur or '').strip()
        if not valeur:
            raise serializers.ValidationError('La fonction est requise.')
        return valeur


class SaisieAnonymeSerializer(serializers.Serializer):
    """Une note saisie sous numéro d'anonymat, pour une épreuve d'une matière."""
    semestre        = serializers.IntegerField()
    matiere         = serializers.IntegerField()
    numero_anonymat = serializers.IntegerField(min_value=1)
    type_evaluation = serializers.ChoiceField(choices=Evaluation.TYPE_CHOICES)
    numero          = serializers.IntegerField(min_value=1, max_value=20)
    valeur          = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('20'),
    )


class LigneAnonymeSerializer(serializers.Serializer):
    """Une copie de la feuille de correction. `valeur` nulle efface l'épreuve."""
    numero_anonymat = serializers.IntegerField(min_value=1)
    valeur          = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal('0'),
        max_value=Decimal('20'), allow_null=True, required=False,
    )


class SaisieAnonymeLotSerializer(serializers.Serializer):
    """
    Une feuille de correction entière : une épreuve d'une matière, toutes ses
    copies. Le type et le numéro d'épreuve sont communs à la feuille — un
    correcteur note un paquet de copies de la même épreuve, jamais un mélange.
    """
    semestre        = serializers.IntegerField()
    matiere         = serializers.IntegerField()
    type_evaluation = serializers.ChoiceField(choices=Evaluation.TYPE_CHOICES)
    numero          = serializers.IntegerField(min_value=1, max_value=20)
    lignes          = LigneAnonymeSerializer(many=True, allow_empty=False)

    def validate_lignes(self, valeur):
        numeros = [ligne['numero_anonymat'] for ligne in valeur]
        if len(numeros) != len(set(numeros)):
            raise serializers.ValidationError(
                'Un même numéro de copie figure deux fois : la feuille serait '
                'enregistrée deux fois de suite, et la seconde note écraserait '
                'la première sans que rien ne le signale.'
            )
        return valeur


class SessionEvaluationIPGEISerializer(serializers.ModelSerializer):
    """
    Fenêtre de saisie. Les états ne se modifient pas au PATCH mais par les
    actions `ouvrir` / `cloturer` / `rouvrir` : elles seules posent la date, le
    responsable et la trace d'audit.
    """
    semestre_code       = serializers.CharField(source='semestre.code', read_only=True)
    semestre_annee      = serializers.CharField(source='semestre.annee_universitaire', read_only=True)
    semestre_cloture    = serializers.BooleanField(source='semestre.est_cloture', read_only=True)
    type_session_display = serializers.CharField(source='get_type_session_display', read_only=True)
    etat                = serializers.CharField(read_only=True)
    est_saisissable     = serializers.BooleanField(read_only=True)
    cloturee_par_nom    = serializers.CharField(
        source='cloturee_par.username', read_only=True, default='',
    )

    class Meta:
        model  = SessionEvaluationIPGEI
        fields = [
            'id', 'semestre', 'semestre_code', 'semestre_annee', 'semestre_cloture',
            'type_session', 'type_session_display', 'etat', 'est_saisissable',
            'est_ouverte', 'est_close', 'date_debut_saisie', 'date_fin_saisie',
            'plafond_rattrapage', 'ouverte_le', 'cloturee_le',
            'cloturee_par', 'cloturee_par_nom',
        ]
        read_only_fields = [
            'semestre', 'type_session', 'est_ouverte', 'est_close',
            'plafond_rattrapage', 'ouverte_le', 'cloturee_le', 'cloturee_par',
        ]


class SaisieCollectiveSerializer(serializers.Serializer):
    """Saisie d'une matière pour toute une classe, en une requête."""
    classe   = serializers.IntegerField()
    matiere  = serializers.IntegerField()
    semestre = serializers.IntegerField()
    lignes   = SaisieNoteSerializer(many=True)


# ── Épreuves ─────────────────────────────────────────────────────────────────
class EpreuveIPGEISerializer(serializers.ModelSerializer):
    """
    Une épreuve et les classes qu'elle convoque.

    `classes` s'écrit comme une simple liste d'identifiants, bien que la
    liaison passe par une table explicite : le client n'a pas à connaître
    `EpreuveClasse`, dont le seul champ propre — la salle — se remplit sur les
    fiches de répartition et non ici.
    """
    matiere_code     = serializers.CharField(source='matiere.code', read_only=True)
    matiere_intitule = serializers.CharField(source='matiere.intitule', read_only=True)
    type_display     = serializers.CharField(source='get_type_epreuve_display', read_only=True)
    semestre_code    = serializers.CharField(source='session.semestre.code', read_only=True)
    semestre_annee   = serializers.CharField(
        source='session.semestre.annee_universitaire', read_only=True)
    session_type     = serializers.CharField(source='session.type_session', read_only=True)
    heure_fin        = serializers.TimeField(read_only=True)
    duree_libelle    = serializers.CharField(read_only=True)
    classes_noms     = serializers.SerializerMethodField()

    classes = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Classe.objects.filter(actif=True),
    )

    class Meta:
        model  = EpreuveIPGEI
        fields = [
            'id', 'session', 'session_type', 'semestre_code', 'semestre_annee',
            'matiere', 'matiere_code', 'matiere_intitule',
            'type_epreuve', 'type_display', 'numero',
            'date', 'heure_debut', 'heure_fin', 'duree_minutes', 'duree_libelle',
            'classes', 'classes_noms', 'observations', 'cree_le', 'cree_par',
        ]
        read_only_fields = ['cree_le', 'cree_par']

    def get_classes_noms(self, obj) -> list:
        return [c.nom for c in obj.classes.all()]

    def validate_classes(self, valeur):
        if not valeur:
            raise serializers.ValidationError(
                'Une épreuve convoque au moins une classe.')
        return valeur

    def validate(self, attrs):
        """
        Deux épreuves ne peuvent pas se chevaucher pour une même classe.

        La contrainte d'unicité en base protège du doublon « DS 1 de maths deux
        fois » ; elle ne dit rien de deux épreuves DIFFÉRENTES posées à la même
        heure sur la même classe — maths et physique le mardi à 8h. C'est
        pourtant la faute la plus facile à commettre, et la seule que personne
        ne rattrape après coup.
        """
        instance = self.instance
        date    = attrs.get('date',          getattr(instance, 'date', None))
        debut   = attrs.get('heure_debut',   getattr(instance, 'heure_debut', None))
        duree   = attrs.get('duree_minutes', getattr(instance, 'duree_minutes', None))
        classes = attrs.get('classes')
        if classes is None and instance is not None:
            classes = list(instance.classes.all())
        if not (date and debut and duree and classes):
            return attrs

        d1 = debut.hour * 60 + debut.minute
        f1 = d1 + duree
        ids = {c.pk for c in classes}

        concurrentes = (
            EpreuveIPGEI.objects
            .filter(date=date, convocations__classe_id__in=ids)
            .exclude(pk=getattr(instance, 'pk', None))
            .prefetch_related('convocations__classe')
            .distinct()
        )
        for autre in concurrentes:
            if d1 < autre.fin_minutes and autre.debut_minutes < f1:
                noms = sorted(
                    c.classe.nom for c in autre.convocations.all() if c.classe_id in ids
                )
                raise serializers.ValidationError({
                    'heure_debut': (
                        f'Chevauchement avec {autre.type_epreuve} '
                        f'{autre.matiere.code} ({autre.heure_debut:%H:%M}–'
                        f'{autre.heure_fin:%H:%M}) pour : {", ".join(noms)}.'
                    )
                })

        session = attrs.get('session', getattr(instance, 'session', None))
        self._verifier_creneaux_libres(session, date, d1, f1, classes)
        return attrs

    @staticmethod
    def _verifier_creneaux_libres(session, date, debut, fin, classes):
        """
        Les créneaux que l'épreuve recouvre doivent être LIBRES de cours.

        On ne peut pas convoquer une classe à un examen pendant qu'elle a cours.
        La grille, elle, ne saurait pas refuser : une épreuve n'y entre pas — sa
        durée ne tombe pas sur les créneaux de 1h30 —, et rien ne l'empêcherait
        donc de recouvrir une séance existante. Le contrôle appartient à ce
        point d'entrée, le seul par lequel une épreuve se déclare.

        Le recouvrement PARTIEL suffit à refuser : un DS de 8h00 à 10h00 ne mord
        que quinze minutes sur le créneau de 09h45-11h15, mais la classe y
        compose — le cours ne peut pas s'y tenir.

        Les séances ANNULÉES ne bloquent rien : elles n'ont pas lieu.
        """
        from apps.parametres.models import Creneau, Jour

        from .models import JOURS_SEMAINE, SeanceReelle, SemaineIPGEI

        # La semaine DU SEMESTRE de l'épreuve, et non la première qui couvre la
        # date : S1 et S3 sont tous deux impairs, partagent leurs numéros ET
        # leurs dates. Chercher par la date seule tombait une fois sur deux sur
        # le mauvais semestre — et n'y trouvait aucune séance, donc aucun
        # conflit. Le contrôle passait alors sans rien vérifier.
        if session is None:
            return
        semaine = (SemaineIPGEI.objects
                   .filter(semestre=session.semestre,
                           date_debut__lte=date, date_fin__gte=date)
                   .first())
        if semaine is None:
            # Hors calendrier de ce semestre : aucune séance ce jour-là.
            return

        jour = Jour.objects.filter(
            jour__iexact=JOURS_SEMAINE[date.weekday()]).first()
        if jour is None:
            return

        # Les créneaux que l'intervalle recouvre, lus dans leur libellé.
        from .models import _bornes_creneau
        couverts = []
        for creneau in Creneau.objects.filter(is_actif=True).order_by('ordre', 'id'):
            bornes = _bornes_creneau(creneau.creneau or '')
            if bornes and debut < bornes[1] and bornes[0] < fin:
                couverts.append(creneau)
        if not couverts:
            return

        occupees = (
            SeanceReelle.objects
            .filter(semaine=semaine, jour=jour, annulee=False,
                    creneau__in=couverts, classe__in=classes)
            .select_related('classe', 'creneau', 'matiere', 'type_seance')
            .order_by('classe__nom', 'creneau__ordre')
        )
        if not occupees:
            return

        conflits = []
        for s in occupees:
            quoi = (s.matiere.code if s.matiere_id
                    else (s.type_seance.type_seance if s.type_seance_id else 'séance'))
            conflits.append(f'{s.classe.nom} — {s.creneau.creneau} ({quoi})')

        raise serializers.ValidationError({
            'heure_debut': (
                'Créneau déjà occupé par un cours : '
                + ' ; '.join(conflits[:6])
                + ('…' if len(conflits) > 6 else '')
                + ". Déplacez l'épreuve ou libérez ces créneaux dans "
                  '« Emploi du temps ».'
            )
        })

    def create(self, validated_data):
        classes = validated_data.pop('classes', [])
        epreuve = EpreuveIPGEI.objects.create(**validated_data)
        self._poser_classes(epreuve, classes)
        return epreuve

    def update(self, instance, validated_data):
        classes = validated_data.pop('classes', None)
        for champ, valeur in validated_data.items():
            setattr(instance, champ, valeur)
        instance.save()
        if classes is not None:
            self._poser_classes(instance, classes)
        return instance

    @staticmethod
    def _poser_classes(epreuve, classes):
        """
        Aligne les convocations sur la liste reçue.

        On ne recrée pas tout : les lignes conservées gardent leur salle, que la
        répartition sur fiches aura pu renseigner. Un `delete()` suivi d'un
        `bulk_create` l'aurait effacée à chaque modification d'horaire.
        """
        voulues = {c.pk for c in classes}
        actuelles = {c.classe_id for c in epreuve.convocations.all()}
        epreuve.convocations.exclude(classe_id__in=voulues).delete()
        EpreuveClasse.objects.bulk_create([
            EpreuveClasse(epreuve=epreuve, classe_id=pk)
            for pk in voulues - actuelles
        ])


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
    #: CE QU'IL EST DEVENU depuis. La décision d'un jury est datée et ne bouge
    #: plus — c'est ce qui protège son PV —, si bien qu'un jury annuel affiche
    #: encore « autorisé au rattrapage » pour un étudiant devenu redoublant
    #: depuis. Sans ce rappel, l'écran paraît en retard sur la réalité.
    statut_actuel         = serializers.CharField(
        source='inscription.statut', read_only=True)
    statut_actuel_display = serializers.CharField(
        source='inscription.get_statut_display', read_only=True)
    #: D'OÙ vient la situation actuelle quand elle diffère de la décision. Deux
    #: étudiants finissent redoublants par des chemins opposés — l'un recalé au
    #: rattrapage, l'autre au concours —, et l'écran ne disait ni l'un ni
    #: l'autre : il affichait « depuis », un mot suspendu sans complément.
    origine_situation = serializers.SerializerMethodField()
    details            = DetailLigneDeliberationSerializer(many=True, read_only=True)
    motif_sans_proposition = serializers.SerializerMethodField()
    # Les deux classements d'un jury de SEMESTRE — dans la classe, et sur la
    # promotion toutes classes du niveau confondues. Nuls sur un jury annuel,
    # qui prononce des décisions plutôt qu'un classement. Ils sont posés par
    # la vue, qui seule connaît le périmètre à comparer.
    rang_classe    = serializers.SerializerMethodField()
    rang_promotion = serializers.SerializerMethodField()
    effectif_classe    = serializers.SerializerMethodField()
    effectif_promotion = serializers.SerializerMethodField()

    class Meta:
        model  = LigneDeliberation
        fields = [
            'id', 'deliberation', 'inscription', 'etudiant_nom', 'etudiant_matricule',
            'classe_nom', 'moyenne_generale', 'total_coefficients', 'rang',
            'rang_classe', 'rang_promotion', 'effectif_classe', 'effectif_promotion',
            'decision_auto', 'decision_auto_display', 'decision', 'decision_display',
            'est_ajustee', 'motif_ajustement', 'observations', 'nb_redoublements',
            'statut_actuel', 'statut_actuel_display', 'origine_situation',
            'motif_sans_proposition', 'details',
        ]
        read_only_fields = [
            'deliberation', 'inscription', 'moyenne_generale', 'total_coefficients',
            'rang', 'decision_auto',
        ]

    def get_origine_situation(self, obj) -> str:
        """
        Ce qui a fait bouger la situation depuis ce jury : `concours`,
        `rattrapage`, ou rien.

        Deux signaux suffisent, et ils sont déjà chargés — aucune requête de
        plus sur une liste de vingt-cinq lignes. Le résultat du concours est
        porté par l'inscription et prime : c'est la DERNIÈRE étape du parcours,
        même pour un étudiant passé d'abord par le rattrapage. À défaut, une
        décision « autorisé au rattrapage » désigne la seconde session comme
        seule suite possible.
        """
        inscription = obj.inscription
        if inscription.statut == obj.decision:
            return ''
        if inscription.resultat_cnim:
            return 'concours'
        if obj.decision == LigneDeliberation.DECISION_RATTRAPAGE:
            return 'rattrapage'
        return ''

    def _rangs(self, obj) -> dict:
        return (self.context.get('rangs_semestriels') or {}).get(obj.pk, {})

    def get_rang_classe(self, obj) -> int | None:
        return self._rangs(obj).get('rang_classe')

    def get_rang_promotion(self, obj) -> int | None:
        return self._rangs(obj).get('rang_promotion')

    def get_effectif_classe(self, obj) -> int | None:
        return (self.context.get('effectifs_semestriels') or {}).get('effectif_classe')

    def get_effectif_promotion(self, obj) -> int | None:
        return (self.context.get('effectifs_semestriels') or {}).get('effectif_promotion')

    def get_motif_sans_proposition(self, obj) -> str:
        """
        Pourquoi aucune décision n'est proposée sur cette ligne.

        Sans cette explication, une colonne vide se lit comme un oubli du
        logiciel, et le jury tranche à l'aveugle ou attend une proposition qui
        ne viendra pas.
        """
        from . import services

        if obj.decision_auto:
            return ''
        deliberation = obj.deliberation
        if deliberation.portee == Deliberation.PORTEE_SEMESTRE:
            return ('Jury de semestre : le passage se décide en délibération '
                    'annuelle, une fois les deux semestres notés.')
        manquants = services.semestres_incomplets(
            obj.inscription, deliberation.annee_universitaire,
        )
        if manquants:
            return (f'{" et ".join(manquants)} incomplet(s) — le passage ne peut '
                    "se prononcer avant la fin de l'année.")
        if obj.moyenne_generale is None:
            return 'Aucune note retenue : la moyenne ne peut pas être calculée.'
        return ('La décision calculée ne figure pas parmi celles ouvertes à ce '
                'niveau — au jury de trancher.')

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
    classe_nom       = serializers.CharField(source='classe.nom', read_only=True, default='')
    statut_display   = serializers.CharField(source='get_statut_display', read_only=True)
    validee_par_nom  = serializers.CharField(source='validee_par.name', read_only=True, default='')
    nb_lignes        = serializers.SerializerMethodField()
    est_verrouillee  = serializers.BooleanField(read_only=True)
    nb_membres_jury  = serializers.SerializerMethodField()
    nb_signatures    = serializers.SerializerMethodField()
    nb_decisions_emises = serializers.SerializerMethodField()
    decisions_niveau    = serializers.SerializerMethodField()
    #: Combien d'étudiants ce jury a renvoyés au rattrapage, et le second jury
    #: qui les reprend s'il est déjà ouvert. L'écran s'en sert pour proposer
    #: l'ouverture — ou pour renvoyer vers le jury qui existe.
    nb_au_rattrapage    = serializers.SerializerMethodField()
    jury_rattrapage     = serializers.SerializerMethodField()
    fraudes_non_vues    = serializers.SerializerMethodField()

    class Meta:
        model  = Deliberation
        fields = [
            'id', 'institution', 'libelle', 'niveau', 'classe', 'classe_nom',
            'annee_universitaire', 'portee',
            'semestre', 'semestre_code', 'seuil_validation', 'plafond_rattrapage',
            'statut', 'statut_display', 'est_verrouillee', 'observations',
            'date_creation', 'date_calcul', 'date_validation',
            'validee_par', 'validee_par_nom', 'nb_lignes',
            'nb_membres_jury', 'nb_signatures', 'nb_decisions_emises',
            'decisions_niveau', 'jury_source', 'nb_au_rattrapage',
            'jury_rattrapage', 'fraudes_non_vues',
        ]
        read_only_fields = ['statut', 'date_calcul', 'date_validation', 'validee_par']

    def get_fraudes_non_vues(self, obj) -> list:
        """
        Sanctions pour fraude posées ou levées APRÈS la validation de ce jury,
        sur des notes qu'il a arrêtées : il a statué sur d'autres notes que
        celles qui figurent aujourd'hui en base, et doit redélibérer.
        """
        from .fraudes import sanctions_non_vues
        return [{'id': s.pk, 'etudiant_matricule': s.inscription.etudiant.matricule,
                 'etudiant_nom': s.inscription.etudiant.nom_display,
                 'statut': s.statut, 'cree_le': s.cree_le, 'levee_le': s.levee_le}
                for s in sanctions_non_vues(obj)]

    def get_nb_lignes(self, obj) -> int:
        return obj.lignes.count()

    def get_nb_membres_jury(self, obj) -> int:
        return obj.membres_jury.count()

    def get_nb_signatures(self, obj) -> int:
        return obj.membres_jury.exclude(signature_le=None).count()

    def get_nb_decisions_emises(self, obj) -> int:
        """Décisions déjà éditées — ce qu'une dévalidation remettrait en cause."""
        from . import services
        return services.decisions_emises(obj)

    def get_decisions_niveau(self, obj) -> list:
        """
        Décisions que le jury peut prononcer à ce niveau.

        Calculées ici et non déduites côté écran : la liste dépend du
        référentiel, qu'un client ne peut pas deviner à partir du seul code —
        « MPI » ne dit pas s'il s'agit d'une première ou d'une deuxième année.
        """
        from . import services

        libelles = dict(LigneDeliberation.DECISION_CHOICES)
        fiche = services.niveau_ref(obj.niveau, obj.institution)
        codes = list(fiche.decisions_possibles) if fiche and fiche.decisions_possibles else None
        if codes is None:
            rang = fiche.rang if fiche is not None else services.rang_du_niveau(obj.niveau)
            # En 2e année, le jury dispose des quatre issues du cursus : les
            # deux du jury annuel — concourir ou passer par le rattrapage — et
            # les deux que prononce le jury de rattrapage, selon que le droit
            # de redoublement reste ouvert.
            codes = ([LigneDeliberation.DECISION_ADMIS, LigneDeliberation.DECISION_REORIENTE]
                     if rang == 1 else
                     [LigneDeliberation.DECISION_CNIM, LigneDeliberation.DECISION_RATTRAPAGE,
                      LigneDeliberation.DECISION_REDOUBLANT,
                      LigneDeliberation.DECISION_REORIENTE])
        return [{'value': code, 'label': libelles.get(code, code)} for code in codes]

    def get_nb_au_rattrapage(self, obj) -> int:
        from .models import LigneDeliberation
        if obj.portee != Deliberation.PORTEE_ANNUELLE:
            return 0
        return obj.lignes.filter(
            decision=LigneDeliberation.DECISION_RATTRAPAGE).count()

    def get_jury_rattrapage(self, obj) -> int | None:
        enfant = obj.jurys_rattrapage.first() if obj.pk else None
        return enfant.pk if enfant else None

    def validate_niveau(self, valeur):
        """
        Le niveau doit exister au référentiel — la liste n'est plus figée dans
        le modèle, c'est donc ici qu'elle se vérifie.
        """
        codes = list(
            NiveauIPGEI.objects.filter(actif=True).values_list('code', flat=True)
        ) or [code for code, _ in NIVEAU_CHOICES]
        if valeur not in codes:
            raise serializers.ValidationError(
                f"Niveau inconnu. Valeurs attendues : {', '.join(codes)}."
            )
        return valeur

    def validate(self, attrs):
        portee   = attrs.get('portee',   getattr(self.instance, 'portee', None))
        semestre = attrs.get('semestre', getattr(self.instance, 'semestre', None))
        if portee == Deliberation.PORTEE_SEMESTRE and semestre is None:
            raise serializers.ValidationError(
                {'semestre': 'Une délibération de semestre doit cibler un semestre.'}
            )
        if portee == Deliberation.PORTEE_ANNUELLE:
            attrs['semestre'] = None

        classe = attrs.get('classe', getattr(self.instance, 'classe', None))
        # Exigée à la CRÉATION seulement : les jurys tenus avant la règle
        # portent tout un niveau, et les rouvrir pour les modifier ne doit pas
        # buter sur un champ qu'ils n'ont jamais eu.
        if self.instance is None and classe is None:
            raise serializers.ValidationError(
                {'classe': 'Le jury siège classe par classe : indiquez la classe '
                           'délibérée.'}
            )
        if classe is not None:
            niveau = attrs.get('niveau', getattr(self.instance, 'niveau', None))
            if niveau and classe.niveau_code != niveau:
                raise serializers.ValidationError(
                    {'classe': f'{classe.nom} relève du niveau {classe.niveau_code}, '
                               f'pas de {niveau}.'})
            annee = attrs.get('annee_universitaire',
                              getattr(self.instance, 'annee_universitaire', None))
            if annee and classe.annee_universitaire != annee:
                raise serializers.ValidationError(
                    {'classe': f'{classe.nom} est une classe de '
                               f'{classe.annee_universitaire}, pas de {annee}.'})
            if classe.est_conteneur:
                raise serializers.ValidationError(
                    {'classe': "La classe d'attente ne se délibère pas : ses "
                               "inscrits n'ont pas encore de classe."})
        return attrs


class MembreJuryIPGEISerializer(serializers.ModelSerializer):
    """
    Membre du jury. La signature n'est jamais écrite par cette voie : elle passe
    par l'action `signer`, qui vérifie que c'est bien le membre qui signe.
    """
    role_display    = serializers.CharField(source='get_role_display', read_only=True)
    utilisateur_nom = serializers.SerializerMethodField()
    a_signe         = serializers.BooleanField(read_only=True)

    class Meta:
        model  = MembreJuryIPGEI
        fields = [
            'id', 'deliberation', 'utilisateur', 'utilisateur_nom',
            'role', 'role_display', 'signature_le', 'a_signe',
        ]
        read_only_fields = ['signature_le']

    def get_utilisateur_nom(self, obj) -> str:
        return obj.utilisateur.name or obj.utilisateur.username


# ── EDT ──────────────────────────────────────────────────────────────────────
def _refuser_double_affectation(voisines, instance, prof, salle):
    """
    Un enseignant, une salle : un seul endroit à la fois.

    L'écran de saisie masque déjà ce qui est pris, mais il ne peut pas tout :
    deux responsables qui éditent deux classes en parallèle ne voient pas les
    listes l'un de l'autre, et l'API reste accessible directement. Sans cette
    règle au serveur, un professeur se retrouve sur deux classes au même
    moment — et ses heures sont comptées deux fois jusque dans la vacation.

    `voisines` couvre les séances des AUTRES classes sur le même créneau.
    """
    if instance is not None:
        voisines = voisines.exclude(pk=instance.pk)

    if prof is not None:
        ailleurs = voisines.filter(prof=prof).first()
        if ailleurs is not None:
            raise serializers.ValidationError(
                {'prof': f'{prof} assure déjà une séance sur ce créneau '
                         f'({_ou_est(ailleurs)}).'}
            )
    if salle is not None:
        ailleurs = voisines.filter(salle=salle).first()
        if ailleurs is not None:
            raise serializers.ValidationError(
                {'salle': f'La salle {salle} est déjà occupée sur ce créneau '
                          f'({_ou_est(ailleurs)}).'}
            )


def _ou_est(seance):
    """Nomme la classe — masquer un nom sans dire où il est pris est stérile."""
    classe = (seance.classe if hasattr(seance, 'classe')
              else seance.grille.classe)
    groupe = f' {seance.sous_groupe.groupe}' if seance.sous_groupe_id else ''
    return f'{classe.nom}{groupe}'


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

        if grille is not None:
            _refuser_double_affectation(
                SeanceType.objects.filter(
                    grille__type_semestre=grille.type_semestre,
                    grille__classe__annee_universitaire=grille.classe.annee_universitaire,
                    jour=jour, creneau=creneau,
                ).exclude(grille__classe_id=grille.classe_id),
                self.instance,
                attrs.get('prof',  getattr(self.instance, 'prof',  None)),
                attrs.get('salle', getattr(self.instance, 'salle', None)),
            )

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
            'date', 'origine', 'annulee', 'motif_annulation', 'observations',
            'nb_absents',
        ]
        # Le motif n'est posé que par le marquage d'un jour férié : l'écran ne
        # l'écrit jamais, il le lit pour afficher « ANNULÉE — FÉRIÉ ».
        read_only_fields = ['prof_initial', 'motif_annulation']
        # DRF dérive un validateur d'unicité de chaque UniqueConstraint du
        # modèle — mais en IGNORANT sa condition. La contrainte partielle qui
        # protège les cases de classe entière (`sous_groupe IS NULL`) se
        # transformait donc en interdiction générale, et ajouter le second
        # groupe d'un TP dédoublé était refusé. L'unicité reste garantie par la
        # base ; `validate()` ci-dessous la formule en messages lisibles.
        validators = []

    def validate(self, attrs):
        """
        Trois règles sur une même case horaire.

        L'une contre le doublon, l'autre contre le mélange — une case appartient
        à la classe entière OU à ses sous-groupes, jamais aux deux —, et la
        dernière contre les épreuves : on ne met pas une classe en cours pendant
        qu'elle compose.
        """
        def valeur(champ):
            return attrs.get(champ, getattr(self.instance, champ, None))

        self._refuser_jour_ferme(attrs, valeur)
        _exiger_matiere(valeur('type_seance'), valeur('matiere'))
        self._refuser_creneau_d_epreuve(
            valeur('classe'), valeur('semaine'), valeur('jour'), valeur('creneau'))

        _refuser_double_affectation(
            SeanceReelle.objects.filter(
                semaine=valeur('semaine'), jour=valeur('jour'),
                creneau=valeur('creneau'), annulee=False,
            ).exclude(classe=valeur('classe')),
            self.instance, valeur('prof'), valeur('salle'),
        )

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

    def _refuser_jour_ferme(self, attrs, valeur):
        """
        Les règles des jours fériés, CÔTÉ SERVEUR — l'écran ne fait que les refléter.

          * AJOUTER une séance : refusé sur tout jour hors cours ;
          * MODIFIER une séance existante : refusé sur un férié ISOLÉ — elle
            reviendrait autre qu'elle n'était au retrait du férié ;
          * la RÉTABLIR à la main : refusé sur tout jour hors cours, c'est la
            placer ;
          * la DÉPLACER vers un jour fermé : refusé, c'est l'y ajouter.
        """
        from django.core.exceptions import ValidationError as DjangoValidationError

        from . import feries

        try:
            if self.instance is None:
                feries.refuser_ajout(valeur('semaine'), valeur('jour'))
                return
            feries.refuser_modification(self.instance)
            deplacee = (valeur('semaine') != self.instance.semaine
                        or valeur('jour') != self.instance.jour)
            if deplacee:
                feries.refuser_ajout(valeur('semaine'), valeur('jour'))
            if self.instance.annulee and attrs.get('annulee') is False:
                feries.refuser_retablissement(self.instance)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'jour': exc.messages})

    def update(self, instance, validated_data):
        # Une annulation ou un rétablissement faits À LA MAIN effacent le motif :
        # la séance n'est plus « annulée par le férié », et le retrait du férié
        # ne doit plus y toucher.
        if 'annulee' in validated_data and validated_data['annulee'] != instance.annulee:
            validated_data['motif_annulation'] = ''
        return super().update(instance, validated_data)

    @staticmethod
    def _refuser_creneau_d_epreuve(classe, semaine, jour, creneau):
        """
        On ne planifie pas un cours là où la classe compose.

        Le pendant du contrôle que l'épreuve applique déjà en sens inverse
        (`EpreuveIPGEISerializer._verifier_creneaux_libres`). Il manquait ici :
        une épreuve n'entre pas dans la grille — sa durée ne tombe pas sur les
        créneaux de 1h30 —, si bien que rien n'empêchait d'y poser une séance.

        Le calcul est mutualisé avec la duplication : une seule règle, un seul
        endroit où elle peut se tromper.
        """
        if not all((classe, semaine, jour, creneau)):
            return
        from . import services

        pris = services.creneaux_pris_par_epreuve(classe, [semaine])
        libelle = pris.get((semaine.pk, jour.pk, creneau.pk))
        if libelle is not None:
            raise serializers.ValidationError({'creneau': (
                f'{libelle} occupe ce créneau : la classe compose, elle ne peut '
                'pas être en cours.')})

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


class DupliquerSemaineSerializer(serializers.Serializer):
    """
    Recopie une semaine réelle vers d'autres — le pendant de la grille type.

    `semaines` désigne les cibles explicitement, ou `nb_semaines` prend les N
    suivantes. Les deux sont acceptés parce que les deux gestes existent :
    « celles-ci, que je coche » et « les huit prochaines ».
    """
    semaine_source = serializers.IntegerField()
    semaines       = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True,
        help_text='Semaines cibles. Vide, `nb_semaines` prend le relais.',
    )
    nb_semaines    = serializers.IntegerField(required=False, min_value=1, max_value=40)
    ecraser        = serializers.BooleanField(default=False)

    def validate(self, attrs):
        if not attrs.get('semaines') and not attrs.get('nb_semaines'):
            raise serializers.ValidationError(
                'Indiquez les semaines à alimenter, ou un nombre de semaines.')
        return attrs


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
    # Contrepartie de l'échange — vide sur un transfert simple.
    etudiant_b_nom       = serializers.CharField(
        source='inscription_b.etudiant.nom_display', read_only=True, default='')
    etudiant_b_matricule = serializers.CharField(
        source='inscription_b.etudiant.matricule', read_only=True, default='')
    est_echange        = serializers.BooleanField(read_only=True)
    statut_display     = serializers.CharField(source='get_statut_display', read_only=True)
    demande_par_nom    = serializers.CharField(source='demande_par.name', read_only=True, default='')

    class Meta:
        model  = PermutationEtudiant
        fields = [
            'id', 'inscription', 'etudiant_nom', 'etudiant_matricule',
            'inscription_b', 'etudiant_b_nom', 'etudiant_b_matricule', 'est_echange',
            'classe_source', 'classe_source_nom', 'classe_cible', 'classe_cible_nom',
            'sous_groupe_cible', 'sous_groupe_cible_b', 'statut', 'statut_display', 'action_directe',
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

        # ── Échange réciproque ─────────────────────────────────────────────
        contrepartie = attrs.get('inscription_b', getattr(self.instance, 'inscription_b', None))
        sg_b = attrs.get('sous_groupe_cible_b', getattr(self.instance, 'sous_groupe_cible_b', None))
        if contrepartie:
            if inscription and contrepartie.pk == inscription.pk:
                raise serializers.ValidationError(
                    {'inscription_b': 'Un étudiant ne permute pas avec lui-même.'})
            if not contrepartie.actif:
                raise serializers.ValidationError(
                    {'inscription_b': "L'inscription de la contrepartie n'est pas active."})
            # Sans cette règle, l'« échange » déplacerait deux élèves sans
            # qu'aucune place ne se libère là où il faut.
            if cible and contrepartie.classe_id != cible.id:
                raise serializers.ValidationError({'inscription_b': (
                    f'La contrepartie doit être inscrite en {cible.nom}.')})
        if sg_b:
            if not contrepartie:
                raise serializers.ValidationError({'sous_groupe_cible_b': (
                    'Aucune contrepartie : ce sous-groupe est sans objet.')})
            if inscription and sg_b.classe_id != inscription.classe_id:
                raise serializers.ValidationError({'sous_groupe_cible_b': (
                    "Ce sous-groupe n'appartient pas à la classe que rejoint la contrepartie.")})
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


# ── Emplois du temps archivés ────────────────────────────────────────────────

class EmploiArchiveSerializer(serializers.ModelSerializer):
    """
    Une séance archivée, **au format d'une séance vivante**.

    Volontairement : l'écran d'historique réutilise tel quel le composant de
    consultation de l'emploi du temps. Une archive qui s'afficherait autrement
    que l'original ne permettrait pas la comparaison, qui est sa seule raison
    d'être.
    """
    jour        = serializers.IntegerField(source='jour_ref',        read_only=True)
    creneau     = serializers.IntegerField(source='creneau_ref',     read_only=True)
    matiere     = serializers.IntegerField(source='matiere_ref',     read_only=True)
    type_seance = serializers.IntegerField(source='type_seance_ref', read_only=True)
    sous_groupe = serializers.IntegerField(source='sous_groupe_ref', read_only=True)

    class Meta:
        model  = EmploiArchive
        fields = [
            'id', 'jour', 'creneau', 'matiere', 'type_seance', 'sous_groupe',
            'matiere_code', 'matiere_intitule', 'type_seance_libelle',
            'type_seance_special', 'prof_nom', 'prof_initial_nom', 'salle_nom',
            'classe_nom', 'sous_groupe_libelle', 'annulee', 'origine',
            'date_seance', 'version', 'genere_le',
        ]


class VersionArchiveSerializer(serializers.Serializer):
    """Une prise de vue disponible : quelle semaine, quelle version, quand."""
    semaine      = serializers.IntegerField()
    numero       = serializers.IntegerField()
    date_debut   = serializers.DateField(allow_null=True)
    date_fin     = serializers.DateField(allow_null=True)
    semestre     = serializers.CharField()
    version      = serializers.IntegerField()
    genere_le    = serializers.DateTimeField()
    nb_seances   = serializers.IntegerField()
