"""
Régression — cohérence SN <-> SR pour les dettes inter-niveaux.

Bug (2026-06-25) : une dette (est_dette=True) ramenée à un niveau supérieur est
rangée sous une InscriptionPedagogique d'un semestre inférieur (ex. S1 dans une
inscription L2). `generer_obligations` ne tourne que sur le PV du semestre courant
(S3/S4) → la dette S1 n'a jamais d'ObligationRattrapage. Conséquence : l'étudiant
figure en session NORMALE (feuille pilotée par l'inscription) mais PAS en
RATTRAPAGE (feuille pilotée par l'obligation).

Fix : `dette_ie_ids_non_valides` ajoute, à la feuille de rattrapage, les IE-dettes
NON VALIDÉES de l'année courante. Ce test fige le comportement ET ses garde-fous :
  - dette NON validée (E/NV) de l'année courante → INCLUSE ;
  - dette VALIDÉE (V/VCS/VCI) → EXCLUE (l'année de validation n'est jamais touchée) ;
  - EM normal (non-dette) → EXCLU (reste piloté par l'obligation) ;
  - dette d'une année ANTÉRIEURE → EXCLUE.
"""
from decimal import Decimal

import pytest

from apps.parametres.models import Institution, Year, Niveau, Semestre
from apps.scolarite.models import Filiere
from apps.departement.models import Departement
from apps.absence.models import Etudiant
from apps.em.models import EM
from apps.inscriptions.models import (
    InscriptionAdministrative, InscriptionPedagogique, InscriptionElement,
)
from apps.evaluations.models import SessionEvaluation, ResultatElement
from apps.evaluations.services.note_lecture import dette_ie_ids_non_valides


@pytest.fixture
def scenario(db):
    inst   = Institution.objects.create(acronyme='TST', nom='Institut Test', est_principale=True)
    niveau = Niveau.objects.create(niveau='L1')
    fil    = Filiere.objects.create(code='LP', intitule_fr='Licence Pro', institution=inst)
    dept   = Departement.objects.create(nom='G1', institution=inst, filiere=fil, niveau=niveau)
    sem    = Semestre.objects.create(code_semestre='S1', semestre='Semestre 1',
                                     niveau_semestre=niveau, type_semestre='I')
    y_cur  = Year.objects.create(annee='2024-2025', est_active=True)
    y_old  = Year.objects.create(annee='2023-2024')
    em     = EM.objects.create(code_em='M011', intitule='Maths', departement=dept,
                               semestre=sem, institution=inst)

    sn_cur = SessionEvaluation.objects.create(annee_univ=y_cur, institution=inst,
                                              type_session='normale',    type_semestre='Impairs')
    sr_cur = SessionEvaluation.objects.create(annee_univ=y_cur, institution=inst,
                                              type_session='rattrapage', type_semestre='Impairs')
    sn_old = SessionEvaluation.objects.create(annee_univ=y_old, institution=inst,
                                              type_session='normale',    type_semestre='Impairs')

    def make_ie(idx, year, est_dette):
        etu = Etudiant.objects.create(matricule=f'M{idx}', nom=f'Etu{idx}',
                                      departement=dept, genre='M')
        adm = InscriptionAdministrative.objects.create(
            etudiant=etu, annee_univ=year, filiere=fil, institution=inst,
            niveau=1, numero_inscription=f'INS-{idx}')
        ped = InscriptionPedagogique.objects.create(inscription_admin=adm, semestre=sem)
        return InscriptionElement.objects.create(inscription_ped=ped, em=em, est_dette=est_dette)

    def make_res(ie, session, code, valide=False, note=Decimal('5.00')):
        ResultatElement.objects.create(inscription_element=ie, session=session,
                                       note_finale=note, code_statut=code, est_valide=valide)

    ie_dette_nv   = make_ie(1, y_cur, True);  make_res(ie_dette_nv,   sn_cur, 'NV')                 # inclus
    ie_dette_e    = make_ie(2, y_cur, True);  make_res(ie_dette_e,    sn_cur, 'E')                  # inclus
    # Cas 23606 : dette NON passée en SN (aucune note) → code_statut vide,
    # note_finale=0, est_valide=False → doit quand même figurer au rattrapage.
    ie_dette_vide = make_ie(3, y_cur, True);  make_res(ie_dette_vide, sn_cur, '', note=Decimal('0.00'))  # inclus
    ie_dette_v    = make_ie(4, y_cur, True);  make_res(ie_dette_v,    sn_cur, 'V',   valide=True)   # EXCLU (validé)
    ie_dette_vcs  = make_ie(5, y_cur, True);  make_res(ie_dette_vcs,  sn_cur, 'VCS', valide=True)   # EXCLU (validé compensation)
    ie_normal     = make_ie(6, y_cur, False); make_res(ie_normal,     sn_cur, 'NV')                 # EXCLU (non-dette)
    ie_old        = make_ie(7, y_old, True);  make_res(ie_old,        sn_old, 'NV')                 # EXCLU (année antérieure)

    return {
        'em': em, 'sr_cur': sr_cur,
        'ie_vide': ie_dette_vide.id,
        'inclus': {ie_dette_nv.id, ie_dette_e.id, ie_dette_vide.id},
        'exclus': {ie_dette_v.id, ie_dette_vcs.id, ie_normal.id, ie_old.id},
    }


