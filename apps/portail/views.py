"""
Portail étudiant — vues.
Toutes les vues exigent le rôle 'etudiant' via IsEtudiant.
L'étudiant ne voit que ses propres données (filtrées par request.user.etudiant_profile).
"""

from rest_framework import generics, status
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsEtudiant
from apps.absence.models import Presence
from apps.reclamations.models import Reclamation
from apps.reclamations.serializers import ReclamationCreateSerializer, ReclamationSerializer
from .serializers import ProfilEtudiantSerializer, AbsenceEtudiantSerializer

# ── Suivi des générations PDF en cours ────────────────────────────────────────
# Évite de lancer plusieurs wkhtmltopdf simultanément pour le même document.


def _get_etudiant(request):
    """Retourne le profil étudiant lié à l'utilisateur connecté."""
    from rest_framework.exceptions import NotFound
    try:
        return request.user.etudiant_profile
    except Exception:
        raise NotFound("Profil étudiant introuvable. Contactez l'administration.")


# ── Profil ────────────────────────────────────────────────────────────────────
class MonProfilView(generics.RetrieveUpdateAPIView):
    """GET / PATCH /api/v1/portail/profil/ — champs éditables uniquement."""
    serializer_class   = ProfilEtudiantSerializer
    permission_classes = [IsEtudiant]
    parser_classes     = [MultiPartParser, FormParser, JSONParser]

    def get_object(self):
        return _get_etudiant(self.request)


# ── Emploi du temps ───────────────────────────────────────────────────────────
def _annee_demandee(request):
    """Année universitaire consultée (« 2025-2026 »), passée en `?annee=` par
    l'app mobile pour revoir une année passée ; None = année en cours."""
    return (request.query_params.get('annee') or '').strip() or None


class MesAnneesView(APIView):
    """GET /api/v1/portail/annees/
    Années où l'étudiant a été inscrit (inscriptions IPGEI), de la plus récente
    à la plus ancienne : l'app mobile permet de revoir une année passée. La
    première est l'année en cours.
    """
    permission_classes = [IsEtudiant]

    def get(self, request):
        from .ipgei_source import _rang_du_niveau, inscriptions_de
        etudiant = _get_etudiant(request)
        vues, annees = set(), []
        for inscription in inscriptions_de(etudiant):
            annee = inscription.annee_universitaire
            if annee in vues:
                continue
            vues.add(annee)
            classe = inscription.classe
            try:
                niveau = _rang_du_niveau(classe)
            except Exception:
                niveau = 0
            annees.append({
                'annee':    annee,
                'filiere':  getattr(classe, 'niveau_code', '') or '',
                'niveau':   niveau or 0,
                'courante': not annees,
            })
        return Response(annees)


