"""
Portail étudiant — vues.
Toutes les vues exigent le rôle 'etudiant' via IsEtudiant.
L'étudiant ne voit que ses propres données (filtrées par request.user.etudiant_profile).
"""
import threading as _threading

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
_gen_lock: _threading.Lock = _threading.Lock()
_gen_in_progress: set = set()   # doc PKs en cours de génération


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
        from apps.inscriptions.models import InscriptionPedagogique

        etudiant = _get_etudiant(request)

        # ── Déterminer annee_universitaire + semestre_id depuis l'inscription péda ──
        ip = InscriptionPedagogique.objects.filter(
            inscription_admin__etudiant=etudiant,
        ).select_related('semestre', 'inscription_admin__annee_univ').order_by(
            '-inscription_admin__annee_univ__annee'
        ).first()

        if not ip:
            return Response({'creneaux': [], 'grille': {}, 'annee': '', 'semestre': ''})

        annee_univ  = ip.inscription_admin.annee_univ.annee
        semestre_id = ip.semestre_id
        dept_id     = etudiant.departement_id

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

        semestre_nom = ip.semestre.semestre if ip.semestre else ''

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
            '-suivi__numero_semaine', 'suivi__jour_fk__jour'
        )
        serializer = AbsenceEtudiantSerializer(qs, many=True)
        return Response(serializer.data)


