"""
Test — « Avancement par semestre », classe par classe (14/10/2026).

L'ancien calcul rapportait les heures du professeur le plus chargé de chaque
matière au volume de la maquette compté UNE fois. Sur la base locale, simulée
avec les séances écoulées comme faites, S3 2026-2027 affichait 60,7 % de CM
quand chaque classe de MP en était à 28 % : le professeur de français fait ses
CM séparément dans les quatre classes, et ses 42 h comptaient face à 21 h.

Le prévu vient désormais du référentiel — chaque classe de l'année doit la
maquette de son niveau, au volume gelé — et le fait des séances « Fait »,
comptées pour chaque classe présente. Sans plafond. Voir
apps/avancement/semestres.py.

Base SQLite en mémoire (siga.settings.test).
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.em.models import EM, VolumeAnnuelEM
from apps.ipgei.models import Classe, Matiere, NiveauIPGEI, SousGroupeTP
from apps.parametres.models import Creneau, Institution, Jour, Niveau, Seance
from apps.prof.models import Prof, ProfTypeHistory
from apps.suivi.models import Suivie, SuiviePointage, SuiviePointageDepartement

ANNEE = '2025-2026'
URL = '/api/v1/avancement/semestres/'


class _Monde(TestCase):
    @classmethod
    def setUpClass(cls):
        # Même précaution que les tests voisins : ces tables de liaison peuvent
        # déjà exister selon la fixture de session.
        existantes = set(connection.introspection.table_names())
        for modele in (ProfTypeHistory, SuiviePointageDepartement):
            if modele._meta.db_table not in existantes:
                with connection.schema_editor() as se:
                    se.create_model(modele)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.mpsi = Niveau.objects.create(niveau='MPSI')
        cls.mp = Niveau.objects.create(niveau='MP')
        cls.ref = {
            'MPSI': NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI',
                                               libelle='MPSI', rang=1),
            'MP':   NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                               libelle='MP', rang=2),
        }
        cls.a = cls._classe('A')
        cls.b = cls._classe('B')
        cls.types = {t: Seance.objects.create(type_seance=t)
                     for t in ('CM', 'TD', 'TP', 'DS', 'EF')}
        cls.jour = Jour.objects.create(jour='Lundi')
        cls.creneaux = [Creneau.objects.create(creneau=f'C{i}', ordre=i, duree=1.5)
                        for i in range(1, 5)]
        cls.profs = [Prof.objects.create(NNI=880000 + i, nom=f'P{i}', type='permanent')
                     for i in range(2)]
        cls.admin = get_user_model().objects.create_user(
            username='admin_semestres', email='semestres@test.local',
            password='Xk93!plqz72', role='admin')

    @classmethod
    def _classe(cls, groupe, niveau='MPSI', annee=ANNEE):
        return Classe.objects.create(
            institution=cls.inst, groupe=groupe, annee_universitaire=annee,
            niveau=cls.mpsi if niveau == 'MPSI' else cls.mp)

    def matiere(self, code='MATH', semestre='S1', cm=0, td=0, tp=0, actif=True):
        """Une matière de la maquette ; le signal lui crée son EM miroir."""
        m = Matiere.objects.create(
            institution=self.inst, code=code, intitule=code, code_semestre=semestre,
            niveau_ref=self.ref['MPSI' if semestre in ('S1', 'S2') else 'MP'],
            coefficient=Decimal('1'), volume_cm=cm, volume_td=td, volume_tp=tp,
            actif=actif)
        m.refresh_from_db()
        self.assertIsNotNone(m.em_miroir, 'le miroir EM doit exister')
        return m.em_miroir

    def faire(self, groupes, em, type_='CM', fait=True, creneau=0, semaine=1,
              prof=0, ts='I', annee=ANNEE):
        """Une séance pointée de 1,5 h, pour un ou plusieurs groupes."""
        p = SuiviePointage.objects.create(
            institution=self.inst, annee_universitaire=annee, type_semestre=ts,
            numero_semaine=semaine, em=em, semestre=em.semestre,
            type_seance_fk=self.types[type_], jour_fk=self.jour,
            creneau_fk=self.creneaux[creneau], prof=self.profs[prof],
            duree_creneau=1.5, commentaire='Fait' if fait else 'Non fait')
        p.departements.set(groupes)
        return p

    def page(self, semestres='Impairs', annee=ANNEE):
        client = APIClient()
        client.force_authenticate(self.admin)
        r = client.get(URL, {'annee_universitaire': annee, 'semestres': semestres})
        self.assertEqual(r.status_code, 200, r.data)
        return {l: {'CM': r.data['progress_cm'][i], 'TD': r.data['progress_td'][i],
                    'TP': r.data['progress_tp'][i], 'PR': r.data['progress_pr'][i]}
                for i, l in enumerate(r.data['labels'])}


class TestLePrevu(_Monde):

    def test_chaque_classe_doit_toute_la_maquette(self):
        """Le prévu est compté par classe : deux classes, deux fois le volume."""
        math = self.matiere(cm=3)
        self.faire([self.a], math)
        self.assertEqual(self.page()['S1']['CM'], 25.0)      # 1,5 / (2 × 3)

    def test_une_matiere_pas_encore_planifiee_compte_au_prevu(self):
        math = self.matiere(cm=3)
        self.matiere('PHYS', cm=3)                            # rien de planifié
        self.faire([self.a, self.b], math)
        self.assertEqual(self.page()['S1']['CM'], 25.0)      # 2 × 1,5 / 2 × (3 + 3)

    def test_la_maquette_d_un_autre_niveau_ne_compte_pas(self):
        """L'équivalent IPGEI des « copies non suivies » : S3 est la maquette de
        MP, une classe de MPSI ne la doit pas — et S3 n'apparaît pas sans
        classe de MP cette année."""
        math = self.matiere(cm=3)
        self.matiere('MATH', semestre='S3', cm=30)
        self.faire([self.a, self.b], math)
        page = self.page()
        self.assertEqual(list(page), ['S1'])
        self.assertEqual(page['S1']['CM'], 50.0)

    def test_le_catalogue_socle_et_les_matieres_inactives_restent_dehors(self):
        math = self.matiere(cm=3)
        self.matiere('PHYS', cm=30, actif=False)
        chim = EM.objects.create(code_em='CHIM-S1', intitule='Chimie', CM=30,
                                 semestre=math.semestre)
        self.faire([self.a, self.b], math)
        self.faire([self.a], chim, creneau=1)
        self.assertEqual(self.page()['S1']['CM'], 50.0)

    def test_le_volume_gele_de_l_annee_fait_foi(self):
        math = self.matiere(cm=3)
        VolumeAnnuelEM.objects.create(annee_universitaire=ANNEE, em=math,
                                      CM=6, TD=0, TP=0, PR=0)
        self.faire([self.a, self.b], math)
        self.assertEqual(self.page()['S1']['CM'], 25.0)      # 2 × 1,5 / 2 × 6

    def test_la_classe_d_attente_et_les_autres_annees_ne_comptent_pas(self):
        math = self.matiere(cm=3)
        self._classe('')                                      # conteneur « MPSI »
        self._classe('C', annee='2024-2025')
        self.faire([self.a, self.b], math)
        self.assertEqual(self.page()['S1']['CM'], 50.0)

    def test_un_semestre_sans_classe_reste_absent(self):
        self.matiere(cm=3)
        self.matiere('MATH', semestre='S3', cm=3)
        self.assertEqual(list(self.page()), ['S1'])

    def test_debut_de_semestre_a_zero(self):
        """Rien de pointé : le semestre figure, à zéro."""
        self.matiere(cm=3, td=6)
        self.assertEqual(self.page()['S1'], {'CM': 0.0, 'TD': 0.0, 'TP': 0, 'PR': 0})

    def test_pairs_et_impairs_separes(self):
        self.matiere(cm=3)
        s2 = self.matiere('MATH', semestre='S2', cm=3)
        self.faire([self.a, self.b], s2, ts='P')
        self.assertEqual(list(self.page('Impairs')), ['S1'])
        self.assertEqual(self.page('Pairs')['S2']['CM'], 50.0)


class TestLeFait(_Monde):

    def test_le_cm_commun_compte_pour_chaque_classe(self):
        math = self.matiere(cm=3)
        self.faire([self.a, self.b], math)
        self.assertEqual(self.page()['S1']['CM'], 50.0)      # 2 × 1,5 / 2 × 3

    def test_meme_enseignant_deux_classes_separement(self):
        """Le cas réel de S3 2026-2027 : l'ancien calcul additionnait les heures
        du professeur dans les deux classes face à un volume compté une fois —
        100 % ici devenait 200 %."""
        math = self.matiere(td=3)
        for classe in (self.a, self.b):
            for creneau in (0, 1):
                self.faire([classe], math, type_='TD', creneau=creneau)
        self.assertEqual(self.page()['S1']['TD'], 100.0)

    def test_deux_enseignants_differents_chacun_sa_classe(self):
        """L'ancien calcul ne retenait que le plus chargé : B, en retard, disparaissait."""
        math = self.matiere(cm=3)
        self.faire([self.a], math, prof=0)
        self.faire([self.a], math, prof=0, creneau=1)
        self.faire([self.b], math, prof=1)
        self.assertEqual(self.page()['S1']['CM'], 75.0)      # (3 + 1,5) / 6

    def test_deux_enseignants_sur_le_meme_creneau_une_seule_seance(self):
        math = self.matiere(cm=3)
        self.faire([self.a, self.b], math, prof=0)
        self.faire([self.a, self.b], math, prof=1)
        self.assertEqual(self.page()['S1']['CM'], 50.0)

    def test_sans_plafond(self):
        math = self.matiere(cm=3)
        for creneau in (0, 1, 2):
            self.faire([self.a, self.b], math, creneau=creneau)
        self.assertEqual(self.page()['S1']['CM'], 150.0)     # 4,5 / 3

    def test_non_fait_ne_compte_pas(self):
        math = self.matiere(cm=3)
        self.faire([self.a, self.b], math, fait=False)
        self.assertEqual(self.page()['S1']['CM'], 0)

    def test_une_autre_annee_ne_compte_pas(self):
        math = self.matiere(cm=3)
        self.faire([self.a, self.b], math, annee='2024-2025')
        self.assertEqual(self.page()['S1']['CM'], 0)

    def test_les_epreuves_n_avancent_pas_le_programme(self):
        """DS, EF et ER hors avancement, comme « par matière » et « par groupe »
        (750beed6) : deux devoirs suffisaient à annoncer un semestre bouclé."""
        math = self.matiere(td=3)
        self.faire([self.a, self.b], math, type_='TD')
        self.faire([self.a, self.b], math, type_='DS', creneau=1)
        self.faire([self.a, self.b], math, type_='EF', creneau=2)
        self.assertEqual(self.page()['S1']['TD'], 50.0)


