"""
Tests du cycle de vie complet de RachatNote (apps/evaluations).

Comportement ACTUEL figé (oracle de non-régression MySQL → PostgreSQL) :

  1. RachatNote est un registre APPEND-ONLY immuable :
     - `save()` sur un objet existant et `delete()` d'instance lèvent PermissionError ;
     - `QuerySet.delete()` contourne la garde (utilisé par la purge PV admin).
  2. La création (POST /api/v1/evaluations/rachats/) a UN SEUL side-effect :
     `ligne.decision` passe à 'rachat'. AUCUNE note (`Note`), AUCUN résultat
     calculé (`ResultatElement`/`ResultatSemestre`) n'est modifié, aucun
     recalcul n'est déclenché — `nouvelle_valeur` est purement déclarative.
  3. Garde-fous du POST : PV clos -> 403 ; ligne non ajournée -> 400 ;
     re-rachat d'une ligne déjà 'rachat' -> accepté (N enregistrements).
  4. `decidee_par` = user du request (read_only côté serializer).
  5. `calculer_decisions()` ne rétrograde JAMAIS une décision 'rachat'
     (idempotence), même si le ResultatSemestre dit non-admis.
  6. RBAC : module 'delib_rachat' (create -> action 'modifier', list -> 'voir').
  7. Bug connu FIGÉ : PUT/PATCH lève TypeError (perform_update fait
     `raise status.HTTP_405_METHOD_NOT_ALLOWED`, un int — intention : 405).
"""
import pytest
from decimal import Decimal

from rest_framework.test import APIClient

from apps.evaluations.models import RachatNote, LigneDeliberation
from apps.evaluations.services.deliberation_semestre import DeliberationSemestreService

from tests.factories.auth import (
    UserFactory, DEUserFactory,
    ModuleRBACFactory, ActionRBACFactory, ModuleActionRBACFactory,
    RoleDefaultFactory,
)
from tests.factories.parametres import YearFactory
from tests.factories.deliberation import PVDeliberationSemestrielFactory
from tests.factories.evaluations import (
    SessionNormaleImpairsFactory, NoteFactory,
    ResultatElementFactory, ResultatSemestreFactory,
)
from tests.factories.inscriptions import (
    InscriptionAdministrativeFactory,
    InscriptionPedagogiqueFactory,
    InscriptionElementFactory,
)


URL_RACHATS = '/api/v1/evaluations/rachats/'


# ── Fixtures locales (aucun fichier partagé modifié) ───────────────────────────

@pytest.fixture
def annee(db):
    return YearFactory(annee='2025-2026')


@pytest.fixture
def session_normale(db, annee, institution):
    return SessionNormaleImpairsFactory(annee_univ=annee, institution=institution)


@pytest.fixture
def pv_s1(db, session_normale, filiere_dlp, institution):
    return PVDeliberationSemestrielFactory(
        session=session_normale, filiere=filiere_dlp,
        institution=institution, semestre_code='S1',
    )


@pytest.fixture
def ligne_ajournee(db, pv_s1, annee, filiere_dlp, institution):
    ia = InscriptionAdministrativeFactory(
        filiere=filiere_dlp, annee_univ=annee,
        niveau=1, institution=institution,
    )
    return LigneDeliberation.objects.create(
        pv=pv_s1, inscription_admin=ia, decision='ajourned',
    )


@pytest.fixture
def user_jury(db):
    """User role DE armé RBAC delib_rachat.voir + delib_rachat.modifier.

    La BD test tourne --no-migrations : les seeds RBAC des migrations
    0008/0009 sont absents -> Module/Action/RoleDefault créés via factories
    (même pattern que tests/test_rbac.py).
    """
    user = DEUserFactory()
    module = ModuleRBACFactory(code='delib_rachat', nom='Deliberations : rachats')
    for action_code in ('voir', 'modifier'):
        ma = ModuleActionRBACFactory(
            module=module, action=ActionRBACFactory(code=action_code),
        )
        RoleDefaultFactory(role='DE', module_action=ma, allowed=True)
    return user


@pytest.fixture
def api(user_jury):
    client = APIClient()
    client.force_authenticate(user=user_jury)
    return client


def _payload(pv, ligne, ancienne='8.50', nouvelle='10.00', motif='Décision du jury — Art. 562'):
    return {
        'pv': pv.id, 'ligne': ligne.id,
        'ancienne_valeur': ancienne, 'nouvelle_valeur': nouvelle,
        'motif': motif,
    }