class TestDetteRattrapage:

    def test_resultat_exact(self, scenario):
        res = set(dette_ie_ids_non_valides(scenario['em'].id, scenario['sr_cur']))
        assert res == scenario['inclus']

    def test_absent_sn_code_vide_inclus(self, scenario):
        # Cas 23606 : dette NON passée en SN (code_statut vide, est_valide=False)
        # → doit figurer au rattrapage (l'étudiant n'est pas validé).
        res = set(dette_ie_ids_non_valides(scenario['em'].id, scenario['sr_cur']))
        assert scenario['ie_vide'] in res

    def test_dette_validee_jamais_incluse(self, scenario):
        # Garde-fou : un EM validé (V) n'est jamais ajouté → l'année de validation
        # n'est pas affectée.
        res = set(dette_ie_ids_non_valides(scenario['em'].id, scenario['sr_cur']))
        assert res.isdisjoint(scenario['exclus'])

    def test_non_dette_exclu(self, scenario):
        res = set(dette_ie_ids_non_valides(scenario['em'].id, scenario['sr_cur']))
        # Aucun IE non-dette ne doit remonter (le flux normal reste piloté par l'obligation).
        from apps.inscriptions.models import InscriptionElement as IE
        for ie_id in res:
            assert IE.objects.get(pk=ie_id).est_dette is True

    def test_annee_anterieure_exclue(self, scenario):
        res = set(dette_ie_ids_non_valides(scenario['em'].id, scenario['sr_cur']))
        from apps.inscriptions.models import InscriptionElement as IE
        for ie_id in res:
            annee = IE.objects.get(pk=ie_id).inscription_ped.inscription_admin.annee_univ.annee
            assert annee == '2024-2025'

    def test_acquis_par_compensation_consolidee_exclu(self, scenario, monkeypatch):
        """Garde-fou CONSOLIDÉ (cas 23641) : une dette dont le ResultatElement
        brut est 'NV' mais qui est ACQUISE au relevé consolidé (compensation /
        capitalisation cross-année) doit être EXCLUE du rattrapage — sinon
        l'étudiant figure à tort dans la liste « refaire ». On simule
        em_acquis_consolide=True pour l'étudiant concerné."""
        import apps.evaluations.services.note_lecture as nl
        from apps.inscriptions.models import InscriptionElement
        ie_vide = InscriptionElement.objects.get(pk=scenario['ie_vide'])
        etu_compense_id = ie_vide.inscription_ped.inscription_admin.etudiant_id
        # Acquis au consolidé UNIQUEMENT pour l'étudiant de ie_vide.
        monkeypatch.setattr(
            nl, 'em_acquis_consolide',
            lambda etu, em, annee: etu.id == etu_compense_id,
        )
        res = set(dette_ie_ids_non_valides(scenario['em'].id, scenario['sr_cur']))
        # ie_vide (acquis par compensation) est désormais EXCLU…
        assert scenario['ie_vide'] not in res
        # …mais les autres dettes non validées restent INCLUSES.
        assert (scenario['inclus'] - {scenario['ie_vide']}).issubset(res)