class TestLesSousGroupes(_Monde):
    """Un TP dédoublé : chaque moitié doit tout le programme."""

    def setUp(self):
        self.a1 = SousGroupeTP.objects.create(classe=self.a, groupe='A1')
        self.a2 = SousGroupeTP.objects.create(classe=self.a, groupe='A2')

    def test_le_tp_dedouble_avance_la_classe_de_la_moyenne(self):
        """A1 et A2 ont chacun eu leur TP : la classe a fait UNE séance, pas deux."""
        info = self.matiere('INFO', tp=3)
        self.faire([self.a1], info, type_='TP', creneau=0)
        self.faire([self.a2], info, type_='TP', creneau=1)
        self.faire([self.b], info, type_='TP', creneau=2)
        self.assertEqual(self.page()['S1']['TP'], 50.0)      # (1,5 + 1,5) / (2 × 3)

    def test_une_moitie_en_retard_retient_la_classe(self):
        info = self.matiere('INFO', tp=3)
        self.faire([self.a1], info, type_='TP', creneau=0)
        self.faire([self.a1], info, type_='TP', creneau=1)
        self.faire([self.a2], info, type_='TP', creneau=2)  # A2 : une seule
        self.faire([self.b], info, type_='TP', creneau=0)
        self.faire([self.b], info, type_='TP', creneau=1)
        self.assertEqual(self.page()['S1']['TP'], 87.5)      # (2,25 + 3) / 6

    def test_le_sous_groupe_herite_des_cm_de_sa_classe(self):
        """Une classe dédoublée en TP suit ses CM entière : sans héritage, ses
        sous-groupes afficheraient 0 % de cours magistral."""
        info = self.matiere('INFO', cm=3, tp=3)
        self.faire([self.a1], info, type_='TP', creneau=0)
        self.faire([self.a2], info, type_='TP', creneau=1)
        self.faire([self.a, self.b], info, type_='CM', creneau=2)
        self.assertEqual(self.page()['S1']['CM'], 50.0)

    def test_un_sous_groupe_jamais_planifie_n_est_pas_un_groupe_en_retard(self):
        """Le cas réel « MPSI B — B1, B2 » : déclaré, sans séance ni inscrit."""
        SousGroupeTP.objects.create(classe=self.a, groupe='A1, A2')
        info = self.matiere('INFO', tp=3)
        self.faire([self.a1], info, type_='TP', creneau=0)
        self.faire([self.a2], info, type_='TP', creneau=1)
        self.faire([self.b], info, type_='TP', creneau=2)
        self.assertEqual(self.page()['S1']['TP'], 50.0)

    def test_un_sous_groupe_a_l_emploi_du_temps_compte_meme_sans_seance_faite(self):
        info = self.matiere('INFO', tp=3)
        Suivie.objects.create(annee_universitaire=ANNEE, type_semestre='I',
                              numero_semaine=1, institution=self.inst,
                              departement=self.a2, em=info, prof=self.profs[0],
                              jour_fk=self.jour, creneau_fk=self.creneaux[1],
                              type_seance_fk=self.types['TP'])
        self.faire([self.a1], info, type_='TP', creneau=0)
        self.faire([self.b], info, type_='TP', creneau=2)
        self.assertEqual(self.page()['S1']['TP'], 37.5)      # (0,75 + 1,5) / 6