# ── Modèle : registre immuable append-only ─────────────────────────────────────

class TestModeleImmuable:

    def test_creation_append_only(self, pv_s1, ligne_ajournee, user_jury):
        rachat = RachatNote.objects.create(
            pv=pv_s1, ligne=ligne_ajournee,
            ancienne_valeur=Decimal('8.50'), nouvelle_valeur=Decimal('10.00'),
            motif='Rachat jury', decidee_par=user_jury,
        )
        assert rachat.pk is not None
        assert rachat.date_decision is not None          # auto_now_add
        assert rachat.ancienne_valeur == Decimal('8.50')
        assert rachat.nouvelle_valeur == Decimal('10.00')
        assert rachat.decidee_par_id == user_jury.pk

    def test_save_sur_objet_existant_interdit(self, pv_s1, ligne_ajournee, user_jury):
        rachat = RachatNote.objects.create(
            pv=pv_s1, ligne=ligne_ajournee,
            ancienne_valeur=Decimal('8.50'), nouvelle_valeur=Decimal('10.00'),
            motif='Rachat jury', decidee_par=user_jury,
        )
        rachat.nouvelle_valeur = Decimal('12.00')
        with pytest.raises(PermissionError):
            rachat.save()

    def test_delete_instance_interdit(self, pv_s1, ligne_ajournee, user_jury):
        rachat = RachatNote.objects.create(
            pv=pv_s1, ligne=ligne_ajournee,
            ancienne_valeur=Decimal('8.50'), nouvelle_valeur=Decimal('10.00'),
            motif='Rachat jury', decidee_par=user_jury,
        )
        with pytest.raises(PermissionError):
            rachat.delete()
        assert RachatNote.objects.filter(pk=rachat.pk).exists()

    def test_queryset_delete_contourne_la_garde(self, pv_s1, ligne_ajournee, user_jury):
        """Comportement figé : QuerySet.delete() ne passe pas par Model.delete()
        — c'est le chemin utilisé délibérément par la purge PV admin
        (PVDeliberationViewSet.perform_destroy)."""
        rachat = RachatNote.objects.create(
            pv=pv_s1, ligne=ligne_ajournee,
            ancienne_valeur=Decimal('8.50'), nouvelle_valeur=Decimal('10.00'),
            motif='Rachat jury', decidee_par=user_jury,
        )
        RachatNote.objects.filter(pk=rachat.pk).delete()
        assert not RachatNote.objects.filter(pk=rachat.pk).exists()


# ── API : création et effets exacts ────────────────────────────────────────────

