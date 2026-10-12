"""
Test — le décalage de rentrée, posé sur le semestre, descend sur les groupes.

Il n'existe qu'UN calendrier par parité : `synchroniser_calendrier` reflète les
mêmes semaines, aux mêmes numéros, dans S1 et S3. Quand une promotion entre
plus tard — les MPSI arrivés le 12/10/2026, quatre semaines après les MP —,
ses étudiants lisent « semaine 5 » sur ce qui est leur première.

Le socle sait déjà convertir : `numero_pedagogique_dept` recule le numéro de
`Departement.decalage_impair`, et rend `None` avant la rentrée. Mais ce champ
vit sur CHAQUE groupe — une quinzaine de lignes pour les MPSI, sous-groupes
compris. Le régler une à une, c'est quinze occasions d'en oublier une, et
l'oubli ne se voit nulle part.

D'où `SemestreIPGEI.decalage_semaines` : une source unique, qui descend sur les
départements du niveau. Ce que les tests figent :

  1. la propagation ne touche que les niveaux DU semestre ;
  2. ni l'autre PARITÉ — un retard en S1 ne regarde pas le S2 ;
  3. ni les autres ANNÉES, dont les documents sont imprimés ;
  4. elle est idempotente, et rend le nombre de lignes réellement corrigées ;
  5. remettre le décalage à zéro le retire — la correction se défait.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date

from django.test import TestCase

from apps.departement.models import Departement
from apps.departement.semaine_helpers import numero_pedagogique_dept
from apps.ipgei.models import NiveauIPGEI, SemestreIPGEI
from apps.ipgei.services import appliquer_decalage
from apps.parametres.models import Institution, Niveau

ANNEE = '2026-2027'


class DecalageDeRentreeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.mpsi = Niveau.objects.create(niveau='MPSI')
        cls.mp   = Niveau.objects.create(niveau='MP')
        NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI',
                                   libelle='MPSI', rang=1, actif=True)
        NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                   libelle='MP', rang=2, actif=True)

    def semestre(self, code, decalage=0, annee=ANNEE):
        return SemestreIPGEI.objects.create(
            institution=self.inst, code=code, annee_universitaire=annee,
            date_debut=date(2026, 9, 14), decalage_semaines=decalage)

    def groupe(self, nom, niveau, annee=ANNEE):
        return Departement.objects.create(
            nom=nom, code=nom.replace(' ', '-'), institution=self.inst,
            niveau=niveau, annee_universitaire=annee)

    # ── La propagation ──────────────────────────────────────────────────────
    def test_le_decalage_descend_sur_les_groupes_du_niveau(self):
        a, b = self.groupe('MPSI A', self.mpsi), self.groupe('MPSI A — A1', self.mpsi)
        corriges = appliquer_decalage(self.semestre('S1', decalage=4))
        self.assertEqual(corriges, 2)
        for g in (a, b):
            g.refresh_from_db()
            self.assertEqual(g.decalage_impair, 4)

    def test_les_autres_niveaux_ne_bougent_pas(self):
        """Le S3 des MP reste à sa date : c'est lui qui a commencé à l'heure."""
        mp = self.groupe('MP A', self.mp)
        self.groupe('MPSI A', self.mpsi)
        appliquer_decalage(self.semestre('S1', decalage=4))
        mp.refresh_from_db()
        self.assertEqual(mp.decalage_impair, 0)

    def test_l_autre_parite_n_est_pas_touchee(self):
        """Un retard au premier semestre ne dit rien du second."""
        g = self.groupe('MPSI A', self.mpsi)
        appliquer_decalage(self.semestre('S1', decalage=4))
        g.refresh_from_db()
        self.assertEqual(g.decalage_impair, 4)
        self.assertEqual(g.decalage_pair, 0, 'le S2 reprend à sa propre date')

    def test_une_autre_annee_n_est_pas_touchee(self):
        """Les documents d'une promotion passée sont imprimés : on n'y revient pas."""
        ancien = self.groupe('MPSI A', self.mpsi, annee='2025-2026')
        self.groupe('MPSI A', self.mpsi)
        appliquer_decalage(self.semestre('S1', decalage=4))
        ancien.refresh_from_db()
        self.assertEqual(ancien.decalage_impair, 0)

    def test_un_semestre_PAIR_ecrit_dans_sa_propre_colonne(self):
        """Le pendant du test précédent, sans lequel il ne prouvait rien.

        Un S2 retardé — partiels reportés, stage de démarrage — doit écrire
        `decalage_pair` et laisser `decalage_impair` tranquille. Sans ce cas,
        écrire toujours dans la colonne impaire passait inaperçu.
        """
        g = self.groupe('MPSI A', self.mpsi)
        appliquer_decalage(self.semestre('S2', decalage=2))
        g.refresh_from_db()
        self.assertEqual(g.decalage_pair, 2)
        self.assertEqual(g.decalage_impair, 0, "le S1 garde sa propre rentrée")

    def test_les_deux_parites_cohabitent(self):
        """Poser l'une ne défait pas l'autre."""
        g = self.groupe('MPSI A', self.mpsi)
        appliquer_decalage(self.semestre('S1', decalage=4))
        appliquer_decalage(self.semestre('S2', decalage=2))
        g.refresh_from_db()
        self.assertEqual((g.decalage_impair, g.decalage_pair), (4, 2))

    # ── La répétition ───────────────────────────────────────────────────────
    def test_appliquer_deux_fois_ne_corrige_qu_une_fois(self):
        self.groupe('MPSI A', self.mpsi)
        s = self.semestre('S1', decalage=4)
        self.assertEqual(appliquer_decalage(s), 1)
        self.assertEqual(appliquer_decalage(s), 0, 'rien à corriger la seconde fois')

    def test_remettre_a_zero_retire_le_decalage(self):
        g = self.groupe('MPSI A', self.mpsi)
        s = self.semestre('S1', decalage=4)
        appliquer_decalage(s)
        s.decalage_semaines = 0
        appliquer_decalage(s)
        g.refresh_from_db()
        self.assertEqual(g.decalage_impair, 0)

    def test_sans_niveau_rien_ne_se_passe(self):
        """Un semestre dont aucun niveau n'est actif ne touche à rien."""
        NiveauIPGEI.objects.filter(rang=1).update(actif=False)
        self.groupe('MPSI A', self.mpsi)
        self.assertEqual(appliquer_decalage(self.semestre('S1', decalage=4)), 0)

    # ── Ce que l'étudiant lira ──────────────────────────────────────────────
    def test_la_semaine_5_devient_la_premiere(self):
        """Le cas réel, bout en bout : 12/10/2026 est la semaine n°5 en base."""
        g = self.groupe('MPSI A', self.mpsi)
        appliquer_decalage(self.semestre('S1', decalage=4))
        g.refresh_from_db()
        self.assertEqual(numero_pedagogique_dept(5, g, 'I'), 1)
        self.assertEqual(numero_pedagogique_dept(6, g, 'I'), 2)

    def test_les_semaines_d_avant_la_rentree_disparaissent(self):
        """Elles ne valent ni 0 ni −3 : elles n'existent pas pour cette promotion."""
        g = self.groupe('MPSI A', self.mpsi)
        appliquer_decalage(self.semestre('S1', decalage=4))
        g.refresh_from_db()
        for numero in (1, 2, 3, 4):
            self.assertIsNone(numero_pedagogique_dept(numero, g, 'I'))
