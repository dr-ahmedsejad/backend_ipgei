"""
Test — avancement par GROUPE.

Ce que l'écran des matières ne peut pas dire : MPSI A a fait ses 4 h de TP,
MPSI B n'en a fait que 2, et la matière affiche 100 % parce qu'elle retient le
groupe le plus avancé. Le calcul par groupe doit, lui, montrer les deux taux —
et nommer la séance que B n'a pas eue.

Trois garanties vérifiées ici :
  1. chaque groupe a SON taux, et le chiffre « programme » reste le maximum ;
  2. une séance à VENIR n'est pas une séance manquée — sans quoi tout groupe
     serait en retard dès la première semaine ;
  3. `seances_ratees` nomme la séance : semaine, jour, créneau, enseignant.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date, timedelta

from django.db import connection
from django.test import TestCase

from apps.avancement.par_groupe import avancement_par_groupe, seances_ratees
from apps.departement.models import Departement
from apps.ipgei.models import Classe, SousGroupeTP
from apps.em.models import EM
from apps.parametres.models import Creneau, Institution, Jour, Niveau, Seance, Semestre
from apps.prof.models import Prof, ProfTypeHistory
from apps.suivi.models import SuiviePointage, SuiviePointageDepartement

HIER   = date.today() - timedelta(days=7)
DEMAIN = date.today() + timedelta(days=7)


class AvancementParGroupeTest(TestCase):
    @classmethod
    def setUpClass(cls):
        # Même précaution que les tests de `suivi` : la table de liaison peut
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
        cls.niveau = Niveau.objects.create(niveau='MPSI')
        cls.niveau_mp = Niveau.objects.create(niveau='MP')
        cls.semestre = Semestre.objects.create(
            code_semestre='S1', semestre='S1 — 1re année',
            niveau_semestre=cls.niveau, type_semestre='I')

        # De vraies classes IPGEI : le referentiel des groupes ne retient que
        # celles-la, un departement nu du socle n'y figure pas.
        cls.a = Classe.objects.create(nom='MPSI A', institution=cls.inst,
                                      annee_universitaire='2025-2026',
                                      niveau=cls.niveau, groupe='A')
        cls.b = Classe.objects.create(nom='MPSI B', institution=cls.inst,
                                      annee_universitaire='2025-2026',
                                      niveau=cls.niveau, groupe='B')

        # 4 h de TP au programme : deux séances de 2 h.
        cls.matiere = EM.objects.create(
            code_em='INFO-S1', intitule='Informatique', CM=0, TD=0, TP=4, PR=0,
            departement=cls.a, semestre=cls.semestre)

        cls.tp = Seance.objects.create(type_seance='TP')
        cls.cm = Seance.objects.create(type_seance='CM')
        cls.jour = Jour.objects.create(jour='Mardi')
        cls.creneau = Creneau.objects.create(creneau='10:00-12:00', ordre=1)
        cls.prof = Prof.objects.create(NNI=333333, nom='Prof TP', type='permanent')

    def _pointage(self, groupe, semaine, fait, quand, matiere=None, type_seance=None):
        sp = SuiviePointage.objects.create(
            institution=self.inst, annee_universitaire='2025-2026',
            numero_semaine=semaine, type_semestre='I',
            em=matiere or self.matiere,
            semestre=self.semestre, type_seance_fk=type_seance or self.tp,
            jour_fk=self.jour,
            creneau_fk=self.creneau, prof=self.prof, duree_creneau=2.0,
            commentaire='Fait' if fait else 'Non fait', date_suivie=quand)
        sp.departements.add(groupe)
        return sp

    def test_chaque_groupe_a_son_taux_et_le_programme_garde_le_maximum(self):
        # A fait ses deux séances, B n'a fait que la première.
        self._pointage(self.a, 1, True,  HIER)
        self._pointage(self.a, 2, True,  HIER)
        self._pointage(self.b, 1, True,  HIER)
        self._pointage(self.b, 2, False, HIER)

        matiere = avancement_par_groupe('2025-2026', 'I')[0]
        taux = {g['nom']: g['pct_TP'] for g in matiere['groupes']}

        self.assertEqual(taux['MPSI A'], 100)
        self.assertEqual(taux['MPSI B'], 50, "le retard de B doit apparaitre")
        self.assertEqual(matiere['programme_pct_TP'], 100,
                         "le chiffre du programme reste celui du groupe le plus avance")

        heures_dues = {g['nom']: g['rate_TP'] for g in matiere['groupes']}
        self.assertEqual(heures_dues['MPSI A'], 0)
        self.assertEqual(heures_dues['MPSI B'], 2.0)

    def test_une_seance_a_venir_n_est_pas_une_seance_manquee(self):
        self._pointage(self.b, 1, True,  HIER)
        self._pointage(self.b, 2, False, DEMAIN)   # programmée, pas encore passée

        matiere = avancement_par_groupe('2025-2026', 'I')[0]
        b = next(g for g in matiere['groupes'] if g['nom'] == 'MPSI B')

        self.assertEqual(b['rate_TP'], 0,
                         "une seance future ne doit pas compter comme manquee")
        # Elle figure toutefois au detail, marquee « a venir » : si les autres
        # groupes l'ont deja eue, c'est elle qu'il faudra replanifier.
        detail = seances_ratees('2025-2026', self.matiere.id, self.b.id)
        self.assertEqual(len(detail), 1)
        self.assertFalse(detail[0]['passee'])

    def test_les_seances_ratees_sont_nommees(self):
        self._pointage(self.b, 5, False, HIER)

        ratees = seances_ratees('2025-2026', self.matiere.id, self.b.id)

        self.assertEqual(len(ratees), 1)
        self.assertEqual(ratees[0]['numero_semaine'], 5)
        self.assertEqual(ratees[0]['jour'], 'Mardi')
        self.assertEqual(ratees[0]['creneau'], '10:00-12:00')
        self.assertEqual(ratees[0]['type_seance'], 'TP')
        self.assertEqual(ratees[0]['prof'], 'Prof TP')

    def test_un_groupe_sans_aucun_pointage_garde_sa_ligne(self):
        # Debut de semestre : rien n'est encore pointe. L'ecran doit tout de meme
        # montrer la planification et zero realise, comme celui des matieres —
        # une liste tiree des pointages n'aurait affiche aucun groupe.
        matiere = avancement_par_groupe('2025-2026', 'I', niveau='MPSI')[0]
        noms = {g['nom'] for g in matiere['groupes']}

        self.assertIn('MPSI A', noms)
        self.assertIn('MPSI B', noms)
        self.assertEqual(matiere['plan_TP'], 4, 'la planification reste affichee')
        a = next(g for g in matiere['groupes'] if g['nom'] == 'MPSI A')
        self.assertEqual(a['real_TP'], 0)
        self.assertEqual(a['pct_TP'], 0)

    def test_un_sous_groupe_herite_du_CM_de_sa_classe(self):
        # Le CM est dispense a la classe entiere, le TP est dedouble. Un
        # sous-groupe qui ne compterait que ses propres seances afficherait 0 %
        # de cours magistral — ce qui est faux, il les a suivis avec sa classe.
        sg = SousGroupeTP.objects.create(
            nom='MPSI A — G1', institution=self.inst,
            annee_universitaire='2025-2026', niveau=self.niveau,
            groupe='G1', classe=self.a)
        cours = EM.objects.create(code_em='MATH-S1', intitule='Mathématiques',
                                  CM=4, TD=0, TP=4, PR=0,
                                  departement=self.a, semestre=self.semestre)

        # 4 h de CM a la classe, 2 h de TP au seul sous-groupe.
        self._pointage(self.a, 1, True, HIER, matiere=cours, type_seance=self.cm)
        self._pointage(self.a, 2, True, HIER, matiere=cours, type_seance=self.cm)
        self._pointage(sg,     1, True, HIER, matiere=cours)

        matiere = next(m for m in avancement_par_groupe('2025-2026', 'I', niveau='MPSI')
                       if m['code_em'] == 'MATH-S1')
        g1 = next(g for g in matiere['groupes'] if g['nom'] == 'MPSI A — G1')

        self.assertEqual(g1['real_CM'], 4.0, 'le CM de la classe compte pour son sous-groupe')
        self.assertEqual(g1['pct_CM'], 100)
        self.assertEqual(g1['real_TP'], 2.0, 'le TP reste propre au sous-groupe')
        self.assertEqual(g1['classe'], 'MPSI A')
        self.assertEqual(g1['parent'], self.a.pk)

    def test_une_seance_inscrite_a_la_classe_ET_au_sous_groupe_ne_compte_qu_une_fois(self):
        sg = SousGroupeTP.objects.create(
            nom='MPSI A — G2', institution=self.inst,
            annee_universitaire='2025-2026', niveau=self.niveau,
            groupe='G2', classe=self.a)
        seance = self._pointage(self.a, 1, True, HIER)
        seance.departements.add(sg)          # inscrite aux deux

        matiere = avancement_par_groupe('2025-2026', 'I', niveau='MPSI')[0]
        g2 = next(g for g in matiere['groupes'] if g['nom'] == 'MPSI A — G2')

        self.assertEqual(g2['real_TP'], 2.0, 'une seule seance, donc 2 h et non 4')

    def test_l_ecart_designe_le_groupe_qui_n_a_pas_eu_la_seance(self):
        # Le cas de l'ecran : quatre groupes ont eu le cours, le cinquieme non
        # parce que son enseignant n'est pas venu. Peu importe la date ou la
        # maquette — c'est l'ecart aux AUTRES qui doit sauter aux yeux.
        self._pointage(self.a, 1, True,  DEMAIN)   # A l'a eu
        self._pointage(self.b, 1, False, DEMAIN)   # B ne l'a pas eu

        matiere = avancement_par_groupe('2025-2026', 'I', niveau='MPSI')[0]
        a = next(g for g in matiere['groupes'] if g['nom'] == 'MPSI A')
        b = next(g for g in matiere['groupes'] if g['nom'] == 'MPSI B')

        self.assertEqual(matiere['programme_TP'], 2.0, 'la reference est le groupe le plus avance')
        self.assertEqual(a['ecart_TP'], 0, 'le groupe de tete n a pas d ecart')
        self.assertEqual(b['ecart_TP'], 2.0, 'B a une seance de moins que A')
        # Et cela vaut MEME pour une seance a venir : le retard est reel des
        # lors qu'un autre groupe l'a faite.
        self.assertEqual(b['rate_TP'], 0, 'la seance n est pas encore echue')

    def test_une_classe_n_est_pas_en_retard_sur_son_propre_sous_groupe(self):
        # Un CM pointe au niveau du sous-groupe faisait passer la classe pour en
        # retard sur lui — or le sous-groupe herite d'elle, il ne peut la
        # depasser que par cet artefact. On compare donc entre pairs : les
        # classes entre elles, les sous-groupes entre eux.
        sg = SousGroupeTP.objects.create(
            nom='MPSI A — G1', institution=self.inst,
            annee_universitaire='2025-2026', niveau=self.niveau,
            groupe='G1', classe=self.a)
        self._pointage(sg, 1, True, HIER)

        matiere = avancement_par_groupe('2025-2026', 'I', niveau='MPSI')[0]
        a = next(g for g in matiere['groupes'] if g['nom'] == 'MPSI A')

        self.assertEqual(a['ecart_TP'], 0,
                         "la classe se compare aux autres classes, pas a son sous-groupe")

    def test_une_classe_sans_sous_groupe_se_compare_aux_sous_groupes_des_autres(self):
        # Le cas reel : un CM pointe sur le seul sous-groupe A1. MPSI B, qui n'a
        # pas de sous-groupe, ne l'a pas eu — elle est donc en retard, meme si
        # aucune CLASSE entiere n'a fait ce cours.
        a1 = SousGroupeTP.objects.create(
            nom='MPSI A — A1', institution=self.inst,
            annee_universitaire='2025-2026', niveau=self.niveau,
            groupe='A1', classe=self.a)
        SousGroupeTP.objects.create(
            nom='MPSI A — A2', institution=self.inst,
            annee_universitaire='2025-2026', niveau=self.niveau,
            groupe='A2', classe=self.a)
        self._pointage(a1, 1, True, HIER)

        matiere = avancement_par_groupe('2025-2026', 'I', niveau='MPSI')[0]
        par_nom = {g['nom']: g for g in matiere['groupes']}

        self.assertEqual(par_nom['MPSI A — A1']['ecart_TP'], 0, 'A1 est la reference')
        self.assertEqual(par_nom['MPSI A — A2']['ecart_TP'], 2.0, 'A2 ne l a pas eu')
        self.assertEqual(par_nom['MPSI B']['ecart_TP'], 2.0,
                         "B non plus, et elle n'a pas de sous-groupe")
        self.assertEqual(par_nom['MPSI A']['ecart_TP'], 0,
                         "la classe divisee n'a pas d'ecart propre")

    def test_sans_aucune_seance_programmee_rien_n_est_du(self):
        # Le piege : zero heure due, c'est AUSSI ce que donne une base ou le
        # suivi n'a jamais ete genere. L'ecran doit pouvoir distinguer « tout
        # est fait » de « on ne sait rien » — d'ou `due_*`, les heures ecoulees.
        matiere = avancement_par_groupe('2025-2026', 'I', niveau='MPSI')[0]
        a = next(g for g in matiere['groupes'] if g['nom'] == 'MPSI A')

        self.assertEqual(a['rate_TP'], 0)
        self.assertEqual(a['due_TP'], 0, 'aucune seance ecoulee : rien a juger')

    def test_une_seance_ecoulee_et_faite_fonde_le_a_jour(self):
        self._pointage(self.a, 1, True, HIER)

        matiere = avancement_par_groupe('2025-2026', 'I', niveau='MPSI')[0]
        a = next(g for g in matiere['groupes'] if g['nom'] == 'MPSI A')

        self.assertEqual(a['due_TP'], 2.0, 'la seance ecoulee est comptee')
        self.assertEqual(a['rate_TP'], 0, 'et elle a ete faite : rien en retard')

    def test_le_niveau_ecarte_les_matieres_de_l_autre_annee(self):
        # S1 est un semestre de MPSI, S3 de MP : tous deux impairs. Sans filtre
        # de niveau, un tableau MPSI affichait les matieres de deuxieme annee,
        # que ses groupes ne suivent pas.
        semestre_mp = Semestre.objects.create(
            code_semestre='S3', semestre='S3 — 2e année',
            niveau_semestre=self.niveau_mp, type_semestre='I')
        EM.objects.create(code_em='THERMO-S3', intitule='Thermodynamique',
                          CM=10, TD=0, TP=0, PR=0,
                          departement=self.a, semestre=semestre_mp)

        codes_mpsi = {m['code_em'] for m in avancement_par_groupe('2025-2026', 'I', niveau='MPSI')}
        codes_mp   = {m['code_em'] for m in avancement_par_groupe('2025-2026', 'I', niveau='MP')}

        self.assertEqual(codes_mpsi, {'INFO-S1'})
        self.assertEqual(codes_mp, {'THERMO-S3'})

    def test_le_filtre_de_niveau_ecarte_les_autres_annees(self):
        # Un groupe de MP fait la meme matiere : il ne doit pas surgir dans un
        # tableau de MPSI, ou sa colonne serait vide sans que rien ne l'explique.
        mp = Classe.objects.create(nom='MP A', institution=self.inst,
                                   annee_universitaire='2025-2026',
                                   niveau=self.niveau_mp, groupe='A')
        self._pointage(self.a, 1, True, HIER)
        self._pointage(mp, 1, True, HIER)

        mpsi = avancement_par_groupe('2025-2026', 'I', niveau='MPSI')[0]
        noms = {g['nom'] for g in mpsi['groupes']}

        # Les DEUX classes MPSI figurent — celle qui a pointe comme celle qui
        # n'a rien fait —, mais jamais celle de MP.
        self.assertEqual(noms, {'MPSI A', 'MPSI B'})
        self.assertNotIn('MP A', noms)
        self.assertTrue(all(g['niveau'] == 'MPSI' for g in mpsi['groupes']))

    def test_un_groupe_qui_n_a_rien_fait_figure_quand_meme(self):
        self._pointage(self.a, 1, True,  HIER)
        self._pointage(self.b, 1, False, HIER)

        matiere = avancement_par_groupe('2025-2026', 'I')[0]
        noms = {g['nom'] for g in matiere['groupes']}

        self.assertIn('MPSI B', noms,
                      "le groupe en retard total est justement celui qu'on cherche")
        b = next(g for g in matiere['groupes'] if g['nom'] == 'MPSI B')
        self.assertEqual(b['pct_TP'], 0)
