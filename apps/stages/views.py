from decimal import Decimal, ROUND_HALF_UP
from django.utils import timezone
from rest_framework import viewsets, status, views
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter

from core.permissions import RBACPermission
from .models import ConventionStage, EvaluationStage, DerogationMedicale
from .serializers import (
    ConventionStageSerializer, EvaluationStageSerializer, DerogationMedicaleSerializer,
)
from core.telechargement import entete_piece_jointe


class ConventionStageViewSet(viewsets.ModelViewSet):
    queryset = ConventionStage.objects.select_related('etudiant', 'tuteur_academique').all()
    serializer_class   = ConventionStageSerializer
    permission_classes = [RBACPermission]
    required_module    = 'stage_convention'
    parser_classes     = [MultiPartParser, FormParser, JSONParser]
    filter_backends    = [DjangoFilterBackend, SearchFilter]
    filterset_fields   = ['statut', 'est_pfe', 'etudiant']
    search_fields      = ['sujet', 'entreprise_nom', 'etudiant__nom', 'etudiant__matricule']

    @staticmethod
    def _evaluation_a_des_notes(ev) -> bool:
        """True si l'évaluation de stage porte des données saisies (notes, jury,
        soutenance, validation PFE)."""
        if ev is None:
            return False
        return any([
            ev.note_entreprise is not None,
            ev.note_rapport is not None,
            ev.note_soutenance is not None,
            ev.note_finale is not None,
            ev.est_valide_pfe,
            ev.date_soutenance is not None,
            ev.jury.exists(),
        ])

    def destroy(self, request, *args, **kwargs):
        """
        Garde-fou : EvaluationStage.convention est en CASCADE. Supprimer une
        convention détruirait son évaluation (notes entreprise/rapport/soutenance,
        note finale, jury, validation PFE — Art. 16 ingénieur). On bloque si
        l'évaluation porte des notes, sauf confirmation explicite ?force=1.
        """
        instance = self.get_object()
        ev = getattr(instance, 'evaluation', None)
        force = request.query_params.get('force') in ('1', 'true', 'True')
        if self._evaluation_a_des_notes(ev) and not force:
            return Response(
                {
                    'status': 409,
                    'error': (
                        "Cette convention de stage a une évaluation avec des notes "
                        "saisies (rapport / soutenance / PFE). La supprimer détruirait "
                        "ces données. Relancez avec ?force=1 pour confirmer."
                    ),
                },
                status=status.HTTP_409_CONFLICT,
            )
        return super().destroy(request, *args, **kwargs)


class EvaluationStageViewSet(viewsets.ModelViewSet):
    queryset = EvaluationStage.objects.select_related('convention').prefetch_related('jury').all()
    serializer_class   = EvaluationStageSerializer
    permission_classes = [RBACPermission]
    required_module    = 'stage_evaluation'
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['convention']


