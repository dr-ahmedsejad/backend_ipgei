"""
La vue de surveillance IPGEI (14/10/2026) : toutes les classes et leurs
sous-groupes de TP sur une journée.

Source : l'emploi du temps vivant (`SeanceReelle`) et les épreuves
(`EpreuveClasse`). Module RBAC « surveillance » : DA et AA par défaut, d'autres
rôles par la matrice des permissions, l'administrateur toujours.
La case ouvre la saisie des absences (`pointable`) selon les règles de l'écran
de saisie. Voir apps/ipgei/surveillance.py.
"""
import datetime as dt
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from tests.factories.auth import UserFactory

ANNEE = '2025-2026'
LUNDI = dt.date(2025, 10, 13)
URL = '/api/v1/ipgei/surveillance/'
CRENEAUX = [('08h00-09h30', 1), ('09h45-11h15', 2), ('11h30-13h00', 3),
            ('15h10-16h40', 4), ('17h00-18h30', 5)]


def api(u):
    c = APIClient()
    c.force_authenticate(user=u)
    return c


def droit(role, module, action):
    from apps.authentication.models import Action, Module, ModuleAction, RoleDefault
    mod, _ = Module.objects.get_or_create(code=module, defaults={'nom': module, 'icone': 'Eye', 'ordre': 1})
    act, _ = Action.objects.get_or_create(code=action, defaults={'nom': action})
    ma, _ = ModuleAction.objects.get_or_create(module=mod, action=act)
    RoleDefault.objects.update_or_create(role=role, module_action=ma, defaults={'allowed': True})


# Les droits de l'écran d'appel (17/10/2026) : `presences/saisie-seance/`
# (voir) et `presences/bulk/` (modifier), tous deux sous abs_saisie.
DROITS_SAISIE = [('abs_saisie', 'voir'), ('abs_saisie', 'modifier')]


@pytest.fixture(autouse=True)
def droits_par_defaut(db):
    """Ce que pose la migration authentication 0026 — la base de test ne joue
    pas les migrations de données (SEEDS_DESACTIVES_POUR_TESTS)."""
    for role in ('DA', 'AA'):
        droit(role, 'surveillance', 'voir')


@pytest.fixture
def monde(db):
    from apps.ipgei.models import (Classe, Matiere, NiveauIPGEI, SemestreIPGEI,
                                   SousGroupeTP)
    from apps.parametres.models import Creneau, Institution, Jour, Niveau, Seance, Semaine
    from apps.prof.models import Prof
    from apps.salle.models import Salle

    inst = Institution.objects.create(acronyme='IPG', nom='Institut', est_principale=True)
    mpsi, mp = Niveau.objects.create(niveau='MPSI'), Niveau.objects.create(niveau='MP')
    ref_mpsi = NiveauIPGEI.objects.create(institution=inst, code='MPSI', libelle='MPSI', rang=1)
    NiveauIPGEI.objects.create(institution=inst, code='MP', libelle='MP', rang=2)
    s1 = SemestreIPGEI.objects.create(institution=inst, code='S1', annee_universitaire=ANNEE,
                                      date_debut=dt.date(2025, 9, 15))
    semaine = s1.semaines.create(numero=5, date_debut=LUNDI, date_fin=LUNDI + dt.timedelta(days=6))

    jours = {}
    for i, lib in enumerate(('Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi')):
        jours[lib] = Jour.objects.create(jour=lib)
        Semaine.objects.create(annee_universitaire=ANNEE, numero_semaine=5, type_semestre='I',
                               jour_fk=jours[lib], date=LUNDI + dt.timedelta(days=i))
    creneaux = {lib: Creneau.objects.create(creneau=lib, ordre=o, duree=1.5) for lib, o in CRENEAUX}
    types = {t: Seance.objects.create(type_seance=t) for t in ('CM', 'TD', 'TP', 'DS', 'EF', 'ER')}
    types['IM'] = Seance.objects.create(type_seance='Instruction militaire', is_special=True)

    def classe(niveau, groupe):
        return Classe.objects.create(institution=inst, niveau=niveau, groupe=groupe,
                                     annee_universitaire=ANNEE)

    classes = {'MPSI A': classe(mpsi, 'A'), 'MPSI B': classe(mpsi, 'B'),
               'MP A': classe(mp, 'A'), 'MPSI': classe(mpsi, '')}
    sg = {g: SousGroupeTP.objects.create(classe=classes['MPSI A'], groupe=g) for g in ('A1', 'A2')}
    matieres = {code: Matiere.objects.create(institution=inst, code=code, intitule=code.title(),
                                             code_semestre='S1', niveau_ref=ref_mpsi,
                                             coefficient=Decimal('2'))
                for code in ('MATH', 'PHYS', 'INFO')}
    profs = {n: Prof.objects.create(nom=n, NNI=1000 + i) for i, n in enumerate(('SEJAD', 'SIDI'))}
    salles = {n: Salle.objects.create(nom=n) for n in ('101', '102', 'Labo 1', 'Labo 2')}
    return dict(inst=inst, semaine=semaine, jours=jours, creneaux=creneaux, types=types,
                classes=classes, sg=sg, matieres=matieres, profs=profs, salles=salles)


