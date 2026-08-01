"""Diagnostic READ-ONLY de la consolidation des sessions d'un PV annuel.

Builder pur extrait de PVDeliberationViewSet.diagnostic_sessions : prend un
PVDeliberation et retourne une Response JSON. Aucune écriture en base.
"""
from rest_framework import status
from rest_framework.response import Response


def _diagnostic_sessions_attendues(pv):
    """Les 4 slots de session (SN/SR × Impairs/Pairs) attendus pour le PV annuel,
    + warnings sur leur état (absente / non clôturée). Retourne (dict, list)."""
    from apps.evaluations.models import SessionEvaluation
    sessions_qs = SessionEvaluation.objects.filter(
        annee_univ=pv.annee_univ, institution=pv.institution,
    )
    by_key = {(s.type_session, s.type_semestre): s for s in sessions_qs}

    sessions_attendues = {}
    warnings = []
    for parite in ('Impairs', 'Pairs'):
        label_p = parite[0]  # 'I' ou 'P'
        sn = by_key.get(('normale',    parite))
        sr = by_key.get(('rattrapage', parite))

        sessions_attendues[f'SN-{label_p}'] = (
            {'id': sn.id, 'code': sn.code, 'est_close': sn.est_close, 'est_ouverte': sn.est_ouverte}
            if sn else None
        )
        sessions_attendues[f'SR-{label_p}'] = (
            {'id': sr.id, 'code': sr.code, 'est_close': sr.est_close, 'est_ouverte': sr.est_ouverte}
            if sr else None
        )

        if sn is None:
            warnings.append(f"Session normale {parite} absente — moyenne annuelle incomplete.")
        elif not sn.est_close:
            warnings.append(f"Session normale {parite} ({sn.code}) non cloturee — moyenne provisoire.")
        if sr and not sr.est_close:
            warnings.append(
                f"Session rattrapage {parite} ({sr.code}) ouverte — "
                f"Art. 18 (max SN/SR) non encore propage."
            )
    return sessions_attendues, warnings