@pytest.mark.django_db
def test_obligation_acquise_par_compensation_exclue(monkeypatch):
    """eligible_rattrapage_ie_ids : une OBLIGATION dont l'EM est acquis SEULEMENT
    par compensation (jamais validé dans sa propre session) est exclue du
    rattrapage (cas 23631/23620). Une obligation non acquise reste."""
    from decimal import Decimal
    import apps.evaluations.services.note_lecture as nl
    from apps.evaluations.services.note_lecture import eligible_rattrapage_ie_ids
    from apps.evaluations.models import (
        SessionEvaluation, ResultatElement, PVDeliberation, LigneDeliberation,
        ObligationRattrapage,
    )

    inst = Institution.objects.create(acronyme='TST', nom='T', est_principale=True)
    niv  = Niveau.objects.create(niveau='L1')
    fil  = Filiere.objects.create(code='LP', intitule_fr='LP', institution=inst)
    dept = Departement.objects.create(nom='G1', institution=inst, filiere=fil, niveau=niv)
    sem  = Semestre.objects.create(code_semestre='S1', semestre='S1', niveau_semestre=niv, type_semestre='I')
    year = Year.objects.create(annee='2024-2025', est_active=True)
    em   = EM.objects.create(code_em='ST11', intitule='Algèbre', departement=dept, semestre=sem, institution=inst)
    sn   = SessionEvaluation.objects.create(annee_univ=year, institution=inst, type_session='normale',    type_semestre='Impairs')
    sr   = SessionEvaluation.objects.create(annee_univ=year, institution=inst, type_session='rattrapage', type_semestre='Impairs')

    def _obligation(mat):
        etu = Etudiant.objects.create(matricule=mat, nom=mat, departement=dept, genre='M')
        adm = InscriptionAdministrative.objects.create(
            etudiant=etu, annee_univ=year, filiere=fil, institution=inst,
            niveau=1, numero_inscription=f'INS-{mat}')
        ped = InscriptionPedagogique.objects.create(inscription_admin=adm, semestre=sem)
        ie  = InscriptionElement.objects.create(inscription_ped=ped, em=em, est_dette=False)
        # Raw NON validé (note < 10, NV) : si l'EM est « acquis », c'est par compensation.
        ResultatElement.objects.create(inscription_element=ie, session=sn, note_finale=Decimal('7.00'), code_statut='NV', est_valide=False)
        pv  = PVDeliberation.objects.create(type_pv='semestriel', session=sn, filiere=fil, institution=inst, niveau=1, semestre_code='S1')
        lg  = LigneDeliberation.objects.create(pv=pv, inscription_admin=adm, decision='ajourned')
        ObligationRattrapage.objects.create(ligne=lg, inscription_element=ie, type_obligation='facultatif')
        return etu, ie

    etu_comp, ie_comp = _obligation('COMP')   # sera « acquis par compensation »
    etu_dette, ie_dette = _obligation('DETTE')  # vraie dette (non acquis)

    # em_acquis_consolide = True uniquement pour l'étudiant compensé.
    monkeypatch.setattr(nl, 'em_acquis_consolide', lambda etu, em_, annee: etu.id == etu_comp.id)

    res = eligible_rattrapage_ie_ids(em.id, sr)
    assert ie_comp.id not in res      # acquis par compensation → exclu du rattrapage
    assert ie_dette.id in res         # vraie dette → reste éligible