def seance(monde, classe='MPSI A', sous_groupe=None, matiere='MATH', type_='CM',
           creneau='08h00-09h30', prof='SEJAD', salle='101', jour='Lundi', annulee=False, date=None):
    from apps.ipgei.models import SeanceReelle
    index = ('Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi').index(jour)
    return SeanceReelle.objects.create(
        classe=monde['classes'][classe], semaine=monde['semaine'], jour=monde['jours'][jour],
        creneau=monde['creneaux'][creneau] if isinstance(creneau, str) else creneau,
        matiere=monde['matieres'][matiere] if matiere else None,
        prof=monde['profs'][prof] if prof else None,
        salle=monde['salles'][salle] if salle else None,
        sous_groupe=monde['sg'][sous_groupe] if sous_groupe else None,
        type_seance=monde['types'][type_], annulee=annulee,
        date=date or LUNDI + dt.timedelta(days=index))


def suivi(monde, s, **autres):
    """La ligne de suivi que la génération tire de la séance (miroir.projeter_semaine_reelle)."""
    from apps.suivi.models import Suivie
    s.matiere.refresh_from_db()
    valeurs = dict(
        annee_universitaire=ANNEE, type_semestre='I', numero_semaine=5, date_suivie=s.date,
        departement_id=s.sous_groupe_id or s.classe_id, em_id=s.matiere.em_miroir_id,
        creneau_fk=s.creneau, type_seance_fk=s.type_seance, jour_fk=s.jour,
        institution=monde['inst'], prof=s.prof, salle=s.salle)
    valeurs.update(autres)
    return Suivie.objects.create(**valeurs)


def journee(client, date=LUNDI):
    r = client.get(URL, {'annee_universitaire': ANNEE, 'date': date.isoformat()})
    assert r.status_code == 200, getattr(r, 'data', r)
    return r.data


def case(j, monde, unite, creneau='08h00-09h30'):
    dep = monde['sg'][unite] if unite in monde['sg'] else monde['classes'][unite]
    return j['cases'].get(f"{dep.pk}:{monde['creneaux'][creneau].pk}")


@pytest.fixture
def admin(db):
    return UserFactory(username='adm', role='admin', is_superuser=True)


# ── Droits ───────────────────────────────────────────────────────────────────
class TestAcces:

    @pytest.mark.parametrize('role', ['DA', 'AA'])
    def test_da_et_aa_par_defaut(self, monde, role):
        assert api(UserFactory(role=role)).get(
            URL, {'annee_universitaire': ANNEE, 'date': LUNDI.isoformat()}).status_code == 200

    def test_un_autre_role_par_la_matrice(self, monde):
        from core.permissions import invalidate_role_rbac
        de = UserFactory(role='DE')
        p = {'annee_universitaire': ANNEE, 'date': LUNDI.isoformat()}
        assert api(de).get(URL, p).status_code == 403          # pas par défaut
        droit('DE', 'surveillance', 'voir')
        # Le refus est en cache : c'est la matrice qui le périme.
        invalidate_role_rbac('DE')
        assert api(de).get(URL, p).status_code == 200

    def test_admin(self, monde):
        assert api(UserFactory(role='admin')).get(
            URL, {'annee_universitaire': ANNEE, 'date': LUNDI.isoformat()}).status_code == 200

    @pytest.mark.parametrize('role', ['DE', 'DG', 'IT', 'scolarite', 'chef_departement',
                                      'enseignant', 'etudiant'])
    def test_les_autres_non(self, monde, role):
        assert api(UserFactory(role=role)).get(
            URL, {'annee_universitaire': ANNEE, 'date': LUNDI.isoformat()}).status_code == 403

    def test_le_pdf_aussi(self, monde):
        r = api(UserFactory(role='scolarite')).get(
            URL + 'pdf/', {'annee_universitaire': ANNEE, 'date': LUNDI.isoformat()})
        assert r.status_code == 403

    def test_anonyme(self, monde):
        assert APIClient().get(URL, {'annee_universitaire': ANNEE,
                                     'date': LUNDI.isoformat()}).status_code in (401, 403)

    def test_date_obligatoire(self, admin):
        assert api(admin).get(URL, {'annee_universitaire': ANNEE}).status_code == 400
        assert api(admin).get(URL, {'annee_universitaire': ANNEE, 'date': '13/10/2025'}).status_code == 400