class TestLaReponse(_Monde):

    def test_la_forme_ne_change_pas(self):
        """L'écran lit ces clés : elles restent celles d'avant."""
        self.matiere(cm=3)
        client = APIClient()
        client.force_authenticate(self.admin)
        r = client.get(URL, {'annee_universitaire': ANNEE, 'semestres': 'Impairs'})
        self.assertEqual(set(r.data), {'labels', 'progress_cm', 'progress_td',
                                       'progress_tp', 'progress_pr',
                                       'libelle_semestres', 'annee_univ'})
        self.assertEqual((r.data['libelle_semestres'], r.data['annee_univ']),
                         ('impairs', ANNEE))

    def test_annee_obligatoire(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        self.assertEqual(client.get(URL).status_code, 400)

    def test_rien_n_est_ecrit(self):
        math = self.matiere(cm=3)
        self.faire([self.a, self.b], math)

        def etat():
            return (list(Suivie.objects.values()), list(SuiviePointage.objects.values()),
                    list(EM.objects.values()), list(VolumeAnnuelEM.objects.values()),
                    list(Matiere.objects.values()))
        avant = etat()
        with CaptureQueriesContext(connection) as requetes:
            self.page()
        self.assertEqual(etat(), avant)
        ecritures = [q['sql'] for q in requetes.captured_queries
                     if q['sql'].lstrip().upper().startswith(
                         ('INSERT', 'UPDATE', 'DELETE'))]
        self.assertEqual(ecritures, [])