class MonEmploiView(APIView):
    """GET /api/v1/portail/emploi-du-temps/
    Retourne la grille emploi du temps de l'étudiant (même format que suivi/grille).
    """
    permission_classes = [IsEtudiant]

    def get(self, request):
        from collections import defaultdict
        from django.db.models import Max, Q
        from apps.suivi.models import Suivie
        from apps.parametres.models import Creneau, Seance

        from apps.ipgei.models import SemestreIPGEI

        from .ipgei_source import inscription_courante

        etudiant = _get_etudiant(request)

        # LE CONTEXTE VIENT DE L'INSCRIPTION IPGEI, non de l'inscription
        # pédagogique du socle : cette dernière est vide depuis que l'IPGEI l'a
        # remplacée, et l'écran rendait donc une grille vide sans rien dire.
        #
        # `Suivie` en revanche est bien alimentée — par la projection
        # `apps/ipgei/miroir.py` —, et son `departement_id` EST l'identifiant
        # de la classe : une classe IPGEI hérite de `Departement`, une seule
        # ligne logique pour les deux. Rien à rebrancher de ce côté.
        inscription = inscription_courante(etudiant, _annee_demandee(request))
        if inscription is None:
            return Response({'creneaux': [], 'grille': {}, 'annee': '', 'semestre': ''})

        annee_univ  = inscription.annee_universitaire
        dept_id     = inscription.classe_id
        # Le semestre du socle ne sert qu'à départager deux suivis d'une même
        # semaine ; l'IPGEI n'en a pas l'équivalent ici, et `Suivie` est déjà
        # restreint à l'année et à la classe.
        semestre_id = None

        # ── Résolution créneau label → id ──
        creneaux_all = list(Creneau.objects.values('id', 'creneau', 'ordre'))
        creneau_to_id = {}
        for c in creneaux_all:
            creneau_to_id[c['creneau']] = c['id']
            creneau_to_id[str(c['id'])] = c['id']

        # ── Résolution type_seance ──
        seance_to_label = {}
        for s in Seance.objects.values('id', 'type_seance'):
            seance_to_label[str(s['id'])]     = s['type_seance']
            seance_to_label[s['type_seance']] = s['type_seance']

        # ── Semaines disponibles pour ce dept/annee ──
        base_qs = Suivie.objects.filter(
            annee_universitaire=annee_univ,
            departement_id=dept_id,
        )
        semaines_dispo = sorted(
            base_qs.values_list('numero_semaine', flat=True).distinct()
        )
        if not semaines_dispo:
            return Response({'creneaux': [], 'grille': {}, 'annee': annee_univ, 'semestre': '', 'semaines': [], 'semaines_dates': {}, 'semaine_actuelle': None})

        # ── Dates début/fin par numéro de semaine ──
        from apps.parametres.models import Semaine as SemaineModel
        from django.db.models import Min, Max as MaxDate
        dates_qs = SemaineModel.objects.filter(
            annee_universitaire=annee_univ,
            numero_semaine__in=semaines_dispo,
        ).values('numero_semaine').annotate(
            debut=Min('date'),
            fin=MaxDate('date'),
        )
        semaines_dates = {
            row['numero_semaine']: {
                'debut': row['debut'].strftime('%d/%m/%Y') if row['debut'] else '',
                'fin':   row['fin'].strftime('%d/%m/%Y')   if row['fin']   else '',
            }
            for row in dates_qs
        }

        # ── Semaine demandée ou courante ──
        semaine_param = request.query_params.get('semaine')
        if semaine_param and semaine_param.isdigit():
            semaine_num = int(semaine_param)
            if semaine_num not in semaines_dispo:
                semaine_num = semaines_dispo[-1]
        else:
            # Trouver la semaine correspondant à aujourd'hui via le modèle Semaine
            from django.utils import timezone
            from apps.parametres.models import Semaine as SemaineModel
            today = timezone.localdate()
            semaine_obj = SemaineModel.objects.filter(
                annee_universitaire=annee_univ,
                date__lte=today,
            ).order_by('-date').first()
            if semaine_obj and semaine_obj.numero_semaine in semaines_dispo:
                semaine_num = semaine_obj.numero_semaine
            else:
                # Fallback : semaine la plus proche dans le passé parmi les disponibles
                passees = [s for s in semaines_dispo if semaine_obj and s <= semaine_obj.numero_semaine] if semaine_obj else []
                semaine_num = passees[-1] if passees else semaines_dispo[-1]

        # ── type_semestre du semestre de l'étudiant ──
        type_semestre = None
        if semestre_id:
            from apps.parametres.models import Semestre as SemestreModel
            try:
                type_semestre = SemestreModel.objects.values_list('type_semestre', flat=True).get(pk=semestre_id)
            except SemestreModel.DoesNotExist:
                pass

        # ── Filtre semestre ──
        if semestre_id and type_semestre:
            filtre_sem = (
                Q(semestre_id=semestre_id, type_semestre=type_semestre) |
                Q(semestre_id__isnull=True, type_semestre=type_semestre)
            )
        elif semestre_id:
            filtre_sem = Q(semestre_id=semestre_id) | Q(semestre_id__isnull=True)
        else:
            filtre_sem = Q()

        # ── Séances de la semaine sélectionnée ──
        # Phase 5 : les CharField legacy (creneau/jour/type_seance) sont supprimés.
        # On lit uniquement via les FK : creneau_fk, jour_fk, type_seance_fk.
        qs_seances = (
            base_qs
            .filter(numero_semaine=semaine_num)
            .filter(filtre_sem)
            .select_related('prof', 'em', 'salle', 'creneau_fk', 'jour_fk', 'type_seance_fk')
        )

        # ── Colonnes créneau ──
        cr_ids = set(
            s['creneau_fk_id']
            for s in qs_seances.values('creneau_fk_id')
            if s['creneau_fk_id']
        )

        creneaux_used = list(
            Creneau.objects.filter(pk__in=cr_ids).order_by('ordre').values('id', 'creneau', 'ordre')
        )

        # ── Construction grille ──
        grille = defaultdict(lambda: defaultdict(list))
        for s in qs_seances:
            cr_id = s.creneau_fk_id
            jour  = s.jour_fk.jour if s.jour_fk_id and s.jour_fk else None
            ts    = s.type_seance_fk.type_seance if s.type_seance_fk_id and s.type_seance_fk else None
            if not cr_id or not jour or (not s.em_id and not s.prof_id):
                continue
            grille[jour][str(cr_id)].append({
                'id':          s.pk,
                'type_seance': seance_to_label.get(ts or '', ts or ''),
                'prof_nom':    s.prof.nom if s.prof else None,
                'em_code':     s.em.code_em if s.em else None,
                'em_intitule': s.em.intitule if s.em else None,
                'salle_nom':   s.salle.nom if s.salle else None,
                'numero_semaine': s.numero_semaine,
            })

        # Le semestre affiché en en-tête vient du cursus IPGEI : celui de la
        # parité en cours pour le niveau de la classe. `ip.semestre` venait de
        # l'inscription pédagogique du socle, que ce module ne lit plus.
        semestre_nom = ''
        semestre_courant = next(
            (s for s in SemestreIPGEI.objects.filter(
                annee_universitaire=annee_univ).order_by('code')
             if inscription.niveau in s.niveaux), None)
        if semestre_courant is not None:
            semestre_nom = semestre_courant.code

        return Response({
            'creneaux':        creneaux_used,
            'grille':          {jour: dict(crs) for jour, crs in grille.items()},
            'annee':           annee_univ,
            'semestre':        semestre_nom,
            'semaines':        semaines_dispo,
            'semaines_dates':  semaines_dates,
            'semaine_actuelle': semaine_num,
        })