def test_la_migration_cree_le_module_pour_da_et_aa(db, settings):
    """La migration 0026 elle-même : le module, l'action « voir », DA et AA —
    et elle n'ajoute rien de plus quand on la rejoue."""
    import importlib

    from django.apps import apps as django_apps

    from apps.authentication.models import Module, RoleDefault
    Module.objects.filter(code='surveillance').delete()     # posé par la fixture
    settings.SEEDS_DESACTIVES_POUR_TESTS = False
    m = importlib.import_module('apps.authentication.migrations.0026_module_surveillance')
    m.ajouter(django_apps, None)
    avant = RoleDefault.objects.count()
    m.ajouter(django_apps, None)                                  # idempotente
    assert RoleDefault.objects.count() == avant
    mod = Module.objects.get(code='surveillance')
    assert (mod.nom, mod.icone) == ('Vue de surveillance', 'Eye')
    roles = set(RoleDefault.objects.filter(module_action__module__code='surveillance',
                                           module_action__action__code='voir', allowed=True)
                .values_list('role', flat=True))
    assert roles == {'DA', 'AA'}


def test_la_migration_respecte_les_seeds_desactives(db, settings):
    import importlib

    from django.apps import apps as django_apps

    from apps.authentication.models import Module
    Module.objects.filter(code='surveillance').delete()     # posé par la fixture
    settings.SEEDS_DESACTIVES_POUR_TESTS = True
    m = importlib.import_module('apps.authentication.migrations.0026_module_surveillance')
    m.ajouter(django_apps, None)
    assert not Module.objects.filter(code='surveillance').exists()