def _calculer_classement(data):
    """
    Logique de calcul du classement, partagee entre l'endpoint JSON et l'endpoint
    Excel. Retourne un tuple (status, payload).
      - status = 200 -> payload = dict resultat
      - status != 200 -> payload = dict {'detail': '...'}
    """
    from apps.inscriptions.models import InscriptionAdministrative, InscriptionPedagogique
    from apps.parametres.models import Year, Semestre
    from apps.evaluations.services.calcul_notes import NoteCalculService

    filiere_id    = data.get('filiere')
    niveau        = data.get('niveau_cible')
    annee_label   = (data.get('annee_univ') or '').strip()
    semestres_ids = data.get('semestres') or []
    type_stage    = data.get('type_stage') or ''

    if not filiere_id or not niveau or not annee_label or not semestres_ids:
        return 400, {'detail': 'Parametres requis: filiere, niveau_cible, annee_univ, semestres (liste non vide).'}
    try:
        niveau = int(niveau)
        filiere_id = int(filiere_id)
        semestres_ids = [int(s) for s in semestres_ids]
    except (TypeError, ValueError):
        return 400, {'detail': 'filiere, niveau_cible, semestres doivent etre des entiers.'}

    try:
        annee = Year.objects.get(annee=annee_label)
    except Year.DoesNotExist:
        return 404, {'detail': f'Annee {annee_label} introuvable.'}

    semestres = list(Semestre.objects.filter(pk__in=semestres_ids).order_by('code_semestre'))
    if not semestres:
        return 400, {'detail': 'Aucun semestre valide trouve.'}

    ias = (
        InscriptionAdministrative.objects
        .filter(filiere_id=filiere_id, niveau=niveau, annee_univ=annee)
        .select_related('etudiant', 'institution')
    )

    items = []
    for ia in ias:
        etu = ia.etudiant
        inst = ia.institution
        ips = list(InscriptionPedagogique.objects.filter(
            inscription_admin__etudiant=etu,
            inscription_admin__institution=inst,
            semestre_id__in=semestres_ids,
        ).select_related('semestre', 'inscription_admin__annee_univ'))

        somme_moyennes = Decimal('0')
        nb_semestres_avec_moyenne = 0
        credits_valides_total = 0
        details_par_semestre = []
        tous_valides = True
        tous_dispos = True

        # Pour chaque semestre demande, on regroupe TOUTES les IPs de l'etudiant
        # pour ce semestre (peut etre 2+ si redoublement / dette repete).
        ips_par_sem = {}
        for ip in ips:
            ips_par_sem.setdefault(ip.semestre_id, []).append(ip)

        # Strategie : pour chaque IP, calculer le RS consolide (SR>SN). Puis,
        # parmi toutes les IPs, prendre celle qui a la MEILLEURE moyenne (ignorer
        # les IPs vides : RS sans moyenne, ou sessions non cloturees a 0).
        # Cela evite le piege du "redoublement avec semestre encore en cours" qui
        # remplacerait une vraie note de l'annee precedente par une moyenne 0.
        sem_meilleur_rs = {}
        for sem in semestres:
            candidats = []
            for ip in ips_par_sem.get(sem.id, []):
                ip_annee = ip.inscription_admin.annee_univ
                type_sem = NoteCalculService._parite_semestre(ip.semestre)
                rs = NoteCalculService._selectionner_rs_consolide(
                    insc_ped=ip, annee_univ=ip_annee, institution=inst, type_semestre=type_sem,
                )
                if rs is not None and rs.moyenne is not None:
                    candidats.append((rs.moyenne, rs, ip))
            if candidats:
                # Trie sur la moyenne decroissante - prendre la meilleure
                candidats.sort(key=lambda t: t[0], reverse=True)
                _, best_rs, best_ip = candidats[0]
                sem_meilleur_rs[sem.id] = (best_rs, best_ip)

        for sem in semestres:
            entry = sem_meilleur_rs.get(sem.id)
            if entry is None:
                tous_dispos = False
                tous_valides = False
                details_par_semestre.append({
                    'semestre': sem.code_semestre, 'moyenne': None,
                    'credits_valides': 0, 'est_admis': False,
                })
                continue
            rs, ip = entry
            somme_moyennes += rs.moyenne
            nb_semestres_avec_moyenne += 1
            credits_valides_total += rs.credits_valides
            if not rs.est_admis:
                tous_valides = False
            details_par_semestre.append({
                'semestre': sem.code_semestre, 'moyenne': str(rs.moyenne),
                'credits_valides': rs.credits_valides, 'est_admis': rs.est_admis,
            })

        sem_manquants = [s.code_semestre for s in semestres if s.id not in sem_meilleur_rs]
        if nb_semestres_avec_moyenne == 0:
            moyenne = None
        else:
            moyenne = (somme_moyennes / Decimal(nb_semestres_avec_moyenne)).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP,
            )

        items.append({
            'etudiant_id':            etu.id,
            'matricule':              etu.matricule,
            'nom':                    etu.nom_fr or etu.nom,
            'prenom':                 etu.prenom_fr or '',
            'genre':                  etu.genre,
            'moyenne':                str(moyenne) if moyenne is not None else None,
            'credits_valides':        credits_valides_total,
            'tous_semestres_valides': tous_valides,
            'donnees_completes':      tous_dispos and len(sem_manquants) == 0,
            'semestres_manquants':    sorted(sem_manquants) if sem_manquants else [],
            'details':                details_par_semestre,
        })

    items.sort(key=lambda x: (x['moyenne'] is None, -float(x['moyenne']) if x['moyenne'] else 0))

    rang = 0
    prev_moy = None
    for i, it in enumerate(items):
        if it['moyenne'] is None:
            it['rang'] = None
            continue
        current = float(it['moyenne'])
        if prev_moy is None or current != prev_moy:
            rang = i + 1
        it['rang'] = rang
        prev_moy = current

    from apps.scolarite.models import Filiere
    filiere_obj = Filiere.objects.filter(pk=filiere_id).first()

    return 200, {
        'filiere':          filiere_obj.intitule_fr if filiere_obj else '',
        'filiere_code':     filiere_obj.code if filiere_obj else '',
        'niveau':           niveau,
        'annee_univ':       annee_label,
        'type_stage':       type_stage,
        'semestres_inclus': [s.code_semestre for s in semestres],
        'total_etudiants':  len(items),
        'items':            items,
    }