@pytest.mark.django_db
def test_vcs_consolide_rattrapable_si_exception_active(monkeypatch):
    """Exception « rattrapage VCS/VCI » (cas 255010/HE53) : un EM acquis SEULEMENT
    par compensation, mais de statut consolidé VCS, RESTE éligible au rattrapage
    facultatif QUAND le drapeau est actif sur la session NORMALE. Drapeau à False
    (défaut) → exclu (comportement préservé). VCI seul ne suffit pas pour un VCS."""
    from decimal import Decimal
    import apps.evaluations.services.note_lecture as nl
    from apps.evaluations.services.note_lecture import eligible_rattrapage_ie_ids
    from apps.evaluations.models import (
        SessionEvaluation, ResultatElement, PVDeliberation, LigneDeliberation,
        ObligationRattrapage,
    )

    inst = Institution.objects.create(acronyme='TST', nom='T', est_principale=True)
    niv  = Niveau.objects.create(niveau='L1')
    fil  = Filiere.objects.create(code='LP', intitule_fr='LP', institution=inst)
    dept = Departement.objects.create(nom='G1', institution=inst, filiere=fil, niveau=niv)
    sem  = Semestre.objects.create(code_semestre='S1', semestre='S1', niveau_semestre=niv, type_semestre='I')
    year = Year.objects.create(annee='2025-2026', est_active=True)
    em   = EM.objects.create(code_em='HE53', intitule='DPP', departement=dept, semestre=sem, institution=inst)
    sn   = SessionEvaluation.objects.create(annee_univ=year, institution=inst, type_session='normale',    type_semestre='Impairs')
    sr   = SessionEvaluation.objects.create(annee_univ=year, institution=inst, type_session='rattrapage', type_semestre='Impairs')

    etu = Etudiant.objects.create(matricule='255010', nom='Efah', departement=dept, genre='M')
    adm = InscriptionAdministrative.objects.create(
        etudiant=etu, annee_univ=year, filiere=fil, institution=inst, niveau=1, numero_inscription='INS-1')
    ped = InscriptionPedagogique.objects.create(inscription_admin=adm, semestre=sem)
    ie  = InscriptionElement.objects.create(inscription_ped=ped, em=em, est_dette=False)
    # Code STOCKÉ 'NV' (le VCS n'existe qu'au consolidé) — comme 255010/HE53.
    ResultatElement.objects.create(inscription_element=ie, session=sn, note_finale=Decimal('7.00'), code_statut='NV', est_valide=False)
    pv  = PVDeliberation.objects.create(type_pv='semestriel', session=sn, filiere=fil, institution=inst, niveau=1, semestre_code='S1')
    lg  = LigneDeliberation.objects.create(pv=pv, inscription_admin=adm, decision='admis')
    ObligationRattrapage.objects.create(ligne=lg, inscription_element=ie, type_obligation='facultatif', code_statut_initial='NV')

    # Acquis SEULEMENT par compensation ; statut consolidé = VCS.
    monkeypatch.setattr(nl, 'em_acquis_consolide', lambda e, m, a: True)
    monkeypatch.setattr(nl, 'statut_em_consolide', lambda e, m, a: 'VCS')

    # Drapeau OFF (défaut) → exclu.
    assert ie.id not in eligible_rattrapage_ie_ids(em.id, sr)

    # Drapeau VCS ON sur la session NORMALE → inclus (rattrapage facultatif).
    sn.rattrapage_vcs_actif = True
    sn.save()
    assert ie.id in eligible_rattrapage_ie_ids(em.id, sr)

    # Seul VCI actif → ne réintègre PAS un VCS.
    sn.rattrapage_vcs_actif = False
    sn.rattrapage_vci_actif = True
    sn.save()
    assert ie.id not in eligible_rattrapage_ie_ids(em.id, sr)