# ── Absences ──────────────────────────────────────────────────────────────────
class MesAbsencesView(APIView):
    """GET /api/v1/portail/absences/"""
    permission_classes = [IsEtudiant]

    def get(self, request):
        etudiant = _get_etudiant(request)
        qs = Presence.objects.select_related(
            'suivi__creneau_fk', 'suivi__em', 'suivi__prof', 'suivi__jour_fk',
        ).filter(etudiant=etudiant).exclude(statut=0).order_by(
            '-suivi__annee_universitaire', '-suivi__numero_semaine', 'suivi__jour_fk__jour'
        )
        annee = _annee_demandee(request)
        if annee:
            qs = qs.filter(suivi__annee_universitaire=annee)
        serializer = AbsenceEtudiantSerializer(qs, many=True)
        return Response(serializer.data)


# ── Notes ─────────────────────────────────────────────────────────────────────
class MesNotesView(APIView):
    """
    GET /api/v1/portail/notes/

    Les notes IPGEI de toutes les années de l'étudiant, et non la couche LMD
    que ce module lisait — elle est vide depuis que l'IPGEI l'a remplacée.

    Seuls les semestres dont le JURY A STATUÉ sont rendus : une moyenne n'est
    pas une décision, et la publier avant la délibération remplit le guichet de
    réclamations sur des chiffres provisoires.
    """
    permission_classes = [IsEtudiant]

    def get(self, request):
        from apps.ipgei import services

        from .ipgei_source import inscriptions_de, note_en_ligne, notes_publiables

        etudiant = _get_etudiant(request)
        lignes = []
        for inscription in inscriptions_de(etudiant, _annee_demandee(request)):
            seuil = services.seuil_applicable(
                inscription, inscription.annee_universitaire)
            lignes += [note_en_ligne(n, seuil)
                       for n in notes_publiables(inscription)]
        return Response(lignes)


