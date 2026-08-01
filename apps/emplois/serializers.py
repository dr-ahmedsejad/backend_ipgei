from rest_framework import serializers
from .models import Emplois, EmploisArchive


class EmploisSerializer(serializers.ModelSerializer):
    prof_nom           = serializers.CharField(source='prof.nom',                  read_only=True, allow_null=True)
    em_code            = serializers.CharField(source='em.code_em',                read_only=True, allow_null=True)
    em_intitule        = serializers.CharField(source='em.intitule',               read_only=True, allow_null=True)
    dept_nom           = serializers.CharField(source='departement.nom',           read_only=True, allow_null=True)
    salle_nom          = serializers.CharField(source='salle.nom',                 read_only=True, allow_null=True)
    semestre_nom       = serializers.CharField(source='semestre.semestre',         read_only=True, allow_null=True)
    creneau_label      = serializers.CharField(source='creneau_fk.creneau',        read_only=True, allow_null=True)
    type_seance_label  = serializers.CharField(source='type_seance_fk.type_seance', read_only=True, allow_null=True)
    jour_label         = serializers.CharField(source='jour_fk.jour',              read_only=True, allow_null=True)

    # Alias retrocompatibles (avant Phase 5, type_seance et jour etaient des
    # CharField directs ; le frontend lit toujours `e.type_seance` et `e.jour`).
    # On expose ces alias en lecture seule pour que la grille s'affiche
    # correctement sans toucher au frontend.
    type_seance        = serializers.CharField(source='type_seance_fk.type_seance', read_only=True, allow_null=True)
    jour               = serializers.CharField(source='jour_fk.jour',              read_only=True, allow_null=True)

    # Mode d'affichage cellule : True -> uniquement le type centre (Sport, Instruction militaire, ...)
    type_seance_is_special = serializers.BooleanField(source='type_seance_fk.is_special', read_only=True, default=False)

    class Meta:
        model  = Emplois
        fields = '__all__'


class EmploisCreateSerializer(serializers.ModelSerializer):
    """
    Serializer create/update tolerant : accepte aussi les "anciennes formes"
    envoyees par le frontend (jour='Lundi', type_seance='CM') herites
    d'avant la Phase 5 (CharField -> FK). Resout en jour_fk_id / type_seance_fk_id.

    Auto-injecte aussi `institution` depuis `departement.institution` si absent
    (contrainte NOT NULL au niveau modele Django).
    """
    # Champs legacy : ecriture seule, ignores au render. Permet de recevoir
    # 'Lundi' / 'CM' du frontend sans casser la nouvelle structure FK.
    jour         = serializers.CharField(write_only=True, required=False, allow_blank=True)
    type_seance  = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model  = Emplois
        exclude = ['taux_paiement']  # auto-renseigne par save()
        extra_kwargs = {
            # `institution` est NOT NULL en modele mais on l'auto-derive du departement.
            'institution': {'required': False, 'allow_null': True},
        }

    # ── Resolution legacy `jour` -> jour_fk ───────────────────────────────────
    def _resolve_jour_fk(self, jour_label):
        """'Lundi' -> Jour.id ; '3' -> Jour.id ; '' / None -> None."""
        from apps.parametres.models import Jour
        if jour_label is None:
            return None
        s = str(jour_label).strip()
        if not s:
            return None
        if s.isdigit():
            return Jour.objects.filter(pk=int(s)).values_list('pk', flat=True).first()
        return Jour.objects.filter(jour=s).values_list('pk', flat=True).first()

    # ── Resolution legacy `type_seance` -> type_seance_fk ─────────────────────
    def _resolve_seance_fk(self, type_seance_label):
        """'CM' -> Seance.id ; '2' -> Seance.id ; '' / None -> None."""
        from apps.parametres.models import Seance
        if type_seance_label is None:
            return None
        s = str(type_seance_label).strip()
        if not s:
            return None
        if s.isdigit():
            return Seance.objects.filter(pk=int(s)).values_list('pk', flat=True).first()
        return Seance.objects.filter(type_seance=s).values_list('pk', flat=True).first()

    def _resolve_institution_from_departement(self, departement):
        """Recupere l'institution depuis le departement (Departement.institution)
        ou fallback sur l'institution principale."""
        if departement is None:
            from apps.parametres.models import Institution
            return Institution.objects.filter(est_principale=True).first()
        # `departement` est l'instance Django apres validation DRF
        if hasattr(departement, 'institution') and departement.institution_id:
            return departement.institution
        from apps.parametres.models import Institution
        return Institution.objects.filter(est_principale=True).first()

    def validate(self, attrs):
        # 1. Pop les legacy fields
        jour_legacy   = attrs.pop('jour',        None)
        seance_legacy = attrs.pop('type_seance', None)

        # 2. Resoudre jour -> jour_fk (uniquement si pas deja fourni en clair)
        if 'jour_fk' not in attrs and jour_legacy is not None:
            jid = self._resolve_jour_fk(jour_legacy)
            if jid:
                from apps.parametres.models import Jour
                attrs['jour_fk'] = Jour.objects.get(pk=jid)
            # Si la resolution echoue (libelle inconnu), on laisse jour_fk a None
            # (champ nullable, Django acceptera, l'integrite metier se gere ailleurs).

        # 3. Resoudre type_seance -> type_seance_fk
        if 'type_seance_fk' not in attrs and seance_legacy is not None:
            sid = self._resolve_seance_fk(seance_legacy)
            if sid:
                from apps.parametres.models import Seance
                attrs['type_seance_fk'] = Seance.objects.get(pk=sid)

        # 4. Auto-injection institution si absente
        if not attrs.get('institution'):
            attrs['institution'] = self._resolve_institution_from_departement(
                attrs.get('departement'),
            )

        # 5. Type special (Sport, Instruction militaire...) : forcer prof/em/salle a None
        # meme si le client envoie des valeurs (defense-in-depth contre des creneaux
        # qui auraient ete crees avec un autre type avant que l'utilisateur ne bascule).
        seance_fk = attrs.get('type_seance_fk')
        if seance_fk and getattr(seance_fk, 'is_special', False):
            attrs['prof']  = None
            attrs['em']    = None
            attrs['salle'] = None

        return attrs


class DisponibiliteCheckSerializer(serializers.Serializer):
    departement_id      = serializers.IntegerField()
    # `jour` accepte soit le libelle ('Lundi'), soit l'ID numerique. La vue resout en jour_fk_id.
    jour                = serializers.CharField()
    creneau_id          = serializers.IntegerField()
    annee_universitaire = serializers.CharField()
    semestre_id         = serializers.IntegerField(required=False)
    prof_id             = serializers.IntegerField(required=False, allow_null=True)
    salle_id            = serializers.IntegerField(required=False, allow_null=True)
    exclude_id          = serializers.IntegerField(required=False)