@pytest.mark.django_db
def test_dette_validee_au_rattrapage_reste_visible(monkeypatch):
    """Cas 23629/HE31 : une dette VALIDÉE via le rattrapage EN COURS (résultat SR
    code V) reste VISIBLE dans la feuille (avec sa note saisie), même si elle est
    « acquise » au consolidé. Une dette acquise par compensation SEULE (aucun
    résultat SR) reste exclue (correctif 23631/23620 préservé)."""
    from decimal import Decimal
    import apps.evaluations.services.note_lecture as nl
    from apps.evaluations.services.note_lecture import dette_ie_ids_non_valides
    from apps.evaluations.models import SessionEvaluation, ResultatElement

    inst = Institution.objects.create(acronyme='TST', nom='T', est_principale=True)
    niv  = Niveau.objects.create(niveau='L1')
    fil  = Filiere.objects.create(code='LP', intitule_fr='LP', institution=inst)
    dept = Departement.objects.create(nom='G1', institution=inst, filiere=fil, niveau=niv)
    sem  = Semestre.objects.create(code_semestre='S1', semestre='S1', niveau_semestre=niv, type_semestre='I')
    year = Year.objects.create(annee='2025-2026', est_active=True)
    em   = EM.objects.create(code_em='HE31', intitule='Eco', departement=dept, semestre=sem, institution=inst)
    sn   = SessionEvaluation.objects.create(annee_univ=year, institution=inst, type_session='normale',    type_semestre='Impairs')
    sr   = SessionEvaluation.objects.create(annee_univ=year, institution=inst, type_session='rattrapage', type_semestre='Impairs')

    def _dette(mat, with_sr_v):
        etu = Etudiant.objects.create(matricule=mat, nom=mat, departement=dept, genre='M')
        adm = InscriptionAdministrative.objects.create(
            etudiant=etu, annee_univ=year, filiere=fil, institution=inst, niveau=2, numero_inscription='INS-' + mat)
        ped = InscriptionPedagogique.objects.create(inscription_admin=adm, semestre=sem)
        ie  = InscriptionElement.objects.create(inscription_ped=ped, em=em, est_dette=True)
        ResultatElement.objects.create(inscription_element=ie, session=sn, note_finale=Decimal('8.00'), code_statut='NV', est_valide=False)
        if with_sr_v:
            ResultatElement.objects.create(inscription_element=ie, session=sr, note_finale=Decimal('10.00'), code_statut='V', est_valide=True)
        return etu, ie

    _etu_rat, ie_rat   = _dette('23629', with_sr_v=True)    # validé via le rattrapage
    _etu_comp, ie_comp = _dette('COMP',  with_sr_v=False)   # acquis par compensation seule

    # Les deux « acquis » au consolidé.
    monkeypatch.setattr(nl, 'em_acquis_consolide', lambda e, m, a: True)

    res = set(dette_ie_ids_non_valides(em.id, sr))
    assert ie_rat.id in res       # validé au rattrapage en cours → reste visible (sa note)
    assert ie_comp.id not in res  # acquis par compensation seule → exclu


@pytest.mark.django_db
def test_dette_vcs_rattrapable_si_exception_active(monkeypatch):
    """Cas 24603/ST11 : une dette acquise par compensation (statut consolidé VCS),
    SANS obligation et PAS encore rattrapée, devient éligible quand le drapeau VCS
    est actif (rattrapage facultatif). Drapeau off → exclue ; VCI seul ne réintègre
    pas un VCS. Symétrie avec la voie obligation (cas 255010/HE53)."""
    from decimal import Decimal
    import apps.evaluations.services.note_lecture as nl
    from apps.evaluations.services.note_lecture import dette_ie_ids_non_valides
    from apps.evaluations.models import SessionEvaluation, ResultatElement

    inst = Institution.objects.create(acronyme='TST', nom='T', est_principale=True)
    niv  = Niveau.objects.create(niveau='L1')
    fil  = Filiere.objects.create(code='LP', intitule_fr='LP', institution=inst)
    dept = Departement.objects.create(nom='G1', institution=inst, filiere=fil, niveau=niv)
    sem  = Semestre.objects.create(code_semestre='S1', semestre='S1', niveau_semestre=niv, type_semestre='I')
    year = Year.objects.create(annee='2025-2026', est_active=True)
    em   = EM.objects.create(code_em='ST11', intitule='Analyse', departement=dept, semestre=sem, institution=inst)
    sn   = SessionEvaluation.objects.create(annee_univ=year, institution=inst, type_session='normale',    type_semestre='Impairs')
    sr   = SessionEvaluation.objects.create(annee_univ=year, institution=inst, type_session='rattrapage', type_semestre='Impairs')

    etu = Etudiant.objects.create(matricule='24603', nom='Aichetou', departement=dept, genre='F')
    adm = InscriptionAdministrative.objects.create(
        etudiant=etu, annee_univ=year, filiere=fil, institution=inst, niveau=2, numero_inscription='INS-24603')
    ped = InscriptionPedagogique.objects.create(inscription_admin=adm, semestre=sem)
    ie  = InscriptionElement.objects.create(inscription_ped=ped, em=em, est_dette=True)
    # SN NV, AUCUN résultat SR (pas encore rattrapé).
    ResultatElement.objects.create(inscription_element=ie, session=sn, note_finale=Decimal('7.45'), code_statut='NV', est_valide=False)

    monkeypatch.setattr(nl, 'em_acquis_consolide', lambda e, m, a: True)
    monkeypatch.setattr(nl, 'statut_em_consolide', lambda e, m, a: 'VCS')

    # Drapeau OFF (défaut) → exclue.
    assert ie.id not in dette_ie_ids_non_valides(em.id, sr)
    # VCS ON sur la session NORMALE → incluse (rattrapage facultatif).
    sn.rattrapage_vcs_actif = True
    sn.save()
    assert ie.id in dette_ie_ids_non_valides(em.id, sr)
    # Seul VCI actif → ne réintègre PAS un VCS.
    sn.rattrapage_vcs_actif = False
    sn.rattrapage_vci_actif = True
    sn.save()
    assert ie.id not in dette_ie_ids_non_valides(em.id, sr)