class ClassementStageView(views.APIView):
    """
    POST /api/v1/stages/classement/

    Calcule un classement flexible des etudiants pour l'attribution des stages.
    Le classement est base sur la MOYENNE ARITHMETIQUE SIMPLE des moyennes des
    semestres choisis (chaque semestre compte pour 1, peu importe ses credits).
    Ex: stage L2 = (S1 + S2 + S3) / 3 ; PFE = (S1 + S2 + S3 + S4 + S5) / 5.

    Pour chaque etudiant inscrit (filiere + niveau_cible) sur l'annee donnee :
      - Pour chaque semestre demande, on prend le ResultatSemestre consolide
        (rattrapage cloturee si dispo, sinon normale)
      - Moyenne globale = somme(moyennes) / nb_semestres_avec_moyenne
      - Tri decroissant par moyenne, rang attribue dynamiquement

    Body :
    {
        "filiere":       <int>            // FK Filiere (obligatoire)
        "niveau_cible": <int>             // niveau de l'inscription (1, 2, 3...)
        "annee_univ":   "<2024-2025>"     // libelle annee source des notes
        "semestres":    [<id>, <id>, ...] // ids des Semestre a inclure (>=1)
        "type_stage":   "L2" | "PFE"      // libre, juste pour le label retour
    }
    """
    permission_classes = [RBACPermission]
    required_module    = 'stage_classement'

    def post(self, request):
        code, payload = _calculer_classement(request.data or {})
        return Response(payload, status=code)