# ── La journée ───────────────────────────────────────────────────────────────
class TestLaJournee:

    def test_la_case_dit_ou_et_avec_qui(self, monde, admin):
        s = seance(monde, matiere='PHYS', type_='TD', salle='102', prof='SIDI')
        j = journee(api(admin))
        c = case(j, monde, 'MPSI A')
        assert (c['salle'], c['type'], c['em_code'], c['prof'], c['seance']) == \
               ('102', 'TD', 'PHYS', 'SIDI', s.pk)
        assert c['evaluation'] is False and c['annulee'] is False
        assert c['reunis'] == [] and c['paralleles'] == [] and c['herite'] is False
        assert j['jour'] == 'Lundi' and j['numero_semaine'] == 5 and j['type_semestre'] == 'I'
        assert j['dans_le_calendrier'] is True

    def test_les_lignes_classes_par_niveau_et_leurs_sous_groupes(self, monde, admin):
        # A1 a une séance un AUTRE jour : il a sa ligne. A2 n'en a aucune : il
        # répéterait sa classe. La classe d'attente « MPSI » n'en a pas.
        seance(monde, sous_groupe='A1', matiere='INFO', type_='TP', jour='Mardi')
        j = journee(api(admin))
        assert [(g['libelle'], g['sous_groupe']) for g in j['groupes']] == [
            ('MPSI A', False), ('A1', True), ('MPSI B', False), ('MP A', False)]
        assert [g['niveau'] for g in j['groupes']] == ['MPSI', 'MPSI', 'MPSI', 'MP']
        assert j['groupes'][0]['niveau_libelle'] == 'MPSI — 1re année'
        assert j['groupes'][3]['niveau_libelle'] == 'MP — 2e année'
        assert j['groupes'][1]['classe'] == monde['classes']['MPSI A'].pk

    def test_le_sous_groupe_suit_sa_classe(self, monde, admin):
        seance(monde, matiere='MATH', type_='CM')                       # classe entière
        seance(monde, sous_groupe='A1', matiere='INFO', type_='TP', salle='Labo 1',
               creneau='09h45-11h15')
        j = journee(api(admin))
        herite = case(j, monde, 'A1')
        assert herite['herite'] is True and herite['em_code'] == 'MATH' and herite['salle'] == '101'
        propre = case(j, monde, 'A1', '09h45-11h15')
        assert propre['herite'] is False and propre['em_code'] == 'INFO' and propre['salle'] == 'Labo 1'

    def test_la_classe_eclatee_dit_ou_est_chaque_sous_groupe(self, monde, admin):
        seance(monde, sous_groupe='A1', matiere='INFO', type_='TP', salle='Labo 1')
        seance(monde, sous_groupe='A2', matiere='PHYS', type_='TP', salle='Labo 2', prof='SIDI')
        j = journee(api(admin))
        c = case(j, monde, 'MPSI A')
        assert c['dedouble'] is True and c['pointable'] is False
        assert [(s['groupe'], s['salle'], s['em_code']) for s in c['sous_groupes']] == [
            ('A1', 'Labo 1', 'INFO'), ('A2', 'Labo 2', 'PHYS')]
        # Chaque sous-groupe voit SA séance, pas celle de l'autre.
        assert case(j, monde, 'A1')['em_code'] == 'INFO'
        assert case(j, monde, 'A2')['em_code'] == 'PHYS'

    def test_reunis_dans_la_meme_salle(self, monde, admin):
        seance(monde, classe='MPSI A', salle='101')
        seance(monde, classe='MPSI B', salle='101')
        j = journee(api(admin))
        assert case(j, monde, 'MPSI A')['reunis'] == ['MPSI B']
        assert case(j, monde, 'MPSI B')['reunis'] == ['MPSI A']
        assert case(j, monde, 'MPSI A')['paralleles'] == []

    def test_en_parallele_dans_une_autre_salle(self, monde, admin):
        seance(monde, sous_groupe='A1', matiere='INFO', type_='TP', salle='Labo 1')
        seance(monde, sous_groupe='A2', matiere='INFO', type_='TP', salle='Labo 2')
        j = journee(api(admin))
        assert case(j, monde, 'A1')['paralleles'] == [{'groupe': 'MPSI A — A2', 'salle': 'Labo 2'}]
        assert case(j, monde, 'A1')['reunis'] == []

    def test_un_autre_enseignant_n_est_pas_le_meme_cours(self, monde, admin):
        seance(monde, classe='MPSI A', prof='SEJAD')
        seance(monde, classe='MPSI B', prof='SIDI')
        c = case(journee(api(admin)), monde, 'MPSI A')
        assert c['reunis'] == [] and c['paralleles'] == []

    def test_seance_annulee_le_groupe_est_libre(self, monde, admin):
        seance(monde, annulee=True)
        c = case(journee(api(admin)), monde, 'MPSI A')
        assert c['annulee'] is True and c['pointable'] is False and c['motif'] == ''

    def test_cours_de_classe_annule_tp_d_un_sous_groupe_maintenu(self, monde, admin):
        seance(monde, matiere='MATH', annulee=True)
        seance(monde, sous_groupe='A1', matiere='INFO', type_='TP', salle='Labo 1')
        j = journee(api(admin))
        classe = case(j, monde, 'MPSI A')
        assert classe['annulee'] is True and classe['doublon'] is False
        assert [s['groupe'] for s in classe['sous_groupes']] == ['A1']
        assert case(j, monde, 'A1')['em_code'] == 'INFO'        # A1 a cours
        seance(monde, sous_groupe='A2', matiere='PHYS', type_='TP', jour='Mardi')
        assert case(journee(api(admin)), monde, 'A2')['annulee'] is True   # A2 est libre

    def test_examen_sans_surveillant_designe(self, monde, admin):
        from apps.ipgei.models import (EpreuveClasse, EpreuveIPGEI, SessionEvaluationIPGEI)
        session = SessionEvaluationIPGEI.objects.create(
            semestre=monde['semaine'].semestre, type_session=SessionEvaluationIPGEI.TYPE_NORMALE)
        ep = EpreuveIPGEI.objects.create(session=session, matiere=monde['matieres']['MATH'],
                                         type_epreuve='DS', date=LUNDI,
                                         heure_debut=dt.time(8, 0), duree_minutes=180)
        EpreuveClasse.objects.create(epreuve=ep, classe=monde['classes']['MPSI A'])
        EpreuveClasse.objects.create(epreuve=ep, classe=monde['classes']['MPSI B'],
                                     salle=monde['salles']['102'])
        j = journee(api(admin))
        c = case(j, monde, 'MPSI A')
        assert c['evaluation'] is True and c['epreuve'] == ep.pk and c['prof'] == ''
        assert c['horaire'] == '08h00-11h00' and c['type'] == 'DS'
        # Trois heures : la classe compose aussi sur le créneau suivant.
        assert case(j, monde, 'MPSI A', '09h45-11h15')['epreuve'] == ep.pk
        assert case(j, monde, 'MPSI A', '11h30-13h00') is None
        assert c['paralleles'] == [{'groupe': 'MPSI B', 'salle': '102'}]
        # Les sous-groupes composent avec leur classe.
        seance(monde, sous_groupe='A1', matiere='INFO', type_='TP', jour='Mardi')
        assert case(journee(api(admin)), monde, 'A1')['herite'] is True

    def test_la_seance_de_ds_au_planning_est_une_evaluation(self, monde, admin):
        seance(monde, type_='DS', prof=None)
        c = case(journee(api(admin)), monde, 'MPSI A')
        assert c['evaluation'] is True and c['prof'] == ''

    def test_seance_speciale(self, monde, admin):
        seance(monde, matiere=None, type_='IM', prof=None, salle=None)
        c = case(journee(api(admin)), monde, 'MPSI A')
        assert c['special'] is True and c['type'] == 'Instruction militaire'

    def test_creneau_inactif_utilise_ce_jour_la(self, monde, admin):
        from apps.parametres.models import Creneau
        utilise = Creneau.objects.create(creneau='15h00-17h00', ordre=4, duree=2, is_actif=False)
        Creneau.objects.create(creneau='18h00-20h00', ordre=6, duree=2, is_actif=False)
        seance(monde, creneau=utilise)
        j = journee(api(admin))
        libelles = [(c['libelle'], c['inactif']) for c in j['creneaux']]
        assert ('15h00-17h00', True) in libelles
        assert '18h00-20h00' not in [l for l, _ in libelles]       # inactif et inutilisé
        assert [l for l, _ in libelles] == sorted(
            [l for l, _ in libelles], key=lambda l: int(l[:2]) * 60 + int(l[3:5]))
        assert j['cases'][f"{monde['classes']['MPSI A'].pk}:{utilise.pk}"]['em_code'] == 'MATH'

    def test_rien_n_est_ecrit(self, monde, admin):
        from apps.ipgei.models import SeanceReelle
        from apps.suivi.models import Suivie
        s = seance(monde)
        suivi(monde, s)
        avant = (list(SeanceReelle.objects.values()), list(Suivie.objects.values()))
        with CaptureQueriesContext(connection) as requetes:
            journee(api(admin))
        assert (list(SeanceReelle.objects.values()), list(Suivie.objects.values())) == avant
        ecritures = [q['sql'] for q in requetes.captured_queries
                     if q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
        assert ecritures == []

    def test_requetes_en_nombre_constant(self, monde):
        """Pas de N+1 : dix fois plus de séances, même nombre de requêtes."""
        from apps.ipgei.views_surveillance import VueSurveillanceView
        da = UserFactory(role='DE')
        for m, a in DROITS_SAISIE + [('surveillance', 'voir')]:
            droit('DE', m, a)
        da.managed_departements.add(monde['classes']['MPSI A'])

        def mesure():
            from django.core.cache import cache
            cache.clear()
            with CaptureQueriesContext(connection) as q:
                journee(api(da))
            return len(q.captured_queries)

        s = seance(monde)
        suivi(monde, s)
        une = mesure()
        for i, (lib, _) in enumerate(CRENEAUX[1:]):
            for classe in ('MPSI A', 'MPSI B', 'MP A'):
                x = seance(monde, classe=classe, creneau=lib, matiere=('PHYS', 'INFO')[i % 2],
                           salle=('101', '102', 'Labo 1')[('MPSI A', 'MPSI B', 'MP A').index(classe)])
                suivi(monde, x)
        seance(monde, sous_groupe='A1', matiere='INFO', type_='TP', jour='Mardi')
        assert mesure() == une
        assert VueSurveillanceView.required_module == 'surveillance'


# ── Les absences depuis la vue ───────────────────────────────────────────────
class TestLesAbsencesDepuisLaVue:
    """L'appel d'une séance (`/dashboard/absences/saisir/salle/<suivi>`) lit
    tout en une requête, `presences/saisie-seance/` — institution principale,
    module abs_saisie — et s'enregistre par `presences/bulk/` (abs_saisie :
    modifier). La vue ne propose le lien que là où il s'ouvrira.

    Depuis le 17/10/2026 : plus de délégation par groupe (elle venait de
    `/suivi/suivies/`, que l'écran ne lit plus), et un TP s'ouvre — sa liste
    est celle du sous-groupe (`apps/ipgei/liste_appel.py`)."""

    @pytest.fixture
    def scolarite(self, monde):
        """Fait l'appel sans saisir le suivi ni avoir de groupe délégué."""
        u = UserFactory(role='scolarite')
        for m, a in DROITS_SAISIE + [('surveillance', 'voir')]:
            droit('scolarite', m, a)
        return u

    def test_sans_delegation_la_seance_s_ouvre_comme_a_l_ecran(self, monde, scolarite):
        a = suivi(monde, seance(monde, classe='MPSI A'))
        b = suivi(monde, seance(monde, classe='MPSI B', creneau='09h45-11h15'))
        j = journee(api(scolarite))
        ca, cb = case(j, monde, 'MPSI A'), case(j, monde, 'MPSI B', '09h45-11h15')
        assert (ca['pointable'], ca['suivi']) == (True, a.pk)
        assert (cb['pointable'], cb['suivi']) == (True, b.pk)
        # Et c'est bien la règle de l'écran d'appel :
        for s in (a, b):
            r = api(scolarite).get('/api/v1/absences/presences/saisie-seance/', {'seance': s.pk})
            assert r.status_code == 200

    def test_le_role_admin_ouvre_tout(self, monde):
        suivi(monde, seance(monde))
        adm = UserFactory(role='admin', is_superuser=False)
        assert case(journee(api(adm)), monde, 'MPSI A')['pointable'] is True

    def test_le_superutilisateur_ouvre_tout(self, monde, admin):
        suivi(monde, seance(monde, classe='MPSI A'))
        suivi(monde, seance(monde, classe='MP A', creneau='09h45-11h15'))
        j = journee(api(admin))
        assert case(j, monde, 'MPSI A')['pointable'] and case(j, monde, 'MP A', '09h45-11h15')['pointable']

    @pytest.mark.parametrize('manque', DROITS_SAISIE)
    def test_il_faut_tous_les_droits_que_l_ecran_lit(self, monde, manque):
        from apps.authentication.models import RoleDefault
        u = UserFactory(role='DE')
        for m, a in DROITS_SAISIE + [('surveillance', 'voir')]:
            droit('DE', m, a)
        RoleDefault.objects.filter(role='DE', module_action__module__code=manque[0],
                                   module_action__action__code=manque[1]).delete()
        suivi(monde, seance(monde))
        j = journee(api(u))
        assert j['droits_saisie'] is False
        assert case(j, monde, 'MPSI A')['pointable'] is False

    def test_les_droits_sont_ceux_des_routes_d_appel(self):
        from apps.ipgei.surveillance import DROITS_SAISIE as DROITS
        assert set(DROITS) == {('abs_saisie', 'voir'), ('abs_saisie', 'modifier')}

    def test_da_par_defaut_n_a_que_la_lecture(self, monde):
        """Le DA voit la vue, mais `abs_saisie : modifier` ne lui est pas donné
        par défaut : pas de lien."""
        suivi(monde, seance(monde))
        j = journee(api(UserFactory(role='DA')))
        assert j['droits_saisie'] is False and case(j, monde, 'MPSI A')['pointable'] is False

    def test_hors_institution_principale(self, monde, admin):
        from apps.parametres.models import Institution
        autre = Institution.objects.create(acronyme='AUTRE', nom='Autre', est_principale=False)
        a = suivi(monde, seance(monde), institution=autre)
        c = case(journee(api(admin)), monde, 'MPSI A')
        assert c['pointable'] is False and c['motif'] == "Hors de l'institution principale"
        assert c['appel'] is None
        r = api(admin).get('/api/v1/absences/presences/saisie-seance/', {'seance': a.pk})
        assert r.status_code == 404

    def test_sans_ligne_de_suivi(self, monde, admin):
        seance(monde)
        c = case(journee(api(admin)), monde, 'MPSI A')
        assert (c['pointable'], c['suivi'], c['motif'], c['appel']) == (False, None, 'Suivi non généré', None)

    def test_le_sous_groupe_s_ouvre_sur_sa_liste(self, monde, admin):
        """Le TP s'ouvre désormais : l'écran appelle le sous-groupe."""
        tp = suivi(monde, seance(monde, sous_groupe='A1', matiere='INFO', type_='TP'))
        cm = suivi(monde, seance(monde, matiere='MATH', creneau='09h45-11h15'))
        j = journee(api(admin))
        c = case(j, monde, 'A1')
        assert (c['pointable'], c['suivi'], c['motif']) == (True, tp.pk, '')
        herite = case(j, monde, 'A1', '09h45-11h15')
        assert (herite['herite'], herite['pointable'], herite['suivi']) == (True, True, cm.pk)

    def test_jamais_pour_une_journee_a_venir(self, monde, admin):
        demain = dt.date.today() + dt.timedelta(days=1)
        s = seance(monde, date=demain)
        suivi(monde, s)
        j = journee(api(admin), demain)
        assert j['a_venir'] is True
        c = case(j, monde, 'MPSI A')
        assert c['pointable'] is False and c['appel'] is None

    def test_la_cle_est_celle_de_la_vraie_generation_du_suivi(self, monde, admin):
        """La génération réelle (« Ajouter un suivi ») : chaque case retrouve
        SA ligne — séance de classe, TP de sous-groupe, épreuve."""
        from rest_framework.test import APIRequestFactory, force_authenticate

        from apps.ipgei.models import EpreuveClasse, EpreuveIPGEI, SessionEvaluationIPGEI
        from apps.suivi.models import Suivie
        from apps.suivi.views import SuivieViewSet

        seance(monde, matiere='MATH', type_='CM')
        seance(monde, sous_groupe='A1', matiere='INFO', type_='TP', creneau='09h45-11h15')
        session = SessionEvaluationIPGEI.objects.create(
            semestre=monde['semaine'].semestre, type_session=SessionEvaluationIPGEI.TYPE_NORMALE)
        ep = EpreuveIPGEI.objects.create(session=session, matiere=monde['matieres']['PHYS'],
                                         type_epreuve='DS', date=LUNDI,
                                         heure_debut=dt.time(15, 10), duree_minutes=120)
        EpreuveClasse.objects.create(epreuve=ep, classe=monde['classes']['MPSI A'])

        vue = SuivieViewSet.as_view({'post': 'ajouter_suivie'})
        demande = APIRequestFactory().post('/api/v1/suivi/suivies/ajouter/', {
            'annee_universitaire': ANNEE, 'numero_semaine': 5, 'type_semestre': 'I'}, format='json')
        force_authenticate(demande, user=admin)
        assert vue(demande).status_code in (200, 201)

        j = journee(api(admin))
        cm, tp = case(j, monde, 'MPSI A'), case(j, monde, 'A1', '09h45-11h15')
        ds = case(j, monde, 'MPSI A', '15h10-16h40')
        for c in (cm, tp, ds):
            assert c['suivi'] is not None
        assert Suivie.objects.get(pk=cm['suivi']).departement_id == monde['classes']['MPSI A'].pk
        assert Suivie.objects.get(pk=tp['suivi']).departement_id == monde['sg']['A1'].pk
        assert Suivie.objects.get(pk=ds['suivi']).type_seance_fk.type_seance == 'DS'
        assert cm['pointable'] and ds['pointable'] and tp['pointable']


# ── L'état de l'appel ────────────────────────────────────────────────────────
class TestLEtatDeLAppel:
    """Chaque case porte l'état de l'appel de sa séance (17/10/2026) : la
    direction voit d'un coup d'œil où l'appel n'a pas été fait."""

    def test_appel_a_faire_puis_fait(self, monde, admin):
        from apps.absence.models import Etudiant, Presence
        s = suivi(monde, seance(monde))
        c = case(journee(api(admin)), monde, 'MPSI A')
        assert c['appel'] == {'saisi': False, 'presents': 0, 'absents': 0,
                              'sanctions': 0, 'justifiees': 0}
        for i, statut in enumerate((0, 0, 1, 2, 3)):
            Presence.objects.create(suivi=s, statut=statut, etudiant=Etudiant.objects.create(
                matricule=f'77{i}', nom=f'E{i}'))
        c = case(journee(api(admin)), monde, 'MPSI A')
        assert c['appel'] == {'saisi': True, 'presents': 2, 'absents': 1,
                              'sanctions': 1, 'justifiees': 1}

    def test_visible_sans_droit_de_saisie(self, monde):
        """Le DA surveille sans faire l'appel : il voit où il manque."""
        suivi(monde, seance(monde))
        j = journee(api(UserFactory(role='DA')))
        c = case(j, monde, 'MPSI A')
        assert c['pointable'] is False and c['appel']['saisi'] is False

    def test_rien_pour_une_epreuve_une_seance_speciale_ou_annulee(self, monde, admin):
        suivi(monde, seance(monde, matiere='PHYS', type_='DS'))
        # Une séance spéciale n'a pas de matière (le modèle la retire) : sans
        # élément, pas de ligne de suivi qui la désigne.
        seance(monde, matiere=None, type_='IM', prof=None, salle=None, creneau='09h45-11h15')
        suivi(monde, seance(monde, creneau='11h30-13h00', annulee=True))
        j = journee(api(admin))
        for creneau in ('08h00-09h30', '09h45-11h15', '11h30-13h00'):
            assert case(j, monde, 'MPSI A', creneau)['appel'] is None

    def test_la_case_heritee_porte_l_appel_de_sa_classe(self, monde, admin):
        from apps.absence.models import Etudiant, Presence
        s = suivi(monde, seance(monde))
        seance(monde, sous_groupe='A1', matiere='INFO', type_='TP', jour='Mardi')
        Presence.objects.create(suivi=s, statut=1, etudiant=Etudiant.objects.create(
            matricule='880', nom='E'))
        j = journee(api(admin))
        assert case(j, monde, 'A1')['herite'] is True
        assert case(j, monde, 'A1')['appel'] == case(j, monde, 'MPSI A')['appel']
        assert case(j, monde, 'A1')['appel']['saisi'] is True

    def test_rien_n_est_ecrit_et_le_pdf_ne_porte_pas_de_cle_interne(self, monde, admin):
        suivi(monde, seance(monde))
        j = journee(api(admin))
        assert all('_suivi' not in c for c in j['cases'].values())


# ── Le PDF ───────────────────────────────────────────────────────────────────
class TestLePDF:

    def test_journee_et_semaine(self, monde, admin, monkeypatch):
        """Le gabarit se rend sur les données de la journée et de la semaine
        (sans wkhtmltopdf : c'est le contenu qui est vérifié)."""
        from django.http import HttpResponse
        from django.template.loader import render_to_string

        import apps.ipgei.edt_pdf as edt_pdf
        from core.pdf_utils import get_institution_context
        vus = []

        def faux(template, contexte, fichier):
            contexte = {**contexte, **get_institution_context()}   # comme le vrai rendu
            vus.append((fichier, render_to_string(template, contexte)))
            return HttpResponse(b'%PDF', content_type='application/pdf')

        monkeypatch.setattr(edt_pdf, 'reponse_pdf', faux)
        seance(monde, sous_groupe='A1', matiere='INFO', type_='TP', salle='Labo 1')
        seance(monde, sous_groupe='A2', matiere='INFO', type_='TP', salle='Labo 2')
        seance(monde, classe='MPSI B', annulee=True)
        seance(monde, classe='MP A', type_='DS', prof=None, creneau='09h45-11h15')
        c = api(admin)
        for params in ({'date': LUNDI.isoformat()}, {'type_semestre': 'I', 'numero_semaine': 5}):
            r = c.get(URL + 'pdf/', {'annee_universitaire': ANNEE, **params})
            assert r.status_code == 200, getattr(r, 'data', r.content[:300])
        (f1, html1), (f2, html2) = vus
        assert f1 == 'surveillance_2025-10-13.pdf'
        assert 'Vue de surveillance — Lundi 13/10/2025' in html1
        assert 'EN PARALLÈLE : MPSI A — A2 (Labo 2)' in html1
        assert 'TP en sous-groupes' in html1 and 'ANNULÉE' in html1
        assert 'Aucun surveillant désigné' in html1
        assert 'MPSI — 1re année' in html1
        assert '{%' not in html1 and '{#' not in html1
        assert f2 == f'surveillance_{ANNEE}_semaine_5.pdf'
        assert html2.count('Vue de surveillance —') == 6      # lundi → samedi

    def test_semaine_inconnue(self, monde, admin):
        r = api(admin).get(URL + 'pdf/', {'annee_universitaire': ANNEE,
                                          'type_semestre': 'I', 'numero_semaine': 42})
        assert r.status_code == 404

    def test_parametres_de_semaine_controles(self, monde, admin):
        r = api(admin).get(URL + 'pdf/', {'annee_universitaire': ANNEE,
                                          'type_semestre': 'X', 'numero_semaine': 5})
        assert r.status_code == 400