# ── Résultats semestriels ─────────────────────────────────────────────────────
class MesResultatsView(APIView):
    """
    GET /api/v1/portail/resultats/semestres/

    La moyenne de chaque semestre DÉLIBÉRÉ, et la décision du jury.

    La moyenne rendue est celle que le jury a FIGÉE sur sa ligne quand elle
    existe, non un recalcul : le portail doit dire ce que le jury a vu, sinon
    l'étudiant lit un chiffre que son PV ne porte pas.
    """
    permission_classes = [IsEtudiant]

    def get(self, request):
        from apps.ipgei import services
        from apps.ipgei.models import (Deliberation, LigneDeliberation,
                                       SemestreIPGEI)

        from .ipgei_source import inscriptions_de

        etudiant = _get_etudiant(request)
        sorties = []
        for inscription in inscriptions_de(etudiant):
            # SEMESTRIELLES **ET** ANNUELLES. N'exposer que les premières
            # rendait une liste vide pour tout le monde : au relevé du
            # 15/09/2026, les onze délibérations validées sont annuelles ou de
            # rattrapage, aucune n'est rattachée à un semestre. Un écran vide
            # là où un jury a statué se lit comme une panne.
            for ligne in (LigneDeliberation.objects
                          .filter(inscription=inscription,
                                  deliberation__statut=Deliberation.STATUT_VALIDEE)
                          .select_related('deliberation__semestre')
                          .order_by('deliberation__portee')):
                semestre = ligne.deliberation.semestre
                # La valeur figée d'abord ; le recalcul seulement si le jury
                # n'en a pas gardé — un jury ancien peut ne rien avoir figé.
                moyenne = ligne.moyenne_generale
                if moyenne is None and semestre is not None:
                    moyenne, _, _ = services.moyenne_semestrielle(
                        inscription, semestre, absents_a_zero=True)
                sorties.append({
                    'id':              ligne.pk,
                    'semestre':        semestre.code if semestre else 'Année',
                    'annee_univ':      ligne.deliberation.annee_universitaire,
                    'moyenne':         float(moyenne) if moyenne is not None else None,
                    # Pas de crédits à l'IPGEI : le total des coefficients est
                    # ce qui pèse, et le relevé le porte déjà.
                    'credits_valides': 0,
                    'decision':        ligne.get_decision_display() or '',
                    'filiere':         inscription.classe.nom,
                })
        return Response(sorties)


# ── Documents ─────────────────────────────────────────────────────────────────
class DocumentsDisponiblesView(APIView):
    """
    `GET /api/v1/portail/documents/disponibles/`

    La liste des documents que l'étudiant peut télécharger : l'attestation
    d'inscription, et un relevé par semestre dont le jury a statué.

    PAS DE PRÉ-GÉNÉRATION, ET IL NE FAUT PAS LA RÉINTRODUIRE TELLE QU'ELLE ÉTAIT.
    Le socle pré-rendait les PDF en threads démons pour rendre le téléchargement
    instantané (`_pregen_pdf_bg`). Ce code était débranché et lisait la couche
    LMD vide ; il a été retiré plutôt que porté, pour deux raisons qui tiennent
    au registre officiel de l'IPGEI :

    1. Il RÉÉCRIVAIT un document déjà émis. Quand les notes changeaient, il
       supprimait le PDF et le régénérait sous le MÊME numéro de série et le
       MÊME jeton de vérification. Une pièce remise à un étudiant aurait donc,
       vérifiée en ligne plus tard, affiché un autre contenu que celui qu'il
       tient en main. À l'IPGEI une pièce émise est scellée : si les notes
       changent, on émet une NOUVELLE pièce, l'ancienne reste ce qu'elle était.

    2. Il écrasait le SCEAU. `hash_sha256` est l'empreinte des OCTETS du PDF
       (`ipgei.documents.generer_pdf`) ; ce code y écrivait une empreinte des
       NOTES. Deux sens dans un même champ, et le premier cassé.

    Pré-générer sans ces défauts exigerait de créer les entrées du registre
    AVANT toute demande — des numéros de série consommés et des pièces
    inscrites que personne n'a demandées, dans un registre dont c'est
    précisément le rôle de dire ce qui a été délivré. Le premier
    téléchargement prend quelques secondes ; c'est le prix d'un registre juste.
    """
    permission_classes = [IsEtudiant]

    def get(self, request):
        from apps.ipgei.models import Deliberation, LigneDeliberation, SemestreIPGEI

        from .ipgei_source import inscription_courante, inscriptions_de

        etudiant = _get_etudiant(request)
        # L'inscription IPGEI, non l'administrative du socle : cette dernière
        # est vide, et l'écran ne proposait donc AUCUN document — pas même
        # l'attestation, que rien n'empêche pourtant de délivrer.
        inscription = inscription_courante(etudiant)
        if inscription is None:
            return Response({'attestation': None, 'releves': []})

        annee_univ = inscription.annee_universitaire

        attestation = {
            'type_document': 'ipgei_attestation_inscription',
            'label':         "Attestation d'inscription",
            'annee_univ':    annee_univ,
        }

        # Les relevés de TOUTES les années, y compris historiques — et
        # seulement ceux dont le jury a statué. Un relevé remis avant la
        # délibération porte des chiffres qui ne sont pas des décisions.
        releves = []
        for insc in inscriptions_de(etudiant):
            semestres = (SemestreIPGEI.objects
                         .filter(pk__in=LigneDeliberation.objects
                                 .filter(inscription=insc,
                                         deliberation__statut=Deliberation.STATUT_VALIDEE,
                                         deliberation__semestre__isnull=False)
                                 .values_list('deliberation__semestre_id', flat=True))
                         .order_by('code'))
            for semestre in semestres:
                releves.append({
                    'type_document':  'ipgei_releve_semestre',
                    'semestre_id':    semestre.pk,
                    'semestre_code':  semestre.code,
                    'semestre_label': f'Semestre {semestre.code[1:]}',
                    'annee_univ':     insc.annee_universitaire,
                    'label': f'Relevé de notes — {semestre.code} '
                             f'({insc.annee_universitaire})',
                })
        return Response({'attestation': attestation, 'releves': releves})