class TestCreationEtEffets:

    def test_post_cree_rachat_et_passe_ligne_en_rachat(
            self, api, user_jury, pv_s1, ligne_ajournee):
        resp = api.post(URL_RACHATS, _payload(pv_s1, ligne_ajournee), format='json')

        assert resp.status_code == 201
        assert RachatNote.objects.count() == 1
        rachat = RachatNote.objects.get()
        assert rachat.pv_id == pv_s1.pk
        assert rachat.ligne_id == ligne_ajournee.pk
        assert rachat.ancienne_valeur == Decimal('8.50')
        assert rachat.nouvelle_valeur == Decimal('10.00')
        assert rachat.decidee_par_id == user_jury.pk
        # Side-effect UNIQUE : la décision de la ligne passe à 'rachat'
        ligne_ajournee.refresh_from_db()
        assert ligne_ajournee.decision == 'rachat'

    def test_decidee_par_force_au_user_connecte(
            self, api, user_jury, pv_s1, ligne_ajournee):
        """`decidee_par` est read_only : un pk fourni dans le payload est ignoré."""
        autre = UserFactory(username='intrus')
        data = _payload(pv_s1, ligne_ajournee)
        data['decidee_par'] = autre.pk

        resp = api.post(URL_RACHATS, data, format='json')

        assert resp.status_code == 201
        assert RachatNote.objects.get().decidee_par_id == user_jury.pk

    def test_rachat_ne_modifie_aucune_note_ni_resultat(
            self, api, pv_s1, ligne_ajournee, session_normale, semestre_S1):
        """Invariant central : `nouvelle_valeur` n'est appliquée NULLE PART.
        Note, ResultatElement et ResultatSemestre restent identiques, aucun
        recalcul n'est déclenché."""
        from apps.evaluations.signals import disable_recalcul_signal

        ip = InscriptionPedagogiqueFactory(
            inscription_admin=ligne_ajournee.inscription_admin, semestre=semestre_S1,
        )
        ie = InscriptionElementFactory(inscription_ped=ip, element=None)
        # Le post_save de Note recalcule ResultatElement/ResultatSemestre :
        # on le désactive pour figer des valeurs posées à la main (sinon les
        # factories ci-dessous violent les unique_together).
        with disable_recalcul_signal():
            note = NoteFactory(
                inscription_element=ie, session=session_normale,
                type_note='EXAM', valeur=Decimal('8.00'),
            )
        res_el = ResultatElementFactory(
            inscription_element=ie, session=session_normale,
            note_finale=Decimal('8.00'), est_valide=False, code_statut='NV',
        )
        res_sem = ResultatSemestreFactory(
            inscription_ped=ip, session=session_normale,
            moyenne=Decimal('9.50'), credits_valides=12, est_admis=False,
        )

        resp = api.post(
            URL_RACHATS,
            _payload(pv_s1, ligne_ajournee, ancienne='9.50', nouvelle='10.00'),
            format='json',
        )
        assert resp.status_code == 201

        note.refresh_from_db()
        res_el.refresh_from_db()
        res_sem.refresh_from_db()
        assert note.valeur == Decimal('8.00')
        assert res_el.note_finale == Decimal('8.00')
        assert res_el.est_valide is False
        assert res_el.code_statut == 'NV'
        assert res_sem.moyenne == Decimal('9.50')
        assert res_sem.credits_valides == 12       # pas de crédit ajouté
        assert res_sem.est_admis is False
        # Seule la ligne de délibération a bougé
        ligne_ajournee.refresh_from_db()
        assert ligne_ajournee.decision == 'rachat'

    def test_re_rachat_accepte_registre_append_only(
            self, api, pv_s1, ligne_ajournee):
        """Une ligne déjà 'rachat' peut être re-rachetée : N enregistrements
        (pas de unique_together sur ligne)."""
        r1 = api.post(URL_RACHATS, _payload(pv_s1, ligne_ajournee), format='json')
        r2 = api.post(
            URL_RACHATS,
            _payload(pv_s1, ligne_ajournee, ancienne='10.00', nouvelle='11.00',
                     motif='Correction jury'),
            format='json',
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert RachatNote.objects.filter(ligne=ligne_ajournee).count() == 2

    def test_calculer_decisions_ne_retrograde_jamais_un_rachat(
            self, api, pv_s1, ligne_ajournee, session_normale, semestre_S1):
        """Après rachat, `calculer_decisions()` (Art. 15) ne rétrograde pas la
        décision même si le ResultatSemestre dit non-admis — idempotent."""
        ip = InscriptionPedagogiqueFactory(
            inscription_admin=ligne_ajournee.inscription_admin, semestre=semestre_S1,
        )
        ResultatSemestreFactory(
            inscription_ped=ip, session=session_normale,
            moyenne=Decimal('9.50'), credits_valides=12, est_admis=False,
        )
        resp = api.post(URL_RACHATS, _payload(pv_s1, ligne_ajournee), format='json')
        assert resp.status_code == 201

        service = DeliberationSemestreService(pv_s1)
        service.calculer_decisions()
        service.calculer_decisions()   # double appel : idempotence

        ligne_ajournee.refresh_from_db()
        assert ligne_ajournee.decision == 'rachat'
        assert RachatNote.objects.count() == 1


# ── API : garde-fous et méthodes interdites ────────────────────────────────────

class TestGardesFous:

    def test_pv_clos_refuse_403(self, api, pv_s1, ligne_ajournee):
        pv_s1.est_clos = True
        pv_s1.save(update_fields=['est_clos'])

        resp = api.post(URL_RACHATS, _payload(pv_s1, ligne_ajournee), format='json')

        assert resp.status_code == 403
        assert RachatNote.objects.count() == 0
        ligne_ajournee.refresh_from_db()
        assert ligne_ajournee.decision == 'ajourned'   # inchangée

    def test_ligne_admise_refuse_400(self, api, pv_s1, ligne_ajournee):
        ligne_ajournee.decision = 'admis'
        ligne_ajournee.save(update_fields=['decision'])

        resp = api.post(URL_RACHATS, _payload(pv_s1, ligne_ajournee), format='json')

        assert resp.status_code == 400
        assert RachatNote.objects.count() == 0

    def test_delete_refuse_de_403_rbac_supprimer_non_accorde(
            self, api, pv_s1, ligne_ajournee, user_jury):
        """DELETE mappe sur l'action RBAC 'supprimer' : AUCUN rôle n'a
        delib_rachat.supprimer (seed 0009) -> le RBAC refuse (403) AVANT
        d'atteindre perform_destroy (405)."""
        rachat = RachatNote.objects.create(
            pv=pv_s1, ligne=ligne_ajournee,
            ancienne_valeur=Decimal('8.50'), nouvelle_valeur=Decimal('10.00'),
            motif='Rachat jury', decidee_par=user_jury,
        )
        resp = api.delete(f'{URL_RACHATS}{rachat.pk}/')

        assert resp.status_code == 403
        assert RachatNote.objects.filter(pk=rachat.pk).exists()

    def test_delete_admin_refuse_405_method_not_allowed(
            self, db, pv_s1, ligne_ajournee, user_jury):
        """L'admin bypasse le RBAC et atteint perform_destroy ->
        MethodNotAllowed (405) : RachatNote est immuable même pour l'admin."""
        from tests.factories.auth import AdminUserFactory
        rachat = RachatNote.objects.create(
            pv=pv_s1, ligne=ligne_ajournee,
            ancienne_valeur=Decimal('8.50'), nouvelle_valeur=Decimal('10.00'),
            motif='Rachat jury', decidee_par=user_jury,
        )
        client = APIClient()
        client.force_authenticate(user=AdminUserFactory())

        resp = client.delete(f'{URL_RACHATS}{rachat.pk}/')

        assert resp.status_code == 405
        assert RachatNote.objects.filter(pk=rachat.pk).exists()

    def test_patch_renvoie_500_bug_connu_fige(
            self, api, pv_s1, ligne_ajournee, user_jury):
        """Bug connu FIGÉ (ne pas « corriger » ce test sans corriger la vue) :
        perform_update fait `raise status.HTTP_405_METHOD_NOT_ALLOWED` — un int
        n'est pas une exception -> TypeError, masqué en 500 générique par
        core.exceptions.custom_exception_handler. Intention : 405.
        L'important : la mise à jour N'A PAS lieu."""
        rachat = RachatNote.objects.create(
            pv=pv_s1, ligne=ligne_ajournee,
            ancienne_valeur=Decimal('8.50'), nouvelle_valeur=Decimal('10.00'),
            motif='Rachat jury', decidee_par=user_jury,
        )
        resp = api.patch(f'{URL_RACHATS}{rachat.pk}/',
                         {'nouvelle_valeur': '15.00'}, format='json')

        assert resp.status_code == 500
        rachat.refresh_from_db()
        assert rachat.nouvelle_valeur == Decimal('10.00')   # rien n'a bougé


# ── RBAC delib_rachat ──────────────────────────────────────────────────────────

class TestRBAC:

    def test_user_sans_permission_refuse_list_et_create(
            self, db, pv_s1, ligne_ajournee):
        """Role 'AA' sans RoleDefault ni UserPermission sur delib_rachat -> 403
        (fail-closed, même mécanique que tests/test_rbac.py)."""
        # Le module existe (créé ou non) mais aucune permission accordée
        ModuleActionRBACFactory(
            module=ModuleRBACFactory(code='delib_rachat'),
            action=ActionRBACFactory(code='modifier'),
        )
        client = APIClient()
        client.force_authenticate(user=UserFactory(username='aa_sans_droit'))

        assert client.get(URL_RACHATS).status_code == 403
        resp = client.post(URL_RACHATS, _payload(pv_s1, ligne_ajournee), format='json')
        assert resp.status_code == 403
        assert RachatNote.objects.count() == 0

    def test_de_avec_role_default_liste_les_rachats(
            self, api, pv_s1, ligne_ajournee, user_jury):
        RachatNote.objects.create(
            pv=pv_s1, ligne=ligne_ajournee,
            ancienne_valeur=Decimal('8.50'), nouvelle_valeur=Decimal('10.00'),
            motif='Rachat jury', decidee_par=user_jury,
        )
        resp = api.get(URL_RACHATS, {'pv': pv_s1.pk})

        assert resp.status_code == 200
        data = resp.json()
        rows = data['results'] if isinstance(data, dict) else data
        assert len(rows) == 1
        assert rows[0]['ancienne_valeur'] == '8.50'
        assert rows[0]['nouvelle_valeur'] == '10.00'