@pytest.mark.django_db
def test_dette_code_stocke_vcs_rattrapable_si_exception(monkeypatch):
    """Cas 24609/ST11 : une dette dont le code STOCKÉ est DÉJÀ 'VCS' (donc écartée
    du filtre candidat par défaut via .exclude(V/VCI/VCS)) doit être RÉINTÉGRÉE au
    pool des candidats quand le drapeau VCS est actif, puis conservée par la
    condition 3. C'est le cas qui « échappait » au premier correctif dette."""
    from decimal import Decimal
    import apps.evaluations.services.note_lecture as nl
    from apps.evaluations.services.note_lecture import dette_ie_ids_non_valides
    from apps.evaluations.models import SessionEvaluation, ResultatElement

    inst = Institution.objects.create(acronyme='TST', nom='T', est_principale=True)
    niv  = Niveau.objects.create(niveau='L1')
    fil  = Filiere.objects.create(code='LP', intitule_fr='LP', institution=inst)
    dept = Departement.objects.create(nom='G1', institution=inst, filiere=fil, niveau=niv)
    sem  = Semestre.objects.create(code_semestre='S1', semestre='S1', niveau_semestre=niv, type_semestre='I')
    year = Year.objects.create(annee='2025-2026', est_active=True)
    em   = EM.objects.create(code_em='ST11', intitule='Analyse', departement=dept, semestre=sem, institution=inst)
    sn   = SessionEvaluation.objects.create(annee_univ=year, institution=inst, type_session='normale',    type_semestre='Impairs')
    sr   = SessionEvaluation.objects.create(annee_univ=year, institution=inst, type_session='rattrapage', type_semestre='Impairs')

    etu = Etudiant.objects.create(matricule='24609', nom='X', departement=dept, genre='F')
    adm = InscriptionAdministrative.objects.create(
        etudiant=etu, annee_univ=year, filiere=fil, institution=inst, niveau=2, numero_inscription='INS-24609')
    ped = InscriptionPedagogique.objects.create(inscription_admin=adm, semestre=sem)
    ie  = InscriptionElement.objects.create(inscription_ped=ped, em=em, est_dette=True)
    # Code STOCKÉ = 'VCS' → écarté du filtre candidat par défaut.
    ResultatElement.objects.create(inscription_element=ie, session=sn, note_finale=Decimal('8.00'), code_statut='VCS', est_valide=False)

    monkeypatch.setattr(nl, 'em_acquis_consolide', lambda e, m, a: True)
    monkeypatch.setattr(nl, 'statut_em_consolide', lambda e, m, a: 'VCS')

    # Drapeau OFF → même pas candidate → exclue.
    assert ie.id not in dette_ie_ids_non_valides(em.id, sr)
    # VCS ON → réintégrée au pool + conservée par la condition 3.
    sn.rattrapage_vcs_actif = True
    sn.save()
    assert ie.id in dette_ie_ids_non_valides(em.id, sr)