class TelechargerDirectView(APIView):
    """
    GET /api/v1/portail/documents/telecharger-direct/?type=…&semestre=…

    Délivre le document par les GÉNÉRATEURS IPGEI, et non par ceux du socle.

    L'ancien corps — trois cents lignes — cherchait l'inscription
    administrative du socle, hachait les notes de la couche LMD pour détecter un
    changement, et pré-générait en threads démons. Toutes ces tables sont vides
    depuis que l'IPGEI les a remplacées : l'étudiant recevait « Aucune
    inscription trouvée ».

    Les générateurs IPGEI font déjà ce que ce code réimplémentait : registre,
    numéro de série, empreinte SHA-256, QR de vérification, mention de
    duplicata. Les appeler vaut mieux que de les doubler.

    Le RELEVÉ reste verrouillé tant qu'aucun jury validé n'a statué sur le
    semestre — l'attestation, elle, ne dépend d'aucune délibération.
    """
    permission_classes = [IsEtudiant]

    TYPES = {
        'ipgei_attestation_inscription': 'attestation',
        'ipgei_releve_semestre':         'releve_semestre',
        'ipgei_releve_annuel':           'releve_annuel',
        # Les noms du socle restent acceptés : d'anciens liens les portent.
        'attestation_inscription':       'attestation',
        'releve_semestre':               'releve_semestre',
    }

    def get(self, request):
        from django.http import HttpResponse

        from apps.ipgei import documents as docs_ipgei
        from apps.ipgei.models import SemestreIPGEI

        from .ipgei_source import (document_deja_delivre, inscriptions_de,
                                   jury_a_statue)

        demande = request.query_params.get('type')
        semestre_id = request.query_params.get('semestre')
        if not demande:
            return Response({'detail': 'Paramètre type requis.'},
                            status=status.HTTP_400_BAD_REQUEST)
        genre = self.TYPES.get(demande)
        if genre is None:
            return Response({'detail': f'Type de document inconnu : {demande}.'},
                            status=status.HTTP_400_BAD_REQUEST)

        etudiant = _get_etudiant(request)
        base_url = request.build_absolute_uri('/').rstrip('/')

        semestre = None
        if genre == 'releve_semestre':
            if not semestre_id:
                return Response({'detail': 'Paramètre semestre requis pour un relevé.'},
                                status=status.HTTP_400_BAD_REQUEST)
            semestre = SemestreIPGEI.objects.filter(pk=semestre_id).first()
            if semestre is None:
                return Response({'detail': 'Semestre introuvable.'},
                                status=status.HTTP_404_NOT_FOUND)

        # L'inscription de l'ANNÉE du semestre demandé : un étudiant peut
        # retirer le relevé d'une année passée, et c'est l'inscription de
        # cette année-là qui porte ses notes.
        annee = semestre.annee_universitaire if semestre else None
        inscription = inscriptions_de(etudiant, annee).first()
        if inscription is None:
            return Response({'detail': "Aucune inscription pour cette année."},
                            status=status.HTTP_404_NOT_FOUND)

        if genre in ('releve_semestre', 'releve_annuel') and not jury_a_statue(
                inscription, semestre):
            return Response(
                {'detail': "Ce relevé n'est pas encore disponible : le jury n'a "
                           'pas délibéré.'},
                status=status.HTTP_403_FORBIDDEN)

        # ON RESSERT AVANT DE REGÉNÉRER. `creer_document` enregistre chaque
        # délivrance — juste au guichet, ruineux au portail, où l'étudiant
        # rafraîchit et reclique. Un étudiant a déjà cinq exemplaires du même
        # relevé au registre ; à trois cents, la table et les numéros de série
        # s'useraient en quelques semaines.
        types_ipgei = {
            'attestation':     docs_ipgei.TYPE_ATTESTATION,
            'releve_semestre': docs_ipgei.TYPE_RELEVE_SEMESTRE,
            'releve_annuel':   docs_ipgei.TYPE_RELEVE_ANNUEL,
        }
        deja = document_deja_delivre(
            etudiant, types_ipgei[genre], inscription.annee_universitaire,
            semestre.code if semestre is not None else '')
        if deja is not None:
            with deja.fichier_pdf.open('rb') as f:
                pdf = f.read()
            doc = deja
        elif genre == 'attestation':
            doc, pdf = docs_ipgei.generer_attestation_inscription(
                inscription, utilisateur=request.user, base_url=base_url)
        elif genre == 'releve_annuel':
            doc, pdf = docs_ipgei.generer_releve_annuel(
                inscription, utilisateur=request.user, base_url=base_url)
        else:
            doc, pdf = docs_ipgei.generer_releve_semestre(
                inscription, semestre, utilisateur=request.user, base_url=base_url)

        reponse = HttpResponse(pdf, content_type='application/pdf')
        # `inline` et non `attachment` : les gestionnaires de téléchargement
        # s'accrochent au second et tuent la requête de la page. Cf.
        # `core/download_middleware.py`.
        reponse['Content-Disposition'] = f'inline; filename="{doc.numero_serie}.pdf"'
        reponse['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return reponse


class MesDocumentsView(APIView):
    """GET /api/v1/portail/documents/"""
    permission_classes = [IsEtudiant]

    def get(self, request):
        try:
            from apps.documents.models import DocumentOfficiel
            from apps.documents.serializers import DocumentOfficielSerializer

            etudiant  = _get_etudiant(request)
            documents = DocumentOfficiel.objects.filter(etudiant=etudiant).order_by('-date_creation')
            serializer = DocumentOfficielSerializer(documents, many=True, context={'request': request})
            return Response(serializer.data)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class TelechargerDocumentView(APIView):
    """GET /api/v1/portail/documents/<id>/telecharger/"""
    permission_classes = [IsEtudiant]

    def get(self, request, pk):
        try:
            from apps.documents.models import DocumentOfficiel
            from django.http import FileResponse
            import os

            etudiant = _get_etudiant(request)
            try:
                doc = DocumentOfficiel.objects.get(pk=pk, etudiant=etudiant)
            except DocumentOfficiel.DoesNotExist:
                return Response({'detail': 'Document introuvable.'}, status=status.HTTP_404_NOT_FOUND)

            if not doc.fichier_pdf or not os.path.exists(doc.fichier_pdf.path):
                return Response({'detail': 'Fichier non disponible.'}, status=status.HTTP_404_NOT_FOUND)

            response = FileResponse(open(doc.fichier_pdf.path, 'rb'), content_type='application/pdf')
            response['Content-Disposition'] = f'attachment; filename="{os.path.basename(doc.fichier_pdf.name)}"'
            response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response['Pragma'] = 'no-cache'
            return response
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ── Semaines (lecture seule pour étudiants) ───────────────────────────────────
class SemaniesEtudiantView(APIView):
    """GET /api/v1/portail/semaines/ — liste des semaines accessibles à l'étudiant."""
    permission_classes = [IsEtudiant]

    def get(self, request):
        from apps.parametres.models import Semaine
        from apps.parametres.serializers import SemaineSerializer

        etudiant = _get_etudiant(request)
        qs = Semaine.objects.all().order_by('numero_semaine', 'date').distinct()

        # Filtrer par année universitaire de l'inscription active
        try:
            from apps.inscriptions.models import InscriptionAdministrative
            annee_univ = InscriptionAdministrative.objects.filter(
                etudiant=etudiant,
            ).order_by('-annee_univ__annee').values_list(
                'annee_univ__annee', flat=True
            ).first()
            if annee_univ:
                qs = qs.filter(annee_universitaire=annee_univ)
        except Exception:
            pass

        # Dédupliquer par numero_semaine (garder la première date de chaque semaine)
        seen = set()
        unique = []
        for s in qs:
            if s.numero_semaine not in seen:
                seen.add(s.numero_semaine)
                unique.append(s)

        serializer = SemaineSerializer(unique, many=True, context={'request': request})
        return Response(serializer.data)


# ── Réclamations ──────────────────────────────────────────────────────────────
class MesReclamationsView(APIView):
    """GET / POST /api/v1/portail/reclamations/"""
    permission_classes = [IsEtudiant]
    parser_classes     = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        etudiant     = _get_etudiant(request)
        reclamations = Reclamation.objects.filter(etudiant=etudiant).order_by('-date_soumission')
        serializer   = ReclamationSerializer(reclamations, many=True, context={'request': request})
        return Response(serializer.data)

    def post(self, request):
        etudiant   = _get_etudiant(request)
        serializer = ReclamationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # La note contestée. Le champ visait `InscriptionElement`, de la couche
        # LMD à zéro ligne : aucun étudiant ne pouvait désigner sa note, et
        # cette garde rendait 403 à tout le monde — elle s'appuyait de surcroît
        # sur `Year.est_active`, qu'aucune année ne porte en base.
        from .ipgei_source import peut_reclamer

        note = serializer.validated_data.get('note_ipgei')
        if note is not None:
            if note.inscription.etudiant_id != etudiant.id:
                return Response(
                    {'detail': "Cette note ne vous appartient pas."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            # La période fait foi, et elle seule : elle porte déjà l'année, la
            # parité, l'institution, la filière et le niveau. Le contrôle
            # d'année séparé disait la même chose une seconde fois, et les deux
            # ne pouvaient que diverger.
            if not peut_reclamer(note):
                return Response(
                    {'detail': "Aucune période de réclamation n'est ouverte pour "
                               'ces notes en ce moment. Contactez la scolarité.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        reclamation = serializer.save(etudiant=etudiant)
        return Response(ReclamationSerializer(reclamation).data,
                        status=status.HTTP_201_CREATED)


class PeriodesReclamationActivesView(APIView):
    """
    `GET /api/v1/portail/reclamations/periodes-actives/`

    Les fenêtres de réclamation ouvertes pour l'étudiant connecté. Elle
    déduisait son périmètre d'`InscriptionAdministrative` — couche LMD, zéro
    ligne —, si bien qu'elle rendait toujours une liste vide : l'écran affichait
    « aucune période ouverte » même une période ouverte.
    """
    permission_classes = [IsEtudiant]

    def get(self, request):
        from apps.reclamations.serializers import PeriodeReclamationSerializer

        from .ipgei_source import periodes_ouvertes

        etudiant = _get_etudiant(request)
        periodes = periodes_ouvertes(etudiant)
        return Response(PeriodeReclamationSerializer(periodes, many=True).data)


class DetailReclamationView(generics.RetrieveAPIView):
    """GET /api/v1/portail/reclamations/<id>/"""
    serializer_class   = ReclamationSerializer
    permission_classes = [IsEtudiant]

    def get_object(self):
        etudiant = _get_etudiant(self.request)
        try:
            return Reclamation.objects.get(pk=self.kwargs['pk'], etudiant=etudiant)
        except Reclamation.DoesNotExist:
            from rest_framework.exceptions import NotFound
            raise NotFound('Réclamation introuvable.')