class ClassementStageExcelView(views.APIView):
    """
    POST /api/v1/stages/classement/excel/
    Memes parametres que ClassementStageView mais retourne un classeur Excel
    style (couleurs PRIMARY #006633, Recap + Detail) au format de progression.
    """
    permission_classes = [RBACPermission]
    required_module    = 'stage_classement'

    def post(self, request):
        code, payload = _calculer_classement(request.data or {})
        if code != 200:
            return Response(payload, status=code)

        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        except ImportError:
            return Response({'detail': 'openpyxl non installe.'}, status=500)
        from io import BytesIO
        from django.http import HttpResponse

        # Style aligne sur le PV de progression
        PRIMARY = '006633'
        ACCENT  = 'E5C018'
        thin = Side(border_style='thin', color='CCCCCC')
        border_all = Border(left=thin, right=thin, top=thin, bottom=thin)
        header_font = Font(name='Arial', size=11, bold=True, color='FFFFFF')
        header_fill = PatternFill('solid', fgColor=PRIMARY)
        header_align = Alignment(horizontal='center', vertical='center', wrap_text=True)
        title_font = Font(name='Arial', size=14, bold=True, color=PRIMARY)
        sub_font   = Font(name='Arial', size=10, italic=True, color='555555')
        center = Alignment(horizontal='center', vertical='center')
        left   = Alignment(horizontal='left',   vertical='center')
        cell_v   = PatternFill('solid', fgColor='DCFCE7')   # vert clair
        cell_nv  = PatternFill('solid', fgColor='FEE2E2')   # rouge clair
        cell_inc = PatternFill('solid', fgColor='FEF3C7')   # ambre
        cell_top = PatternFill('solid', fgColor='FFF7E0')   # or pour top 3
        cell_mod = PatternFill('solid', fgColor='E8F5E9')

        wb = Workbook()

        # ════════════════════════════════════════════════════════════════════
        # Feuille 1 — Récapitulatif
        # ════════════════════════════════════════════════════════════════════
        ws = wb.active
        ws.title = 'Récapitulatif'

        items = payload['items']
        sems_inclus = payload['semestres_inclus']
        nb_cols_total = 6 + len(sems_inclus)  # rang, mat, nom, genre, moy, crd, [details], statut

        # Titre
        last_col = chr(ord('A') + nb_cols_total - 1)
        ws.merge_cells(f'A1:{last_col}1')
        c = ws['A1']
        type_stage_lbl = f' — {payload["type_stage"]}' if payload['type_stage'] else ''
        c.value = f'Classement pour attribution stage{type_stage_lbl} — {payload["filiere_code"]} L{payload["niveau"]}'
        c.font = title_font
        c.alignment = center
        ws.row_dimensions[1].height = 22

        ws.merge_cells(f'A2:{last_col}2')
        c = ws['A2']
        c.value = (
            f'Filière : {payload["filiere"]}'
            f'  |  Niveau : L{payload["niveau"]}'
            f'  |  Année : {payload["annee_univ"]}'
            f'  |  Semestres : {", ".join(sems_inclus)}'
            f'  |  Méthode : moyenne arithmétique simple'
        )
        c.font = sub_font
        c.alignment = center

        # Stats
        nb_complets = sum(1 for it in items if it['donnees_completes'])
        nb_partiels = sum(1 for it in items if not it['donnees_completes'] and it['moyenne'] is not None)
        nb_sans     = sum(1 for it in items if it['moyenne'] is None)
        nb_admis_tous = sum(1 for it in items if it['tous_semestres_valides'])

        ws['A4'] = 'Total étudiants';   ws['B4'] = payload['total_etudiants']
        ws['A5'] = 'Données complètes'; ws['B5'] = nb_complets
        ws['A6'] = 'Données partielles'; ws['B6'] = nb_partiels
        ws['A7'] = 'Sans moyenne';      ws['B7'] = nb_sans
        ws['A8'] = 'Tous sem. validés'; ws['B8'] = nb_admis_tous
        for r in range(4, 9):
            ws.cell(r, 1).font = Font(bold=True, color=PRIMARY)
            ws.cell(r, 2).alignment = center

        # Tableau classement : rang, matricule, nom, genre, moyenne, credits,
        # [details S1, S2, ...], statut
        start_row = 10
        headers = ['Rang', 'Matricule', 'Nom & Prénom', 'Genre', 'Moyenne', 'Crédits validés']
        for sem in sems_inclus:
            headers.append(f'Note {sem}')
        headers.append('Statut')

        for col, h in enumerate(headers, 1):
            cell = ws.cell(start_row, col, h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            cell.border = border_all

        # Dictionnaire pour retrouver moyenne par semestre rapidement
        for i, it in enumerate(items, start=1):
            r = start_row + i
            details_map = {d['semestre']: d for d in it['details']}

            # Rang
            rang_cell = ws.cell(r, 1, it['rang'] if it['rang'] is not None else '—')
            rang_cell.alignment = center
            rang_cell.border = border_all
            if it['rang'] in (1, 2, 3):
                rang_cell.fill = cell_top
                rang_cell.font = Font(bold=True, color=PRIMARY)

            # Matricule
            mat = ws.cell(r, 2, it['matricule'])
            mat.alignment = center
            mat.border = border_all
            mat.font = Font(name='Consolas', size=10)

            # Nom Prenom
            nom_cell = ws.cell(r, 3, f'{it["nom"]} {it["prenom"]}'.strip())
            nom_cell.alignment = left
            nom_cell.border = border_all

            # Genre
            g = ws.cell(r, 4, it['genre'])
            g.alignment = center
            g.border = border_all

            # Moyenne
            moy_cell = ws.cell(r, 5, float(it['moyenne']) if it['moyenne'] else None)
            moy_cell.alignment = center
            moy_cell.border = border_all
            moy_cell.number_format = '0.00'
            if it['moyenne'] is not None:
                if float(it['moyenne']) >= 12:
                    moy_cell.fill = cell_v
                elif float(it['moyenne']) >= 10:
                    moy_cell.fill = cell_inc
                else:
                    moy_cell.fill = cell_nv
                moy_cell.font = Font(bold=True)

            # Credits valides
            cr = ws.cell(r, 6, it['credits_valides'])
            cr.alignment = center
            cr.border = border_all

            # Details par semestre
            for j, sem in enumerate(sems_inclus):
                det = details_map.get(sem)
                col_idx = 6 + j + 1
                if det and det['moyenne'] is not None:
                    val = float(det['moyenne'])
                    cell = ws.cell(r, col_idx, val)
                    cell.number_format = '0.00'
                    if det['est_admis']:
                        cell.fill = cell_v
                    else:
                        cell.fill = cell_inc
                else:
                    cell = ws.cell(r, col_idx, '—')
                    cell.fill = cell_nv
                cell.alignment = center
                cell.border = border_all

            # Statut
            statut_col = 6 + len(sems_inclus) + 1
            if not it['donnees_completes']:
                statut_label = f'Incomplet ({", ".join(it["semestres_manquants"])})' if it['semestres_manquants'] else 'Incomplet'
                statut_fill = cell_nv
            elif it['tous_semestres_valides']:
                statut_label = 'Validé'
                statut_fill = cell_v
            else:
                statut_label = 'Partiel'
                statut_fill = cell_inc
            sc = ws.cell(r, statut_col, statut_label)
            sc.alignment = center
            sc.border = border_all
            sc.fill = statut_fill

        # Largeurs colonnes
        ws.column_dimensions['A'].width = 8
        ws.column_dimensions['B'].width = 12
        ws.column_dimensions['C'].width = 32
        ws.column_dimensions['D'].width = 8
        ws.column_dimensions['E'].width = 12
        ws.column_dimensions['F'].width = 12
        for j in range(len(sems_inclus)):
            ws.column_dimensions[chr(ord('G') + j)].width = 12
        ws.column_dimensions[chr(ord('G') + len(sems_inclus))].width = 22

        # Freeze panes : tete de tableau
        ws.freeze_panes = ws.cell(start_row + 1, 1)

        # Pied : date + signature
        end_row = start_row + len(items) + 3
        ws.cell(end_row, 1, f'Édité le : {timezone.now().strftime("%d/%m/%Y %H:%M")}').font = sub_font

        # ════════════════════════════════════════════════════════════════════
        # Sauvegarde
        # ════════════════════════════════════════════════════════════════════
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)

        filename = f'classement-{payload["filiere_code"]}-{payload["annee_univ"]}-L{payload["niveau"]}.xlsx'
        response = HttpResponse(
            buf.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = entete_piece_jointe(filename)
        return response


class DerogationMedicaleViewSet(viewsets.ModelViewSet):
    queryset = DerogationMedicale.objects.select_related('etudiant').all()
    serializer_class   = DerogationMedicaleSerializer
    permission_classes = [RBACPermission]
    required_module    = 'stage_derogation'
    parser_classes     = [MultiPartParser, FormParser, JSONParser]
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ['statut', 'etudiant']

    @action(detail=True, methods=['post'], url_path='approuver')
    def approuver(self, request, pk=None):
        d = self.get_object()
        if d.statut != 'soumise':
            return Response({'detail': 'Déjà traitée.'}, status=status.HTTP_400_BAD_REQUEST)
        d.statut = 'approuvee'
        d.save(update_fields=['statut'])
        return Response(DerogationMedicaleSerializer(d).data)

    @action(detail=True, methods=['post'], url_path='refuser')
    def refuser(self, request, pk=None):
        d = self.get_object()
        if d.statut != 'soumise':
            return Response({'detail': 'Déjà traitée.'}, status=status.HTTP_400_BAD_REQUEST)
        motif = request.data.get('motif', '')
        if not motif:
            return Response({'motif': ['Requis.']}, status=status.HTTP_400_BAD_REQUEST)
        d.statut = 'refusee'
        d.decision_motif = motif
        d.save(update_fields=['statut', 'decision_motif'])
        return Response(DerogationMedicaleSerializer(d).data)
