"""Export du PV de délibération (Excel + PDF).

Builders purs extraits de PVDeliberationViewSet : prennent un PVDeliberation
et retournent une HttpResponse (xlsx / pdf). La vue ne fait plus que déléguer ici.
"""
import logging
from datetime import date

from django.http import HttpResponse
from django.template.loader import get_template
from rest_framework import status
from rest_framework.response import Response

from apps.evaluations.models import ParametreJury
from apps.evaluations.services.deliberation_semestre import DeliberationSemestreService

logger = logging.getLogger('siga')

# Préfixe de niveau selon le type de diplôme : Licence → L1/L2, Ingénieur → E1/E2,
# Doctorat → D1/D2, Master → M1… (au lieu du générique N1).
NIVEAU_PREFIX = {
    'LP': 'L', 'LF': 'L', 'LIC': 'L', 'M': 'M', 'MAS': 'M',
    'ING': 'E', 'Doctorat': 'D', 'DOC': 'D', 'DUT': 'D', 'BTS': 'B',
}


def niveau_label(pv) -> str:
    """Label de niveau préfixé par le type de diplôme : 'L1', 'E2', 'D1'…"""
    type_diplome = pv.filiere.type_diplome if pv.filiere else 'LP'
    return f"{NIVEAU_PREFIX.get(type_diplome, 'L')}{pv.niveau}"


def annee_pv(pv) -> str:
    """Année universitaire effective : directe (PV annuel) ou via la session (PV semestriel)."""
    if getattr(pv, 'annee_univ_id', None):
        return pv.annee_univ.annee
    if getattr(pv, 'session_id', None) and pv.session and pv.session.annee_univ_id:
        return pv.session.annee_univ.annee
    return ''


def est_annee_diplome_pv(pv) -> bool:
    """Fin de cycle : PV annuel au dernier niveau du cursus, hors tronc commun
    (filière sans filles). MÊME règle que PVDeliberationSerializer.get_est_annee_diplome
    et le blocage diplôme. En fin de cycle, un « passage » vaut obtention du diplôme."""
    fil = pv.filiere
    if not fil or pv.type_pv != 'annuel':
        return False
    niveau_fin = fil.niveau_fin or 3
    return pv.niveau == niveau_fin and not fil.filieres_filles.exists()


def verrou_motif_pv(pv) -> str:
    """Motif réglementaire du verrou de passage (même texte que l'UI)."""
    is_ing = bool(pv.filiere and getattr(pv.filiere, 'type_diplome', 'LP') == 'ING')
    return ('S1+S2 non entièrement validés' if is_ing
            else 'L1 non entièrement validée')


# Couleur texte par « kind » de décision (suffixe de classe CSS badge côté PDF).
_DEC_COLOR = {
    'diplome': '#6b21a8', 'admis': '#15803d', 'redoub': '#92400e',
    'exclus': '#7f1d1d', 'blanche': '#0c4a6e', 'vide': '#475569',
}


def decision_annuelle_rendu(decision_annuelle, est_diplome, genre='', verrou=False, motif=''):
    """Rendu d'affichage de la décision annuelle pour le PV (PDF + Excel).

    Réplique badgeDecisionAnnuelle du front :
      - En FIN DE CYCLE (est_diplome) + décision de passage → « Diplômé(e) »
        (accord de genre) au lieu de « Passage de droit/cond ».
      - En cas de VERROU de passage (décision forcée à redoublement, Art. 20 al. 2
        LP / Art. 25 ING), on joint le MOTIF réglementaire de non-passage.

    Retourne {label, kind, color, note} où kind = suffixe de classe CSS badge."""
    fem = (genre or '').upper().startswith('F')
    if est_diplome and decision_annuelle in ('passage_droit', 'passage_cond'):
        return {'label': 'Diplômée ✓' if fem else 'Diplômé ✓',
                'kind': 'diplome', 'color': _DEC_COLOR['diplome'], 'note': ''}
    base = {
        'passage_droit': ('Passage de droit ✓', 'admis'),
        'passage_cond':  ('Passage cond. ✓',    'admis'),
        'redoublement':  ('Redoublement',       'redoub'),
        'exclusion':     ('Exclusion',          'exclus'),
        'annee_blanche': ('Année blanche',      'blanche'),
    }
    label, kind = base.get(decision_annuelle, ('Non délibéré', 'vide'))
    note = motif if (verrou and decision_annuelle == 'redoublement') else ''
    return {'label': label, 'kind': kind, 'color': _DEC_COLOR[kind], 'note': note}