# ── Notes ─────────────────────────────────────────────────────────────────────
class MesNotesView(APIView):
    """GET /api/v1/portail/notes/"""
    permission_classes = [IsEtudiant]

    def get(self, request):
        try:
            from apps.inscriptions.models import InscriptionElement, InscriptionPedagogique
            from .serializers import NoteEtudiantSerializer

            etudiant = _get_etudiant(request)
            inscriptions_ped = InscriptionPedagogique.objects.filter(
                inscription_admin__etudiant=etudiant,
            ).values_list('id', flat=True)

            elements = (
                InscriptionElement.objects
                .select_related(
                    'em',
                    'inscription_ped__semestre',
                    'inscription_ped__inscription_admin__annee_univ',
                )
                .prefetch_related('notes', 'resultats')
                .filter(inscription_ped__in=inscriptions_ped)
            )

            serializer = NoteEtudiantSerializer(elements, many=True)
            return Response(serializer.data)
        except Exception as e:
            import traceback
            return Response({'detail': str(e), 'trace': traceback.format_exc()}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ── Résultats semestriels ─────────────────────────────────────────────────────
class MesResultatsView(APIView):
    """GET /api/v1/portail/resultats/semestres/"""
    permission_classes = [IsEtudiant]

    def get(self, request):
        try:
            from apps.evaluations.models import ResultatSemestre
            from apps.evaluations.serializers import ResultatSemestreSerializer

            etudiant = _get_etudiant(request)
            resultats = ResultatSemestre.objects.select_related(
                'inscription_admin__annee_univ',
                'inscription_admin__filiere',
            ).filter(inscription_admin__etudiant=etudiant)

            serializer = ResultatSemestreSerializer(resultats, many=True, context={'request': request})
            return Response(serializer.data)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ── Documents ─────────────────────────────────────────────────────────────────
def _pregen_pdf_bg(doc_pk, etudiant_pk, annee_univ, semestre_id):
    """
    Génère le PDF d'un DocumentOfficiel en arrière-plan (thread daemon).
    Lance wkhtmltopdf hors du cycle de vie de la requête HTTP.
    Utilise _gen_lock/_gen_in_progress pour éviter les générations en double.
    """
    # ── Exclusion mutuelle : un seul thread par document ─────────────────────
    with _gen_lock:
        if doc_pk in _gen_in_progress:
            return   # déjà en cours → ne pas relancer wkhtmltopdf en parallèle
        _gen_in_progress.add(doc_pk)

    import hashlib, os, logging
    logger = logging.getLogger('siga')
    try:
        from apps.documents.models import DocumentOfficiel
        from apps.documents.services import _generer_pdf
        from apps.evaluations.models import Note
        from apps.inscriptions.models import InscriptionPedagogique
        from apps.absence.models import Etudiant
        from django.core.files.base import ContentFile

        etudiant = Etudiant.objects.get(pk=etudiant_pk)
        doc      = DocumentOfficiel.objects.get(pk=doc_pk)

        # Calcul du hash des notes pour ce semestre
        hash_actuel = ''
        try:
            ip = InscriptionPedagogique.objects.filter(
                inscription_admin__etudiant=etudiant,
                semestre_id=doc.semestre_id or semestre_id,
            ).first()
            if ip:
                notes_qs = Note.objects.filter(
                    inscription_element__inscription_ped=ip,
                ).order_by('inscription_element_id', 'type_note').values_list(
                    'inscription_element_id', 'type_note', 'valeur'
                )
                hash_data = '|'.join(f'{ie},{t},{v}' for ie, t, v in notes_qs)
            else:
                hash_data = ''
            hash_actuel = hashlib.sha256(hash_data.encode()).hexdigest()
        except Exception:
            pass

        besoin_regen = (
            not doc.fichier_pdf or
            not os.path.exists(doc.fichier_pdf.path if doc.fichier_pdf else '') or
            doc.hash_sha256 != hash_actuel
        )
        if not besoin_regen:
            return  # PDF déjà à jour — rien à faire

        # Supprimer l'ancien fichier si présent
        if doc.fichier_pdf:
            try:
                old_path = doc.fichier_pdf.path
                if os.path.exists(old_path):
                    os.remove(old_path)
            except Exception:
                pass
            doc.fichier_pdf = None
            doc.save(update_fields=['fichier_pdf'])

        pdf_bytes = _generer_pdf(doc, etudiant, {
            'annee_universitaire': annee_univ,
            'semestre':            semestre_id,
        })
        if not pdf_bytes:
            logger.warning('_pregen_pdf_bg: _generer_pdf a retourné None pour doc %s', doc_pk)
            return

        doc.hash_sha256 = hash_actuel
        from apps.documents.services import enregistrer_pdf_document
        enregistrer_pdf_document(doc, pdf_bytes)
        doc.save(update_fields=['hash_sha256'])
        logger.info('_pregen_pdf_bg: PDF généré en arrière-plan pour doc %s', doc_pk)

    except Exception as e:
        logger.exception('_pregen_pdf_bg: erreur pour doc %s: %s', doc_pk, e)
    finally:
        with _gen_lock:
            _gen_in_progress.discard(doc_pk)
        # Fermer la connexion DB propre au thread
        try:
            from django.db import connection as _db
            _db.close()
        except Exception:
            pass


def _releve_disponible_pour_etudiant(etudiant, annee_univ_label, semestre_id):
    """
    Verifie qu'un releve est telechargeable par l'etudiant : il faut qu'au moins
    un PVDeliberation cloture (est_clos=True) couvre cet (etudiant, annee, semestre).

    Couverture acceptee :
      - PV semestriel : meme annee + meme code_semestre OU meme parite (I/P)
                        + ligne de deliberation pour cet etudiant
      - PV annuel     : meme annee + ligne de deliberation pour cet etudiant
                        (le PV annuel englobe les 2 semestres de l'annee)

    Retourne True/False. Sans semestre_id : verifie le PV annuel uniquement.
    """
    from apps.evaluations.models import PVDeliberation, LigneDeliberation
    from apps.parametres.models import Semestre

    base_q = LigneDeliberation.objects.filter(
        pv__est_clos=True,
        inscription_admin__etudiant=etudiant,
    )
    if not base_q.exists():
        return False

    if not semestre_id:
        # On regarde uniquement le PV annuel
        return base_q.filter(
            pv__type_pv='annuel',
            pv__annee_univ__annee=annee_univ_label,
        ).exists()

    # Parite du semestre demande (Impairs si S1/S3/S5, Pairs si S2/S4/S6)
    sem = Semestre.objects.filter(pk=semestre_id).first()
    if not sem:
        return False
    parite = sem.type_semestre  # 'I' ou 'P'
    code   = sem.code_semestre  # 'S1', 'S2', etc.

    # PV annuel couvrant l'annee
    if base_q.filter(
        pv__type_pv='annuel',
        pv__annee_univ__annee=annee_univ_label,
    ).exists():
        return True

    # PV semestriel couvrant ce semestre (par code OU parite)
    from django.db.models import Q
    return base_q.filter(
        pv__type_pv='semestriel',
        pv__session__annee_univ__annee=annee_univ_label,
    ).filter(
        Q(pv__semestre_code=code) | Q(pv__session__type_semestre=parite)
    ).exists()


def _find_or_create_doc(etudiant, type_document, annee_univ, semestre_id, user):
    """Trouve ou crée le DocumentOfficiel sans générer le PDF."""
    from apps.documents.models import DocumentOfficiel
    from apps.documents.services import generer_document

    qs = DocumentOfficiel.objects.filter(
        etudiant=etudiant,
        type_document=type_document,
        annee_universitaire=annee_univ,
    )
    if semestre_id:
        qs = qs.filter(semestre_id=semestre_id)
    doc = qs.order_by('-date_generation').first()

    if not doc:
        doc_data = generer_document({
            'etudiant':           etudiant.pk,
            'type_document':      type_document,
            'annee_universitaire': annee_univ,
            **({'semestre': semestre_id} if semestre_id else {}),
        }, user)
        doc = DocumentOfficiel.objects.get(pk=doc_data['id'])

    return doc


class DocumentsDisponiblesView(APIView):
    """GET /api/v1/portail/documents/disponibles/
    Retourne la liste des documents que l'étudiant peut générer/télécharger :
    - une attestation d'inscription (pour l'année active)
    - un relevé de notes par semestre inscrit
    Lance également la pré-génération des PDFs en arrière-plan (threads daemon)
    afin que le téléchargement ultérieur soit instantané.
    """
    permission_classes = [IsEtudiant]

    def get(self, request):
        import threading
        from apps.inscriptions.models import InscriptionAdministrative, InscriptionPedagogique

        etudiant = _get_etudiant(request)
        ia = (
            InscriptionAdministrative.objects
            .filter(etudiant=etudiant)
            .select_related('annee_univ')
            .order_by('-annee_univ__annee')
            .first()
        )
        if not ia:
            return Response({'attestation': None, 'releves': []})

        annee_univ = ia.annee_univ.annee

        attestation = {
            'type_document': 'attestation_inscription',
            'label':         "Attestation d'inscription",
            'annee_univ':    annee_univ,
        }

        # Variante A (stricte) : on parcourt TOUTES les IP (toutes annees)
        # pour exposer aussi les releves historiques deja deliberes.
        # Un releve n'est expose que si la deliberation pour ce
        # (etudiant, annee, semestre) est cloturee.
        ips_all = (
            InscriptionPedagogique.objects
            .filter(inscription_admin__etudiant=etudiant)
            .select_related('semestre', 'inscription_admin__annee_univ')
            .order_by('-inscription_admin__annee_univ__annee', 'semestre__code_semestre')
        )
        releves = []
        for ip in ips_all:
            if not ip.semestre:
                continue
            ip_annee = ip.inscription_admin.annee_univ.annee if ip.inscription_admin.annee_univ_id else None
            if not ip_annee:
                continue
            if not _releve_disponible_pour_etudiant(etudiant, ip_annee, ip.semestre_id):
                continue
            releves.append({
                'type_document':  'releve_semestre',
                'semestre_id':    ip.semestre_id,
                'semestre_code':  ip.semestre.code_semestre,
                'semestre_label': ip.semestre.semestre,
                'annee_univ':     ip_annee,
                'label':          f"Relevé de notes — {ip.semestre.semestre} ({ip_annee})",
            })
        # Pour la pre-generation, on garde aussi la liste des IP de l'annee courante
        ips = ips_all.filter(inscription_admin=ia)

        # ── Pré-génération des PDFs en arrière-plan ───────────────────────────
        # wkhtmltopdf est lancé depuis un thread daemon : sa fermeture n'affecte
        # pas la socket HTTP active → pas de "Failed to fetch" au téléchargement.
        try:
            docs_a_pregen = []

            # Attestation
            try:
                doc_att = _find_or_create_doc(
                    etudiant, 'attestation_inscription', annee_univ, None, request.user
                )
                docs_a_pregen.append((doc_att.pk, None))
            except Exception:
                pass

            # Relevés — uniquement ceux dont la deliberation est cloturee
            for ip in ips:
                if not ip.semestre:
                    continue
                if not _releve_disponible_pour_etudiant(etudiant, annee_univ, ip.semestre_id):
                    continue   # pas de pre-generation tant que pas delibere
                try:
                    doc_rel = _find_or_create_doc(
                        etudiant, 'releve_semestre', annee_univ, ip.semestre_id, request.user
                    )
                    docs_a_pregen.append((doc_rel.pk, ip.semestre_id))
                except Exception:
                    pass

            for doc_pk, sem_id in docs_a_pregen:
                with _gen_lock:
                    if doc_pk not in _gen_in_progress:
                        t = threading.Thread(
                            target=_pregen_pdf_bg,
                            args=(doc_pk, etudiant.pk, annee_univ, sem_id),
                            daemon=True,
                        )
                        t.start()
        except Exception:
            pass  # La pré-génération est best-effort — ne pas bloquer la réponse

        return Response({'attestation': attestation, 'releves': releves})


class TelechargerDirectView(APIView):
    """GET /api/v1/portail/documents/telecharger-direct/?type=...&semestre=...

    Si le PDF est déjà prêt (pré-généré par DocumentsDisponiblesView) → sert
    immédiatement sans lancer wkhtmltopdf.

    Sinon → génère le PDF de façon synchrone dans le thread courant.
    Avant d'appeler pdfkit, _mark_sockets_non_inheritable() (dans services.py)
    marque tous les sockets Python comme non-héritables : wkhtmltopdf ne peut
    donc plus les fermer à sa sortie, ce qui éliminait TypeError: Failed to fetch.
    """
    permission_classes = [IsEtudiant]

    def get(self, request):
        import hashlib, os
        from apps.inscriptions.models import InscriptionAdministrative
        from apps.documents.services import _generer_pdf
        from django.http import HttpResponse
        from django.core.files.base import ContentFile

        type_document = request.query_params.get('type')
        semestre_id   = request.query_params.get('semestre')

        if not type_document:
            return Response({'detail': 'Paramètre type requis.'}, status=status.HTTP_400_BAD_REQUEST)

        etudiant = _get_etudiant(request)

        # Pour un releve, l'annee est deduite du semestre demande (l'etudiant
        # peut telecharger un releve d'une annee anterieure).
        sem_id_int = None
        try:
            sem_id_int = int(semestre_id) if semestre_id else None
        except (TypeError, ValueError):
            sem_id_int = None

        # Pour un releve unifie, on parcourt TOUTES les annees ou l'etudiant
        # est inscrit a ce semestre, et on prend la PLUS RECENTE qui ait un PV
        # cloture. Cela permet de fournir un releve unifie tant qu'une annee
        # source au moins est deliberee (les annees precedentes capitalisees
        # ne ferment pas l'acces a la consolidation).
        annee_univ = None
        annee_disponible = False
        if sem_id_int and type_document in ('releve_semestre', 'releve_complet', 'releve_notes'):
            from apps.inscriptions.models import InscriptionPedagogique
            ips = (
                InscriptionPedagogique.objects
                .filter(inscription_admin__etudiant=etudiant, semestre_id=sem_id_int)
                .select_related('inscription_admin__annee_univ')
                .order_by('-inscription_admin__annee_univ__annee')
            )
            for ip in ips:
                ann_obj = ip.inscription_admin.annee_univ
                if not ann_obj:
                    continue
                if _releve_disponible_pour_etudiant(etudiant, ann_obj.annee, sem_id_int):
                    annee_univ = ann_obj.annee
                    annee_disponible = True
                    break

        if not annee_univ:
            # Fallback : annee la plus recente (utilise pour les types qui ne sont
            # pas un releve, ex: attestation_inscription)
            ia = (
                InscriptionAdministrative.objects
                .filter(etudiant=etudiant)
                .select_related('annee_univ')
                .order_by('-annee_univ__annee')
                .first()
            )
            if not ia:
                return Response({'detail': 'Aucune inscription trouvée.'}, status=status.HTTP_404_NOT_FOUND)
            annee_univ = ia.annee_univ.annee

        # ── Variante A : verrou tant que la deliberation n'est pas cloturee ──
        # Concerne uniquement les releves (l'attestation reste libre).
        if type_document in ('releve_semestre', 'releve_complet', 'releve_notes'):
            if not annee_disponible:
                return Response(
                    {'detail': "Ce relevé n'est pas encore disponible : aucune session délibérée pour ce semestre."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        # ── Trouver ou créer le DocumentOfficiel ─────────────────────────────
        try:
            doc = _find_or_create_doc(etudiant, type_document, annee_univ, semestre_id, request.user)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # ── Hash des notes pour détecter les changements ──────────────────────
        hash_actuel = ''
        try:
            from apps.evaluations.models import Note
            from apps.inscriptions.models import InscriptionPedagogique
            ip = InscriptionPedagogique.objects.filter(
                inscription_admin__etudiant=etudiant,
                semestre_id=doc.semestre_id or semestre_id,
            ).first()
            if ip:
                notes_qs = Note.objects.filter(
                    inscription_element__inscription_ped=ip,
                ).order_by('inscription_element_id', 'type_note').values_list(
                    'inscription_element_id', 'type_note', 'valeur'
                )
                hash_data = '|'.join(f'{ie},{t},{v}' for ie, t, v in notes_qs)
            else:
                hash_data = ''
            hash_actuel = hashlib.sha256(hash_data.encode()).hexdigest()
        except Exception:
            pass

        def _pdf_path():
            try:
                return doc.fichier_pdf.path if doc.fichier_pdf else None
            except ValueError:
                return None

        path = _pdf_path()
        besoin_regen = (
            not path or
            not os.path.exists(path) or
            doc.hash_sha256 != hash_actuel
        )
        # 1re délivrance = original ; impressions suivantes = DUPLICATA (décidé ici,
        # à la délivrance, pas au pré-cache).
        is_duplicata = doc.premiere_generation is not None

        # ── Génération synchrone si nécessaire ───────────────────────────────
        if besoin_regen:
            # Attendre si un thread daemon est déjà en train de générer ce PDF
            # (lancé par DocumentsDisponiblesView) pour éviter deux wkhtmltopdf.
            import time
            wait_start = time.time()
            while doc.pk in _gen_in_progress and (time.time() - wait_start) < 30:
                time.sleep(1)
                doc.refresh_from_db(fields=['fichier_pdf', 'hash_sha256'])
                path = _pdf_path()
                if path and os.path.exists(path):
                    besoin_regen = False
                    break

            if besoin_regen:
                try:
                    if doc.fichier_pdf:
                        try:
                            old = doc.fichier_pdf.path
                            if os.path.exists(old):
                                os.remove(old)
                        except Exception:
                            pass
                        doc.fichier_pdf = None
                        doc.save(update_fields=['fichier_pdf'])

                    # Generation dans un thread daemon ISOLE pour eviter que
                    # wkhtmltopdf herite des handles de la socket HTTP courante
                    # (Windows : sa sortie tuait le socket -> "Failed to fetch").
                    import threading
                    result = {'pdf': None, 'error': None}

                    def _gen():
                        try:
                            result['pdf'] = _generer_pdf(doc, etudiant, {
                                'annee_universitaire': annee_univ,
                                'semestre':            semestre_id,
                            }, is_duplicata=is_duplicata)
                        except Exception as exc:
                            result['error'] = exc

                    t = threading.Thread(target=_gen, daemon=True)
                    t.start()
                    t.join(timeout=60)
                    if t.is_alive():
                        return Response(
                            {'detail': 'Generation PDF trop longue (timeout 60s).'},
                            status=status.HTTP_504_GATEWAY_TIMEOUT,
                        )
                    if result['error']:
                        return Response({'detail': str(result['error'])}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                    pdf_bytes = result['pdf']
                    if not pdf_bytes:
                        return Response(
                            {'detail': 'Erreur de génération PDF.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        )
                    doc.hash_sha256 = hash_actuel
                    from apps.documents.services import enregistrer_pdf_document
                    enregistrer_pdf_document(doc, pdf_bytes)
                    doc.save(update_fields=['hash_sha256'])
                except Exception as e:
                    return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # ── Servir le PDF ────────────────────────────────────────────────────
        try:
            filename = f'{doc.numero_serie}.pdf'
            with doc.fichier_pdf.open('rb') as f:
                pdf_content = f.read()
        except Exception as e:
            return Response({'detail': f'Fichier illisible : {e}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # 1re délivrance : marquer le document délivré ET vider le cache → la
        # prochaine impression régénérera un DUPLICATA (cf. action telecharger).
        if doc.premiere_generation is None:
            from django.utils import timezone
            doc.premiere_generation = timezone.now()
            try:
                doc.fichier_pdf.delete(save=False)
            except Exception:
                pass
            doc.fichier_pdf = None
            doc.save(update_fields=['premiere_generation', 'fichier_pdf'])

        response = HttpResponse(pdf_content, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        response['Content-Length'] = len(pdf_content)
        response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response['Pragma'] = 'no-cache'
        return response


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

        # Garde-fou : reclamation possible uniquement sur les notes de l'annee
        # universitaire active. Empeche tout abus si quelqu'un forge la requete.
        ie = serializer.validated_data.get('inscription_element')
        if ie is not None:
            from apps.parametres.models import Year
            try:
                ie_annee = ie.inscription_ped.inscription_admin.annee_univ
            except Exception:
                ie_annee = None
            annee_active = Year.objects.filter(est_active=True).first()
            if not ie_annee or not annee_active or ie_annee.pk != annee_active.pk:
                return Response(
                    {'detail': "Vous ne pouvez réclamer que sur les notes de l'année universitaire en cours."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            # Verifier aussi que l'IE appartient bien a cet etudiant
            if ie.inscription_ped.inscription_admin.etudiant_id != etudiant.id:
                return Response(
                    {'detail': "Cette note ne vous appartient pas."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            # Verifier qu'une periode de reclamation est ouverte pour cette note
            if not _periode_reclamation_active(etudiant, ie):
                return Response(
                    {'detail': "Aucune periode de reclamation n'est ouverte pour ces notes en ce moment. Contactez la scolarite."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        reclamation = serializer.save(etudiant=etudiant)
        return Response(ReclamationSerializer(reclamation).data, status=status.HTTP_201_CREATED)


def _periode_reclamation_active(etudiant, inscription_element):
    """
    Retourne True si au moins une PeriodeReclamation est en cours pour
    l'etudiant + l'inscription_element donnee. Le scope verifie :
      - annee_univ matche
      - type_semestre (parite) matche
      - filiere matche (ou periode.filiere=null = toutes)
      - niveau matche (ou periode.niveau=null = tous)
      - periode est actif=True ET dans la fenetre [date_ouverture, date_fermeture]
      - periode.institution matche celle de l'IA
    """
    from apps.reclamations.models import PeriodeReclamation
    from django.utils import timezone
    from django.db.models import Q

    try:
        ia      = inscription_element.inscription_ped.inscription_admin
        ip      = inscription_element.inscription_ped
        annee   = ia.annee_univ
        niveau  = ia.niveau
        institution = ia.institution
        filiere = ia.filiere_id
        sem     = ip.semestre
    except Exception:
        return False

    if not annee or not sem:
        return False

    parite = sem.type_semestre  # 'I' ou 'P'
    now = timezone.now()

    return PeriodeReclamation.objects.filter(
        actif=True,
        date_ouverture__lte=now,
        date_fermeture__gte=now,
        annee_univ=annee,
        type_semestre=parite,
        institution=institution,
    ).filter(
        Q(filiere__isnull=True) | Q(filiere_id=filiere)
    ).filter(
        Q(niveau__isnull=True) | Q(niveau=niveau)
    ).exists()


class PeriodesReclamationActivesView(APIView):
    """
    GET /api/v1/portail/reclamations/periodes-actives/
    Retourne la liste des periodes de reclamation actuellement ouvertes
    pour l'etudiant connecte (selon son institution + filiere + niveau + annee).
    """
    permission_classes = [IsEtudiant]

    def get(self, request):
        from apps.reclamations.models import PeriodeReclamation
        from apps.reclamations.serializers import PeriodeReclamationSerializer
        from django.utils import timezone
        from django.db.models import Q
        from apps.inscriptions.models import InscriptionAdministrative

        etudiant = _get_etudiant(request)
        # On regarde les IA pour cet etudiant pour deduire son scope (institution, filiere, niveau)
        ias = InscriptionAdministrative.objects.filter(etudiant=etudiant).select_related(
            'annee_univ', 'filiere', 'institution',
        )
        if not ias.exists():
            return Response([])

        now = timezone.now()
        # Toutes les periodes potentiellement actives
        periodes_qs = PeriodeReclamation.objects.filter(
            actif=True,
            date_ouverture__lte=now,
            date_fermeture__gte=now,
        ).select_related('annee_univ', 'institution', 'filiere')

        # On ne garde que celles qui matchent au moins une IA de l'etudiant
        active = []
        for periode in periodes_qs:
            for ia in ias:
                if ia.annee_univ_id != periode.annee_univ_id:
                    continue
                if ia.institution_id != periode.institution_id:
                    continue
                if periode.filiere_id and ia.filiere_id != periode.filiere_id:
                    continue
                if periode.niveau is not None and ia.niveau != periode.niveau:
                    continue
                active.append(periode)
                break
        return Response(PeriodeReclamationSerializer(active, many=True).data)


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