def build_pv_diagnostic(pv):
    """
    GET /api/v1/evaluations/pvs/{id}/diagnostic-sessions/

    Audit READ-ONLY de la consolidation des 4 sessions (SN-I, SR-I, SN-P, SR-P)
    pour un PV annuel. Pour chaque etudiant et chaque parite, retourne :
      - le ResultatSemestre de session normale (si existe)
      - le ResultatSemestre de session rattrapage (si existe)
      - lequel serait retenu par la nouvelle logique de consolidation
      - signale les anomalies (RS_SN.id > RS_SR.id = bug latent)
      - calcule la moyenne annuelle recalculee + diff vs LigneDeliberation.moyenne_annuelle

    Aucune ecriture en base. Permet de verifier un PV avant correctif.
    """
    from decimal import Decimal, ROUND_HALF_UP
    from apps.evaluations.models import ResultatSemestre
    from apps.inscriptions.models import InscriptionPedagogique

    if pv.type_pv != 'annuel':
        return Response(
            {'detail': 'Diagnostic disponible uniquement pour les PV annuels.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    sessions_attendues, warnings = _diagnostic_sessions_attendues(pv)

    # Pour chaque etudiant inscrit, comparer RS par parite
    lignes = pv.lignes.select_related('inscription_admin__etudiant').all()

    etudiants_diag = []
    for ligne in lignes:
        insc_admin = ligne.inscription_admin
        etudiant   = insc_admin.etudiant

        insc_peds = InscriptionPedagogique.objects.filter(
            inscription_admin=insc_admin,
        ).select_related('semestre')

        semestres_diag = {'Impairs': None, 'Pairs': None}
        total_pondere = Decimal('0')
        total_credits = Decimal('0')
        credits_recalc = 0

        for ip in insc_peds:
            code_sem = (ip.semestre.code_semestre or '').upper()
            digits = ''.join(c for c in code_sem if c.isdigit())
            if not digits:
                continue
            parite = 'Impairs' if (int(digits) % 2 == 1) else 'Pairs'

            qs_rs = ResultatSemestre.objects.filter(
                inscription_ped=ip,
                session__annee_univ=pv.annee_univ,
                session__institution=pv.institution,
                session__type_semestre=parite,
            )
            rs_sn = qs_rs.filter(session__type_session='normale').order_by('-session__id').first()
            rs_sr = qs_rs.filter(session__type_session='rattrapage').order_by('-session__id').first()

            # Regle de selection : SR cloture prioritaire, sinon SN
            if rs_sr and rs_sr.session.est_close:
                retenu  = 'SR'
                rs_used = rs_sr
            elif rs_sn:
                retenu  = 'SN'
                rs_used = rs_sn
            else:
                retenu  = None
                rs_used = None

            # Anomalie : SN.id > SR.id alors que SR existe (bug latent ordre insertion)
            anomalie = None
            if rs_sn and rs_sr and rs_sn.id > rs_sr.id:
                anomalie = (
                    f"RS_SN.id ({rs_sn.id}) > RS_SR.id ({rs_sr.id}) — "
                    f"recalcul SN posterieur au SR. L'ancien code aurait pris SN."
                )

            def _serialize_rs(rs):
                if rs is None:
                    return None
                return {
                    'id':              rs.id,
                    'session_id':      rs.session_id,
                    'session_code':    rs.session.code,
                    'session_close':   rs.session.est_close,
                    'moyenne':         str(rs.moyenne),
                    'credits_valides': rs.credits_valides,
                    'est_admis':       rs.est_admis,
                }

            semestres_diag[parite] = {
                'RS_SN':    _serialize_rs(rs_sn),
                'RS_SR':    _serialize_rs(rs_sr),
                'retenu':   retenu,
                'anomalie': anomalie,
            }

            # Calcul moyenne annuelle recalculee
            # NB : credits_valides cumule capitalisation modulaire (Art. 13)
            # → on l'ajoute meme si est_admis=False (modules valides individuellement)
            if rs_used is not None:
                sem_credits = Decimal(str(ip.semestre.credits or 30))
                total_pondere += rs_used.moyenne * sem_credits
                total_credits += sem_credits
                credits_recalc += rs_used.credits_valides

        if total_credits > 0:
            moy_recalc = (total_pondere / total_credits).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP,
            )
        else:
            moy_recalc = Decimal('0')

        moy_actuelle = ligne.moyenne_annuelle or Decimal('0')
        diff_moy     = (moy_recalc - moy_actuelle).quantize(Decimal('0.01'))
        cred_actuels = ligne.credits_annuels or 0
        diff_cred    = credits_recalc - cred_actuels

        etudiants_diag.append({
            'matricule':                   etudiant.matricule,
            'nom':                         f"{etudiant.nom_display} {etudiant.prenom_fr or ''}".strip(),
            'semestres':                   semestres_diag,
            'moyenne_annuelle_recalculee': str(moy_recalc),
            'moyenne_annuelle_actuelle':   str(moy_actuelle),
            'diff':                        f"{diff_moy:+}",
            'credits_recalcules':          credits_recalc,
            'credits_actuels':             cred_actuels,
            'diff_credits':                diff_cred,
            'a_un_diff':                   (diff_moy != Decimal('0') or diff_cred != 0),
        })

    nb_anomalies = sum(
        1 for e in etudiants_diag
        for s in e['semestres'].values()
        if s and s['anomalie']
    )
    nb_avec_diff = sum(1 for e in etudiants_diag if e['a_un_diff'])

    return Response({
        'pv_id':              pv.id,
        'type_pv':            pv.type_pv,
        'annee_univ':         pv.annee_univ.annee if pv.annee_univ else None,
        'filiere':            (pv.filiere.intitule_fr or pv.filiere.code) if pv.filiere else None,
        'niveau':             pv.niveau,
        'sessions_attendues': sessions_attendues,
        'warnings':           warnings,
        'nb_etudiants':       len(etudiants_diag),
        'nb_anomalies':       nb_anomalies,
        'nb_avec_diff':       nb_avec_diff,
        'etudiants':          etudiants_diag,
    })
