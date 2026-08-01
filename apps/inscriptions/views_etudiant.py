"""
Vues API pour le portail étudiant — accès en lecture seule.

Endpoints :
  GET /inscriptions/etudiant/releve/?annee=<id>  → Relevé annuel (semestres/modules/EM)
  GET /inscriptions/etudiant/progression/        → Décision de progression la plus récente
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status


def _get_etudiant_or_403(request):
    """
    Retourne l'Etudiant lié au CustomUser courant ou (None, Response 403).
    """
    try:
        return request.user.etudiant_profile, None
    except Exception:
        return None, Response(
            {'error': "Aucun profil étudiant lié à votre compte."},
            status=status.HTTP_403_FORBIDDEN,
        )


class ReleveAnnuelEtudiantView(APIView):
    """
    GET /inscriptions/etudiant/releve/?annee=<year_id>
    Relevé annuel : semestres → modules → éléments avec moyennes et statuts.
    Visible uniquement après clôture du PV semestriel correspondant.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        etudiant, err = _get_etudiant_or_403(request)
        if err is not None:
            return err

        annee_param = request.query_params.get('annee')
        if not annee_param:
            return Response(
                {'error': 'Le paramètre annee est requis.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.inscriptions.models import (
            InscriptionAdministrative, InscriptionPedagogique, InscriptionElement,
        )
        from apps.evaluations.models import (
            ResultatSemestre, ResultatModule, ResultatElement, PVDeliberation,
        )
        from apps.modules.models import ElementModule

        # Accepter id (int) OU libelle (string ex "2025-2026")
        annee_str = str(annee_param).strip()
        try:
            insc_admin_qs = InscriptionAdministrative.objects.filter(etudiant=etudiant)
            try:
                annee_id_int = int(annee_str)
                insc_admin_qs = insc_admin_qs.filter(annee_univ_id=annee_id_int)
            except ValueError:
                # libelle direct
                insc_admin_qs = insc_admin_qs.filter(annee_univ__annee=annee_str)
            insc_admin = insc_admin_qs.select_related('filiere', 'annee_univ').first()
            if not insc_admin:
                raise InscriptionAdministrative.DoesNotExist
        except InscriptionAdministrative.DoesNotExist:
            return Response(
                {'error': "Aucune inscription pour cette année."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Vérifier qu'au moins un PV semestriel ou annuel est clos pour cette année
        pv_clos = PVDeliberation.objects.filter(
            filiere=insc_admin.filiere,
            niveau=insc_admin.niveau,
            est_clos=True,
        ).exists()

        if not pv_clos:
            return Response({
                'etudiant':  str(etudiant),
                'filiere':   insc_admin.filiere.code,
                'niveau':    insc_admin.niveau,
                'annee':     str(insc_admin.annee_univ),
                'pv_clos':   False,
                'semestres': [],
            })

        # Charger les inscriptions pédagogiques (semestres) avec leurs résultats.
        # On agrege les EMs multi-IP via _consolider_ies_semestre : pour chaque
        # semestre code, on prend les notes de l'année courante + on hérite
        # les EMs validés des années passées (cas dette en passage conditionnel).
        from collections import OrderedDict
        from apps.documents.services import _consolider_ies_semestre, _calc_me, _round_half_up_float

        ips = (
            InscriptionPedagogique.objects
            .filter(inscription_admin=insc_admin)
            .select_related('semestre')
            .order_by('semestre__code_semestre')
        )

        semestres_data = []
        for ip in ips:
            semestre = ip.semestre
            consolides = _consolider_ies_semestre(etudiant, semestre, insc_admin.annee_univ)

            # Regrouper par module
            modules_dict = OrderedDict()
            credits_total = 0
            for cand in consolides:
                em = cand['em']
                module = em.module_lmd
                mod_key   = module.pk   if module else 0
                mod_code  = module.code if module else '—'
                mod_label = module.intitule_fr if module else '—'
                mod_credits = module.credits if module else 0
                if mod_key not in modules_dict:
                    modules_dict[mod_key] = {
                        'code': mod_code, 'intitule': mod_label,
                        'credits': mod_credits, 'elements': [],
                    }
                me = cand['me']
                est_valide       = (me is not None and me >= 10)
                est_eliminatoire = (me is None) or (me < 6)
                if em.credits:
                    credits_total += em.credits
                modules_dict[mod_key]['elements'].append({
                    'code_em':       em.code_em,
                    'intitule':      em.intitule,
                    'note_finale':   str(me) if me is not None else None,
                    'est_valide':    est_valide,
                    'est_eliminatoire': est_eliminatoire,
                    'code_statut':   'V' if est_valide else ('E' if est_eliminatoire else 'NV'),
                    'credits':       em.credits or 0,
                    'coefficient':   em.coefficient or 0,
                    'annee_source':  cand['annee_source'],
                    'est_courante':  cand['est_courante'],
                    'est_dette':     cand['est_dette'],
                })

            # Calcul module moyenne
            modules_data = []
            total_coeff_sem = 0.0
            mg_num = 0.0
            has_elim_global = False
            for mod in modules_dict.values():
                num = den = 0.0
                has_elim_mod = False
                for e in mod['elements']:
                    if e['coefficient']:
                        coef = float(e['coefficient'])
                        me_val = float(e['note_finale']) if e['note_finale'] else 0.0
                        num += me_val * coef
                        den += coef
                        total_coeff_sem += coef
                        mg_num += me_val * coef
                    if e['est_eliminatoire']:
                        has_elim_mod = True
                        has_elim_global = True
                note_mod = _round_half_up_float(num / den, 2) if den else None
                mod_valide = note_mod is not None and note_mod >= 10 and not has_elim_mod
                modules_data.append({
                    'code':        mod['code'],
                    'intitule':    mod['intitule'],
                    'moyenne':     str(note_mod) if note_mod is not None else None,
                    'est_valide':  mod_valide,
                    'has_eliminatoire': has_elim_mod,
                    'code_statut': 'V' if mod_valide else ('E' if has_elim_mod else 'NV'),
                    'credits':     mod['credits'],
                    'elements':    mod['elements'],
                })

            # Moyenne semestre + admis (Art. 14-15)
            mgs = _round_half_up_float(mg_num / total_coeff_sem, 2) if total_coeff_sem > 0 else None
            all_mm_above_8 = all(
                (m['moyenne'] is not None and float(m['moyenne']) >= 8)
                for m in modules_data
            )
            est_admis = (
                mgs is not None and mgs >= 10 and all_mm_above_8 and not has_elim_global
            )

            # Propagation Art. 13/14 (visuelle)
            if est_admis:
                for mod in modules_data:
                    if (not mod['est_valide']
                            and mod['moyenne'] is not None
                            and float(mod['moyenne']) >= 8
                            and not mod['has_eliminatoire']):
                        mod['est_valide'] = True
                        mod['code_statut'] = 'V'
            for mod in modules_data:
                if mod['est_valide']:
                    for e in mod['elements']:
                        if e['est_eliminatoire']:
                            continue
                        if not e['est_valide']:
                            e['est_valide'] = True
                            e['code_statut'] = 'V'

            credits_valides_em = sum(
                e['credits'] for mod in modules_data for e in mod['elements']
                if e['est_valide'] and not e['est_eliminatoire']
            )
            credits_valides = credits_total if est_admis else credits_valides_em

            # Detecter le cas "semestre non encore evalue" : aucune note saisie
            # nulle part (tous les ME sont None). Dans ce cas on neutralise les
            # badges Eliminatoire qui seraient trompeurs (l'etudiant verrait
            # "Eliminatoire" avant meme que ses examens aient lieu).
            tous_em = [e for mod in modules_data for e in mod['elements']]
            est_evalue = any(e['note_finale'] is not None for e in tous_em)
            if not est_evalue:
                # Reset des flags dependants des notes (pas d'affichage E/V/NV
                # tant que rien n'est saisi)
                for mod in modules_data:
                    mod['est_valide']        = False
                    mod['has_eliminatoire']  = False
                    mod['code_statut']       = ''
                    mod['moyenne']           = None
                    for e in mod['elements']:
                        e['est_valide']        = False
                        e['est_eliminatoire']  = False
                        e['code_statut']       = ''
                est_admis = False
                mgs = None
                credits_valides = 0

            # Annees sources distinctes (info pour le frontend : "S1 inclut des EMs de 2024-2025 + 2025-2026")
            annees_sources = sorted({
                e['annee_source'] for mod in modules_data for e in mod['elements']
                if e['annee_source']
            }, reverse=True)

            semestres_data.append({
                'code_semestre':    semestre.code_semestre,
                'moyenne':          str(mgs) if mgs is not None else None,
                'credits_valides':  credits_valides,
                'credits_total':    credits_total,
                'est_admis':        est_admis,
                'est_evalue':       est_evalue,
                'session':          'Relevé unifié',
                'modules':          modules_data,
                'annees_sources':   annees_sources,
            })

        return Response({
            'etudiant':  str(etudiant),
            'filiere':   insc_admin.filiere.code,
            'niveau':    insc_admin.niveau,
            'annee':     str(insc_admin.annee_univ),
            'pv_clos':   True,
            'semestres': semestres_data,
        })


class ProgressionEtudiantView(APIView):
    """
    GET /inscriptions/etudiant/progression/
    Retourne la progression la plus récente de l'étudiant (lecture passive).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        etudiant, err = _get_etudiant_or_403(request)
        if err is not None:
            return err

        from apps.inscriptions.models import Progression
        from decimal import Decimal

        prog = (
            Progression.objects
            .filter(etudiant=etudiant)
            .select_related('filiere_source', 'filiere_cible', 'annee_source', 'annee_cible')
            .order_by('-annee_cible__annee', '-date_creation')
            .first()
        )
        if not prog:
            return Response(
                {'error': 'Aucune progression disponible. La délibération annuelle n\'a pas encore été clôturée.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Détermine le seuil applicable selon le type de diplôme
        type_diplome = prog.filiere_source.type_diplome or 'LP'
        seuil_progression = 75 if type_diplome == 'ING' else 65

        return Response({
            'decision':              prog.decision,
            'decision_label':        prog.get_decision_display(),
            'filiere_source':        {
                'code':        prog.filiere_source.code,
                'intitule_fr': prog.filiere_source.intitule_fr,
            },
            'filiere_cible':         (
                {'code': prog.filiere_cible.code, 'intitule_fr': prog.filiere_cible.intitule_fr}
                if prog.filiere_cible else None
            ),
            'niveau_source':         prog.niveau_source,
            'niveau_cible':          prog.niveau_cible,
            'credits_annuels':       (
                prog.ligne_deliberation.credits_annuels
                if prog.ligne_deliberation else 0
            ),
            'taux_capitalisation':   (
                str(prog.ligne_deliberation.taux_capitalisation)
                if prog.ligne_deliberation and prog.ligne_deliberation.taux_capitalisation is not None
                else None
            ),
            'annee_source':          str(prog.annee_source),
            'annee_cible':           str(prog.annee_cible),
            'seuil_progression':     seuil_progression,
            'type_diplome':          type_diplome,
            'statut':                prog.statut,
        })