def build_pv_excel(pv):
    """
    GET /api/v1/evaluations/pvs/{id}/excel/
    Génère le PV en classeur Excel (3 feuilles : Récapitulatif,
    Détail par étudiant, Matrice rattrapages).
    Style aligné sur le template PDF (couleur primaire #006633).
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        return Response({'detail': 'openpyxl non installé.'}, status=500)
    from io import BytesIO

    is_sem = pv.type_pv == 'semestriel'

    # Derive semestre_code correct depuis (niveau, session.type_semestre).
    # Le champ pv.semestre_code stocke peut etre mal renseigne (ex 'S1' partout).
    if is_sem and pv.session and pv.session.type_semestre:
        num = pv.niveau * 2 - (1 if pv.session.type_semestre == 'Impairs' else 0)
        pv.semestre_code = f'S{num}'

    # ── Charger les donnees enrichies via le helper unifie ────────────────
    # me_sn / me_sr / me_retenue (Art. 18) deja calcules par enrichir_lignes_pv
    from apps.evaluations.services.pv_enrichment import enrichir_lignes_pv
    from apps.evaluations.models import ObligationRattrapage

    lignes = pv.lignes.select_related(
        'inscription_admin__etudiant', 'inscription_admin',
    ).all()
    lignes_enrichies, enrich_meta = enrichir_lignes_pv(pv)
    has_sr = enrich_meta.get('has_sr_global', False)

    # Fin de cycle (→ « Diplômé(e) ») + motif de verrou (→ non-passage) : calculés
    # une fois pour tout le PV, appliqués au récap et au détail (même règle que l'UI).
    est_dip      = est_annee_diplome_pv(pv)
    motif_verrou = verrou_motif_pv(pv)

    # ── Styles communs ────────────────────────────────────────────────────
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
    cell_o   = PatternFill('solid', fgColor='FECACA')
    cell_f   = PatternFill('solid', fgColor='FEF3C7')
    cell_mod = PatternFill('solid', fgColor='E8F5E9')
    cell_em  = PatternFill('solid', fgColor='FAFFFE')

    wb = Workbook()

    # ════════════════════════════════════════════════════════════════════
    # Feuille 1 — Récapitulatif
    # ════════════════════════════════════════════════════════════════════
    ws = wb.active
    ws.title = 'Récapitulatif'

    # Titre
    ws.merge_cells('A1:F1')
    c = ws['A1']
    sem_suffix = f' — {pv.semestre_code}' if (is_sem and pv.semestre_code) else ''
    c.value = f'PV N° {pv.id} — {pv.filiere.code if pv.filiere else "?"} {niveau_label(pv)}{sem_suffix}'
    c.font = title_font
    c.alignment = center
    ws.row_dimensions[1].height = 22

    ws.merge_cells('A2:F2')
    c = ws['A2']
    if is_sem:
        sem_part = f'  |  Semestre : {pv.semestre_code}' if pv.semestre_code else ''
        ses_label = ('Session rattrapage'
                     if (pv.session and pv.session.type_session == 'rattrapage')
                     else 'Session normale')
        info = (
            f'Filière : {pv.filiere.intitule_fr if pv.filiere else "?"}'
            f'  |  Niveau : {niveau_label(pv)}'
            f'{sem_part}'
            f'  |  {ses_label}'
        )
    else:
        info = (
            f'Filière : {pv.filiere.intitule_fr if pv.filiere else "?"}'
            f'  |  Niveau : {niveau_label(pv)}'
            f'  |  Année : {pv.annee_univ.annee if pv.annee_univ else "?"}'
        )
    c.value = info
    c.font = sub_font
    c.alignment = center

    # Stats
    nb_admis = sum(1 for li in lignes if li.decision in ('admis', 'rachat')
                   or li.decision_annuelle in ('passage_droit', 'passage_cond'))
    nb_ajourn = sum(1 for li in lignes if li.decision == 'ajourned'
                    or li.decision_annuelle == 'redoublement')
    nb_excl  = sum(1 for li in lignes if li.decision == 'exclus'
                   or li.decision_annuelle == 'exclusion')

    ws['A4'] = 'Total étudiants';   ws['B4'] = lignes.count()
    ws['A5'] = 'Validés';            ws['B5'] = nb_admis
    ws['A6'] = 'Non validés';        ws['B6'] = nb_ajourn
    ws['A7'] = 'Exclus';             ws['B7'] = nb_excl
    for r in range(4, 8):
        ws.cell(r, 1).font = Font(bold=True, color=PRIMARY)
        ws.cell(r, 2).alignment = center

    # Tableau récap : matricule, nom, moy, crd, [taux], décision
    start_row = 9
    if is_sem:
        headers = ['Matricule', 'Nom & Prénom', 'Moyenne', 'Crédits', 'Décision']
    else:
        headers = ['Matricule', 'Nom & Prénom', 'Moyenne', 'Crédits', 'Taux %', 'Décision']

    for col, h in enumerate(headers, 1):
        cell = ws.cell(start_row, col, h)
        cell.font = header_font; cell.fill = header_fill
        cell.alignment = header_align; cell.border = border_all

    # Tri par matricule
    def _mat_key(li):
        m = (li.inscription_admin.etudiant.matricule or '')
        try: return (0, int(m))
        except (ValueError, TypeError): return (1, m)
    lignes_sorted = sorted(lignes, key=_mat_key)

    for i, li in enumerate(lignes_sorted, start=1):
        r = start_row + i
        etu = li.inscription_admin.etudiant
        ws.cell(r, 1, etu.matricule).font = Font(name='Consolas', size=10)
        ws.cell(r, 2, etu.nom_fr or etu.nom or '').alignment = left
        ws.cell(r, 3, float(li.moyenne_annuelle) if li.moyenne_annuelle else None).alignment = center
        ws.cell(r, 4, li.credits_annuels or 0).alignment = center
        col_dec = 5
        if not is_sem:
            taux = float(li.taux_capitalisation) if li.taux_capitalisation else None
            ws.cell(r, 5, taux).alignment = center
            col_dec = 6

        # Décision
        dec_note = ''
        if is_sem:
            # PV semestriel : V / V-rachat / NV
            if li.decision == 'rachat':
                dec_label, fill = 'V-rachat', cell_v
            elif li.decision == 'admis':
                dec_label, fill = 'V', cell_v
            else:
                dec_label, fill = 'NV', cell_nv
        else:
            # PV annuel : fin de cycle → « Diplômé(e) » ; verrou → motif de non-passage.
            rendu = decision_annuelle_rendu(
                li.decision_annuelle, est_dip, getattr(etu, 'genre', ''),
                li.verrou_passage, motif_verrou,
            )
            dec_label = rendu['label']
            dec_note  = rendu['note']
            fill = {'diplome': cell_v, 'admis': cell_v, 'redoub': cell_f,
                    'exclus': cell_nv, 'blanche': cell_em}.get(rendu['kind'])
            if dec_note:
                dec_label = f"{dec_label}\n⚠ {dec_note}"

        cell_dec = ws.cell(r, col_dec, dec_label)
        cell_dec.alignment = (Alignment(horizontal='center', vertical='center', wrap_text=True)
                              if dec_note else center)
        cell_dec.font = Font(bold=True)
        if fill: cell_dec.fill = fill

        for col in range(1, col_dec + 1):
            ws.cell(r, col).border = border_all

    # Largeurs colonnes
    ws.column_dimensions['A'].width = 14
    ws.column_dimensions['B'].width = 35
    ws.column_dimensions['C'].width = 11
    ws.column_dimensions['D'].width = 10
    if is_sem:
        ws.column_dimensions['E'].width = 25
    else:
        ws.column_dimensions['E'].width = 10
        ws.column_dimensions['F'].width = 25

    ws.freeze_panes = ws.cell(start_row + 1, 1)

    # ════════════════════════════════════════════════════════════════════
    # Feuille 2 — Détail par étudiant (modules + EM)
    # ════════════════════════════════════════════════════════════════════
    ws2 = wb.create_sheet('Détail par étudiant')

    # PV annuel              : 11 cols (CC TP EXAM RAT ME Coef Credit Statut)
    # PV semestriel/rattrapage:  9 cols (CC TP Exam RAT Credit Statut)
    # PV semestriel/normale   :  8 cols (CC TP Exam Credit Statut) — pas de RAT,
    #                                    aucune note de rattrapage saisie a ce stade
    is_normale_sem = is_sem and pv.session and pv.session.type_session == 'normale'
    if not is_sem:
        nb_cols = 11
        col_cc, col_tp, col_exam = 4, 5, 6
        col_rat = 7
        col_me  = 8
        col_coef, col_cred, col_statut = 9, 10, 11
        det_headers = ['Matricule', 'Étudiant', 'Module / Élément',
                       'CC', 'TP', 'EXAM', 'RAT', 'ME', 'Coef', 'Credit', 'Statut']
    elif is_normale_sem:
        # Session normale : pas de colonne RAT (les rattrapages n'existent
        # pas encore — ils seront saisis lors de la session SR suivante).
        nb_cols = 10
        col_cc, col_tp, col_exam = 4, 5, 6
        col_rat = None
        col_me   = 7
        col_coef = 8
        col_cred = 9
        col_statut = 10
        col_sn   = None
        col_sr   = None
        col_ret  = None
        det_headers = ['Matricule', 'Étudiant', 'Module / Élément',
                       'CC', 'TP', 'Exam', 'ME', 'Coef', 'Credit', 'Statut']
    else:
        # Session rattrapage : RAT contient les notes du rattrapage
        nb_cols = 11
        col_cc, col_tp, col_exam = 4, 5, 6
        col_rat = 7
        col_me   = 8
        col_coef = 9
        col_cred = 10
        col_statut = 11
        col_sn   = None
        col_sr   = None
        col_ret  = None
        det_headers = ['Matricule', 'Étudiant', 'Module / Élément',
                       'CC', 'TP', 'Exam', 'RAT', 'ME', 'Coef', 'Credit', 'Statut']
    last_col_letter = chr(ord('A') + nb_cols - 1)

    ws2.merge_cells(start_row=1, start_column=1, end_row=1, end_column=nb_cols)
    c = ws2['A1']
    # Titre : « Détail des résultats — STAT - S2 (2024-2025) - Session normale/rattrapage »
    # pour un PV semestriel ; « — STAT - Annuel (2024-2025) » pour un PV annuel.
    annee_titre = annee_pv(pv)
    annee_part  = f' ({annee_titre})' if annee_titre else ''
    fil_code    = pv.filiere.code if pv.filiere else ''
    fil_part    = f'{fil_code} - ' if fil_code else ''
    if is_sem:
        ses_label  = ('Session rattrapage'
                      if (pv.session and pv.session.type_session == 'rattrapage')
                      else 'Session normale')
        periode    = pv.semestre_code or 'Semestre'
        c.value = f'Détail des résultats — {fil_part}{periode}{annee_part} - {ses_label}'
    else:
        c.value = f'Détail des résultats — {fil_part}Annuel{annee_part}'
    c.font = title_font; c.alignment = center
    ws2.row_dimensions[1].height = 22

    for col, h in enumerate(det_headers, 1):
        cell = ws2.cell(3, col, h)
        cell.font = header_font; cell.fill = header_fill
        cell.alignment = header_align; cell.border = border_all

    # Libellé décision annuelle : via decision_annuelle_rendu (fin de cycle →
    # « Diplômé(e) », verrou → motif de non-passage). Styles fond/texte par kind.
    _DEC_BG_FG = {
        'diplome': ('F3E8FF', '6B21A8'),
        'admis':   ('DCFCE7', '15803D'),
        'redoub':  ('FEF3C7', '92400E'),
        'exclus':  ('FEE2E2', '7F1D1D'),
        'blanche': ('E0F2FE', '0C4A6E'),
        'vide':    ('F1F5F9', '475569'),
    }

    r = 4
    prev_sem = None
    for item in sorted(lignes_enrichies, key=lambda x: _mat_key(x['ligne'])):
        li = item['ligne']
        etu = li.inscription_admin.etudiant
        prev_sem = None  # reset par etudiant

        # PV annuel : ligne titre etudiant + decision annuelle juste apres son nom
        if not is_sem:
            ws2.merge_cells(start_row=r, start_column=1, end_row=r, end_column=nb_cols)
            rendu = decision_annuelle_rendu(
                li.decision_annuelle, est_dip, getattr(etu, 'genre', ''),
                li.verrou_passage, motif_verrou,
            )
            bg, fg = _DEC_BG_FG.get(rendu['kind'], _DEC_BG_FG['vide'])
            titre_etu = (
                f'  {etu.matricule}  —  {etu.nom_fr or etu.nom or ""} '
                f'{etu.prenom_fr or ""}'.rstrip()
                + f'   |   Décision : {rendu["label"]}'
                + (f'   ⚠ {rendu["note"]}' if rendu['note'] else '')
            )
            cell = ws2.cell(r, 1, titre_etu)
            cell.font = Font(name='Arial', size=11, bold=True, color=fg)
            cell.fill = PatternFill('solid', fgColor=bg)
            cell.alignment = Alignment(horizontal='left', vertical='center', indent=0)
            ws2.row_dimensions[r].height = 20
            r += 1

        for mod in item['modules']:
            # Separateur de semestre (PV annuel uniquement) — affiche le code semestre reel (S1, S2, S3...)
            if not is_sem and mod.get('semestre_code') and mod['semestre_code'] != prev_sem:
                label_sem = f'Semestre {mod["semestre_code"]}'
                ws2.merge_cells(start_row=r, start_column=1, end_row=r, end_column=nb_cols)
                cell = ws2.cell(r, 1, label_sem)
                cell.font = Font(name='Arial', size=10, bold=True, color='FFFFFF')
                cell.fill = PatternFill('solid', fgColor=PRIMARY)
                cell.alignment = Alignment(horizontal='center', vertical='center')
                ws2.row_dimensions[r].height = 16
                r += 1
                prev_sem = mod['semestre_code']

            # Ligne module
            ws2.cell(r, 1, etu.matricule).font = Font(name='Consolas', size=10)
            ws2.cell(r, 2, etu.nom_fr or etu.nom or '')
            ws2.cell(r, 3, f'{mod["module"].code} — {mod["module"].intitule_fr}').font = Font(bold=True, color=PRIMARY)
            # ME (moyenne module) + Coef + Credit — colonnes présentes en PV annuel
            # ET semestriel (col_me / col_coef non nuls désormais aussi en semestriel).
            if col_me is not None:
                ws2.cell(r, col_me, float(mod['moyenne']) if mod['moyenne'] else None).font = Font(bold=True)
                ws2.cell(r, col_me).alignment = center
            if col_coef is not None:
                ws2.cell(r, col_coef, float(mod['module'].coefficient) if mod['module'].coefficient else None).alignment = center
            if col_cred is not None:
                ws2.cell(r, col_cred, mod['credits'] or 0).alignment = center
                ws2.cell(r, col_cred).font = Font(bold=True)
            ws2.cell(r, col_statut, mod['code_statut'] or '—').alignment = center
            for col in range(1, nb_cols + 1):
                cell = ws2.cell(r, col)
                cell.fill = cell_mod
                cell.border = border_all
            ws2.cell(r, col_statut).font = Font(bold=True,
                color='15803D' if mod['code_statut'] == 'V' else 'DC2626')
            r += 1
            # Lignes elements
            for e in mod['elements']:
                ws2.cell(r, 3, f'    {e["code"]} — {e["intitule"]}')
                ws2.cell(r, col_cc, float(e['cc'])   if e['cc']   is not None else None).alignment = center
                ws2.cell(r, col_tp, float(e['tp'])   if e['tp']   is not None else None).alignment = center
                ws2.cell(r, col_exam, float(e['exam']) if e['exam'] is not None else None).alignment = center
                # RAT : note brute exam_rat. Omise pour PV semestriel session normale
                # (pas de rattrapage saisi a ce stade -> colonne masquee).
                if col_rat is not None:
                    ws2.cell(r, col_rat, float(e['exam_rat']) if e['exam_rat'] is not None else None).alignment = center

                # ME (retenue) + Coef + Credit — annuel ET semestriel (colonnes présentes).
                if col_me is not None:
                    me_ret = float(e['me_retenue']) if e['me_retenue'] is not None else None
                    cell_me = ws2.cell(r, col_me, me_ret)
                    cell_me.alignment = center
                    if e['source_retenue'] == 'SR':
                        cell_me.font = Font(bold=True, color='1E40AF')
                        cell_me.fill = PatternFill('solid', fgColor='DBEAFE')
                    else:
                        cell_me.font = Font(bold=True)
                if col_coef is not None:
                    ws2.cell(r, col_coef, float(e['coeff']) if e['coeff'] else None).alignment = center
                if col_cred is not None:
                    em_cred = e['em_credits'] if (e['est_valide'] and e['em_credits']) else None
                    ws2.cell(r, col_cred, em_cred).alignment = center
                    if em_cred:
                        ws2.cell(r, col_cred).font = Font(bold=True)

                ws2.cell(r, col_statut, e['code_statut'] or '—').alignment = center
                color_map = {'V':'15803D','VCI':'16A34A','VCS':'D97706','NV':'EA580C','NVO':'C2410C','E':'7F1D1D','R':'1E40AF'}
                if e['code_statut'] in color_map:
                    ws2.cell(r, col_statut).font = Font(bold=True, color=color_map[e['code_statut']])
                for col in range(1, nb_cols + 1):
                    # Skip fill : cellule ME avec SR retenue (annuel ou semestriel rattrapage)
                    skip_fill = (col_me is not None and col == col_me and e['source_retenue'] == 'SR')
                    if not skip_fill:
                        ws2.cell(r, col).fill = cell_em
                    ws2.cell(r, col).border = border_all
                r += 1

    ws2.column_dimensions['A'].width = 12
    ws2.column_dimensions['B'].width = 25
    ws2.column_dimensions['C'].width = 45
    for col_letter in ['D', 'E', 'F', 'G', 'H', 'I', 'J', 'K']:
        if col_letter <= last_col_letter:
            ws2.column_dimensions[col_letter].width = 10
    ws2.freeze_panes = 'D4'  # freeze matricule + etudiant + module (au-dessus / a gauche)

    # ════════════════════════════════════════════════════════════════════
    # Feuille 3 — Matrice rattrapages (PV semestriel SESSION NORMALE uniquement)
    # Pour un PV de session de rattrapage, pas de matrice (les obligations
    # avaient ete generees apres la session normale precedente).
    # ════════════════════════════════════════════════════════════════════
    if is_sem and pv.session and pv.session.type_session == 'normale':
        # Auto-generation des obligations si absentes (meme logique que le PDF)
        if not pv.est_clos and not ObligationRattrapage.objects.filter(ligne__pv=pv).exists():
            try:
                DeliberationSemestreService(pv).generer_obligations()
            except Exception as exc:
                logger.warning('Excel PV %s: auto-generer_obligations a echoue: %s', pv.id, exc)

        ws3 = wb.create_sheet('Matrice rattrapages')

        obligations_qs = ObligationRattrapage.objects.filter(
            ligne__pv=pv,
        ).select_related(
            'ligne__inscription_admin__etudiant',
            'inscription_element__em',
        )

        # Construire le pivot
        from collections import OrderedDict
        em_codes = []
        seen_em = set()
        for o in obligations_qs:
            code = getattr(o.inscription_element.em, 'code_em', None) or '?'
            if code not in seen_em:
                seen_em.add(code); em_codes.append(code)
        students_map = OrderedDict()
        for o in obligations_qs:
            etu = o.ligne.inscription_admin.etudiant
            key = etu.pk
            if key not in students_map:
                students_map[key] = {'etu': etu, 'cells': {}}
            code = getattr(o.inscription_element.em, 'code_em', None) or '?'
            val = 'O' if o.type_obligation == 'obligatoire' else 'F'
            if students_map[key]['cells'].get(code) != 'O':
                students_map[key]['cells'][code] = val

        students_sorted = sorted(students_map.values(), key=lambda s: (
            (0, int(s['etu'].matricule)) if (s['etu'].matricule or '').isdigit()
            else (1, s['etu'].matricule or '')
        ))

        ws3.merge_cells(start_row=1, start_column=1, end_row=1, end_column=2 + len(em_codes))
        c = ws3['A1']
        ses_label   = ('Session rattrapage'
                       if (pv.session and pv.session.type_session == 'rattrapage')
                       else 'Session normale')
        annee_mat   = annee_pv(pv)
        annee_mat_p = f' ({annee_mat})' if annee_mat else ''
        periode_mat = pv.semestre_code or 'Semestre'
        c.value = f'Matrice des rattrapages — {periode_mat}{annee_mat_p} - {ses_label}'
        c.font = title_font; c.alignment = center
        ws3.row_dimensions[1].height = 22

        ws3.merge_cells(start_row=2, start_column=1, end_row=2, end_column=2 + len(em_codes))
        c = ws3['A2']
        c.value = 'O = Obligatoire (éliminatoire ou module < 8)  |  F = Facultatif (module 8 à 10)  |  — = Non concerné'
        c.font = sub_font; c.alignment = center

        # En-têtes
        ws3.cell(4, 1, 'Matricule').font = header_font
        ws3.cell(4, 2, 'Nom & Prénom').font = header_font
        for col, code in enumerate(em_codes, start=3):
            cell = ws3.cell(4, col, code)
            cell.font = header_font; cell.fill = header_fill
            cell.alignment = header_align; cell.border = border_all
        ws3.cell(4, 1).fill = header_fill; ws3.cell(4, 1).alignment = header_align; ws3.cell(4, 1).border = border_all
        ws3.cell(4, 2).fill = header_fill; ws3.cell(4, 2).alignment = header_align; ws3.cell(4, 2).border = border_all
        ws3.row_dimensions[4].height = 22

        for i, s in enumerate(students_sorted, start=5):
            ws3.cell(i, 1, s['etu'].matricule).font = Font(name='Consolas', size=10)
            ws3.cell(i, 1).border = border_all
            ws3.cell(i, 2, s['etu'].nom_fr or s['etu'].nom or '').border = border_all
            for col, code in enumerate(em_codes, start=3):
                val = s['cells'].get(code)
                cell = ws3.cell(i, col, val or '—')
                cell.alignment = center
                cell.border = border_all
                if val == 'O':
                    cell.fill = cell_o
                    cell.font = Font(bold=True, color='DC2626', size=12)
                elif val == 'F':
                    cell.fill = cell_f
                    cell.font = Font(bold=True, color='D97706', size=12)
                else:
                    cell.font = Font(color='AAAAAA')

        ws3.column_dimensions['A'].width = 12
        ws3.column_dimensions['B'].width = 30
        for col_idx, _ in enumerate(em_codes, start=3):
            ws3.column_dimensions[chr(ord('A') + col_idx - 1)].width = 8
        ws3.freeze_panes = ws3.cell(5, 3)

    # ── Sortie ─────────────────────────────────────────────────────────
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)

    filiere_code  = (pv.filiere.code if pv.filiere else None) or str(pv.filiere_id or 'X')
    periode       = (pv.semestre_code or 'Semestre') if is_sem else 'Annuel'
    parts         = ['PV', filiere_code, niveau_label(pv), periode, annee_pv(pv)]
    filename      = '_'.join(p for p in parts if p) + '.xlsx'

    response = HttpResponse(
        buf.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def build_pv_pdf(pv):
    """
    GET /api/v1/evaluations/pvs/{id}/pdf/
    Génère le PV en PDF via pdfkit/wkhtmltopdf.
    """
    try:
        import pdfkit
    except ImportError:
        return Response({'detail': 'pdfkit non installé.'}, status=500)

    lignes = pv.lignes.select_related(
        'inscription_admin__etudiant',
        'inscription_admin',
    ).prefetch_related(
        'obligations_rattrapage__inscription_element__em',
    ).all()

    membres = pv.membres_jury.select_related('user').order_by('role')
    filled  = membres.count()
    # Minimum 3 lignes de signature vides si moins de 3 membres ont signé
    empty_slots = range(max(0, 3 - filled))

    obligations = []
    obligations_pivot = None  # {'em_codes': [...], 'rows': [{'etudiant': ..., 'cells': {code: 'O'/'F'/None}}]}
    if pv.type_pv == 'semestriel':
        from apps.evaluations.models import ObligationRattrapage
        qs_obl = ObligationRattrapage.objects.filter(ligne__pv=pv)

        # Auto-generation si PV semestriel session NORMALE et obligations absentes
        # → assure que la matrice de rattrapage du PDF est toujours presente
        if (not pv.est_clos
                and pv.session
                and pv.session.type_session == 'normale'
                and not qs_obl.exists()):
            try:
                DeliberationSemestreService(pv).generer_obligations()
            except Exception as exc:
                logger.warning('PDF PV %s: auto-generer_obligations a echoue: %s', pv.id, exc)

        obligations = ObligationRattrapage.objects.filter(
            ligne__pv=pv,
        ).select_related(
            'ligne__inscription_admin__etudiant',
            'inscription_element__em',
        ).order_by(
            'ligne__inscription_admin__etudiant__nom',
            'type_obligation',
        )

        # Tableau croisé : lignes = étudiants, colonnes = codes EM
        em_codes_ordered = []
        seen_em = set()
        for o in obligations:
            code = getattr(o.inscription_element.em, 'code_em', None) or '?'
            if code not in seen_em:
                seen_em.add(code)
                em_codes_ordered.append(code)

        # Regrouper par étudiant
        from collections import OrderedDict
        students_map = OrderedDict()
        for o in obligations:
            etudiant = o.ligne.inscription_admin.etudiant
            key = etudiant.pk
            if key not in students_map:
                students_map[key] = {'etudiant': etudiant, 'cells': {}}
            code = getattr(o.inscription_element.em, 'code_em', None) or '?'
            val = 'O' if o.type_obligation == 'obligatoire' else 'F'
            # obligatoire prend la priorité sur facultatif
            if students_map[key]['cells'].get(code) != 'O':
                students_map[key]['cells'][code] = val

        pivot_rows = []
        for entry in students_map.values():
            pivot_rows.append({
                'etudiant':   entry['etudiant'],
                'cells_list': [entry['cells'].get(code) for code in em_codes_ordered],
            })

        # Tri par matricule croissant pour la matrice
        def _matricule_key(row):
            m = (row['etudiant'].matricule or '')
            # Tri numérique si matricule purement numérique, sinon alphabétique
            try:
                return (0, int(m))
            except (ValueError, TypeError):
                return (1, m)
        pivot_rows.sort(key=_matricule_key)

        obligations_pivot = {
            'em_codes': em_codes_ordered,
            'rows':     pivot_rows,
        }

    params = None
    try:
        params = pv.parametre_jury
    except ParametreJury.DoesNotExist:
        pass

    # ── Enrichissement consolide via helper unifie ───────────────────────
    # Pour chaque EM : me_sn, me_sr, me_retenue (Art. 18), source_retenue
    from apps.evaluations.services.pv_enrichment import enrichir_lignes_pv
    lignes_enrichies, enrich_meta = enrichir_lignes_pv(pv)
    logger.info(
        'PDF PV %s: %d lignes_enrichies (lignes=%d, has_sr=%s)',
        pv.id, len(lignes_enrichies), lignes.count(),
        enrich_meta.get('has_sr_global'),
    )

    # Rendu décision annuelle (fin de cycle → « Diplômé(e) » ; verrou → motif de
    # non-passage), calculé côté Python et attaché à chaque ligne — même approche
    # que l'UI (badgeDecisionAnnuelle). Accessible en template via l.dec_rendu.
    est_dip      = est_annee_diplome_pv(pv)
    motif_verrou = verrou_motif_pv(pv)
    for _item in lignes_enrichies:
        _li  = _item['ligne']
        _etu = _li.inscription_admin.etudiant
        _li.dec_rendu = decision_annuelle_rendu(
            _li.decision_annuelle, est_dip, getattr(_etu, 'genre', ''),
            _li.verrou_passage, motif_verrou,
        )

    # Stats
    decisions = [l.decision_annuelle if pv.type_pv == 'annuel' else l.decision for l in lignes]
    nb_admis         = sum(1 for d in decisions if d in ('admis', 'rachat', 'passage_droit', 'passage_cond'))
    nb_ajournes      = sum(1 for d in decisions if d == 'ajourned')
    nb_passage_cond  = sum(1 for d in decisions if d == 'passage_cond')
    nb_redoublement  = sum(1 for d in decisions if d == 'redoublement')
    nb_exclus        = sum(1 for d in decisions if d in ('exclus', 'exclusion'))

    from core.pdf_utils import get_institution_context
    from apps.parametres.models import Institution
    institution     = Institution.objects.filter(est_principale=True).first()
    _inst_ctx       = get_institution_context()
    institution_logo_url = _inst_ctx['image_url']

    pv.filiere_code  = pv.filiere.code         if pv.filiere  else ''
    pv.filiere_nom   = pv.filiere.intitule_fr  if pv.filiere  else ''
    pv.session_code  = pv.session.code         if pv.session  else ''
    # Type de session (préféré au code dans le PDF) : « Session normale/rattrapage ».
    pv.session_type_label = (
        ('Session rattrapage' if pv.session.type_session == 'rattrapage' else 'Session normale')
        if pv.session else ''
    )
    pv.annee_label   = pv.annee_univ.annee     if pv.annee_univ else ''
    pv.niveau_label  = niveau_label(pv)   # L1/E1/D1 selon le type de diplôme

    # Derive semestre_code correct depuis (niveau, session.type_semestre).
    # Le champ pv.semestre_code stocke est souvent mal renseigne (ex 'S1' partout).
    # Pour PV semestriel : niveau 1 + Impairs → S1 ; 1 + Pairs → S2 ; 2 + Impairs → S3 ; etc.
    if pv.type_pv == 'semestriel' and pv.session and pv.session.type_semestre:
        num = pv.niveau * 2 - (1 if pv.session.type_semestre == 'Impairs' else 0)
        pv.semestre_code = f'S{num}'

    context = {
        **_inst_ctx,
        'pv':               pv,
        'institution':      institution,
        'institution_logo_url': institution_logo_url,
        'lignes':           lignes,
        'lignes_enrichies': lignes_enrichies,
        'enrich_meta':      enrich_meta,  # has_sr_global, has_sr_par_parite, parites_incluses
        'has_sr':           enrich_meta.get('has_sr_global', False),
        'membres':          membres,
        'empty_slots':      empty_slots,
        'obligations':      obligations,
        'obligations_pivot': obligations_pivot,
        'params':           params,
        'date_impression':  date.today().strftime('%d/%m/%Y'),
        'nb_admis':         nb_admis,
        'nb_ajournes':      nb_ajournes,
        'nb_passage_cond':  nb_passage_cond,
        'nb_redoublement':  nb_redoublement,
        'nb_exclus':        nb_exclus,
    }
    html_string = get_template('pv_deliberation.html').render(context)

    config  = pdfkit.configuration(
        wkhtmltopdf=r'C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe'
    )
    # Marges plus serrees pour PV annuel (1 etudiant/page) — sinon defaut paysage
    is_annuel = pv.type_pv == 'annuel'
    options = {
        'orientation':              'Landscape',
        'margin-top':               '0.25in' if is_annuel else '0.35in',
        'margin-right':             '0.30in' if is_annuel else '0.40in',
        'margin-bottom':            '0.35in' if is_annuel else '0.50in',
        'margin-left':              '0.30in' if is_annuel else '0.40in',
        'footer-center':            'Page [page] / [toPage]',
        'enable-local-file-access': '',
        'encoding':                 'UTF-8',
        'no-stop-slow-scripts':     '',
        'javascript-delay':         '1000',
        'load-error-handling':      'ignore',
        'load-media-error-handling':'ignore',
        'disable-smart-shrinking':  '',
    }
    try:
        pdf_bytes = pdfkit.from_string(html_string, False, configuration=config, options=options)
    except Exception as exc:
        logger.error('pdfkit pv error: %s', exc)
        return Response({'detail': f'Erreur génération PDF : {exc}'}, status=500)

    filiere_code = (pv.filiere.code if pv.filiere else None) or str(pv.filiere_id or 'X')
    periode = (pv.semestre_code or 'Semestre') if pv.type_pv == 'semestriel' else 'Annuel'
    parts   = ['PV', filiere_code, niveau_label(pv), periode, annee_pv(pv)]
    filename = '_'.join(p for p in parts if p) + '.pdf'
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def build_pv_rapport_progression(pv):
    """
    GET /api/v1/evaluations/pvs/{id}/rapport-progression/
    Rapport PDF simplifié : matricule · nom · décision (Redouble / Progresse en L2…).
    Uniquement pour les PV annuels.
    """
    try:
        import pdfkit
    except ImportError:
        return Response({'detail': 'pdfkit non installé.'}, status=500)

    if pv.type_pv != 'annuel':
        return Response(
            {'detail': 'Le rapport de progression est réservé aux PV annuels.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Préfixe de niveau selon le type de diplôme
    type_diplome = pv.filiere.type_diplome if pv.filiere else 'LP'
    prefix = NIVEAU_PREFIX.get(type_diplome, 'L')
    niveau_actuel = f'{prefix}{pv.niveau}'
    niveau_suivant = f'{prefix}{pv.niveau + 1}'

    DECISION_LABELS = {
        'passage_droit': f'Progresse en {niveau_suivant}',
        'passage_cond':  f'Progresse en {niveau_suivant} (conditionnel)',
        'redoublement':  f'Redouble en {niveau_actuel}',
        'exclusion':     'Exclu',
        'annee_blanche': 'Année blanche',
    }

    lignes = pv.lignes.select_related(
        'inscription_admin__etudiant',
    ).all()

    def _matricule_key(l):
        m = (l.inscription_admin.etudiant.matricule or '') if l.inscription_admin and l.inscription_admin.etudiant else ''
        try:
            return (0, int(m))
        except (ValueError, TypeError):
            return (1, m)

    lignes_sorted = sorted(lignes, key=_matricule_key)

    # Codes des 2 semestres du niveau (L1 → S1+S2, L2 → S3+S4, L3 → S5+S6)
    sem_impair_code = f'S{pv.niveau * 2 - 1}'
    sem_pair_code   = f'S{pv.niveau * 2}'

    # Pour chaque etudiant, calculer credits par semestre via le RS consolide
    from apps.evaluations.services.calcul_notes import NoteCalculService
    from apps.inscriptions.models import InscriptionPedagogique

    rows = []
    for ligne in lignes_sorted:
        etu = ligne.inscription_admin.etudiant if ligne.inscription_admin else None
        decision = ligne.decision_annuelle or ''
        if etu:
            prenom = getattr(etu, 'prenom_fr', '') or ''
            nom_complet = f'{etu.nom} {prenom}'.strip()
        else:
            nom_complet = '—'

        # Credits par semestre (consolide SR cloturee > SN)
        cred_s_impair = None
        cred_s_pair   = None
        if ligne.inscription_admin:
            ips = InscriptionPedagogique.objects.filter(
                inscription_admin=ligne.inscription_admin,
            ).select_related('semestre')
            for ip in ips:
                type_sem = NoteCalculService._parite_semestre(ip.semestre)
                rs = NoteCalculService._selectionner_rs_consolide(
                    insc_ped=ip,
                    annee_univ=ligne.inscription_admin.annee_univ,
                    institution=ligne.inscription_admin.institution,
                    type_semestre=type_sem,
                )
                if rs:
                    if type_sem == 'Impairs':
                        cred_s_impair = rs.credits_valides
                    elif type_sem == 'Pairs':
                        cred_s_pair = rs.credits_valides

        cred_total = (cred_s_impair or 0) + (cred_s_pair or 0)
        rows.append({
            'matricule':     (etu.matricule if etu else '') or '—',
            'nom':           nom_complet,
            'cred_impair':   cred_s_impair if cred_s_impair is not None else '—',
            'cred_pair':     cred_s_pair   if cred_s_pair   is not None else '—',
            'cred_total':    cred_total,
            'decision':      DECISION_LABELS.get(decision, decision or '—'),
            'decision_code': decision,
        })

    nb_progresse  = sum(1 for r in rows if r['decision_code'] in ('passage_droit', 'passage_cond'))
    nb_redouble   = sum(1 for r in rows if r['decision_code'] == 'redoublement')
    nb_exclu      = sum(1 for r in rows if r['decision_code'] == 'exclusion')
    nb_blanche    = sum(1 for r in rows if r['decision_code'] == 'annee_blanche')

    from core.pdf_utils import get_institution_context
    _inst_ctx = get_institution_context()

    pv.filiere_code = pv.filiere.code        if pv.filiere    else ''
    pv.filiere_nom  = pv.filiere.intitule_fr if pv.filiere    else ''
    pv.annee_label  = pv.annee_univ.annee    if pv.annee_univ else ''

    context = {
        **_inst_ctx,
        'pv':              pv,
        'rows':            rows,
        'niveau_actuel':   niveau_actuel,
        'niveau_suivant':  niveau_suivant,
        'sem_impair_code': sem_impair_code,
        'sem_pair_code':   sem_pair_code,
        'nb_total':        len(rows),
        'nb_progresse':    nb_progresse,
        'nb_redouble':     nb_redouble,
        'nb_exclu':        nb_exclu,
        'nb_blanche':      nb_blanche,
        'date_impression': date.today().strftime('%d/%m/%Y'),
    }
    html_string = get_template('rapport_progression.html').render(context)

    config = pdfkit.configuration(
        wkhtmltopdf=r'C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe'
    )
    options = {
        'orientation':              'Portrait',
        'margin-top':               '0.50in',
        'margin-right':             '0.50in',
        'margin-bottom':            '0.75in',
        'margin-left':              '0.50in',
        'footer-center':            'Page [page] / [toPage]',
        'enable-local-file-access': '',
        'encoding':                 'UTF-8',
        'disable-smart-shrinking':  '',
    }
    try:
        pdf_bytes = pdfkit.from_string(html_string, False, configuration=config, options=options)
    except Exception as exc:
        logger.error('pdfkit rapport-progression error: %s', exc)
        return Response({'detail': f'Erreur génération PDF : {exc}'}, status=500)

    filiere_code = (pv.filiere.code if pv.filiere else None) or str(pv.filiere_id or 'X')
    filename = f'Rapport_progression_{filiere_code}_{niveau_actuel}.pdf'
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response
