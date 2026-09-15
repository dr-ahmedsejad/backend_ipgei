"""
Test — un lot de documents désigné par matricules, collés dans un seul champ.

L'édition par groupe partait d'une classe entière. Elle ne couvrait pas le cas
courant : quelques étudiants pris dans plusieurs classes, ceux d'une
commission, ceux qui ont réclamé leur pièce. On les désigne désormais par leur
matricule, collés tels qu'on les a — d'un tableur, d'un courriel, à la main.

Ce que le test fige :

  1. le découpage accepte ce qu'on colle réellement — retours à la ligne,
     virgules, points-virgules, tabulations, espaces — et pas un séparateur
     imposé, qui ferait échouer le collage le plus fréquent sans dire lequel ;
  2. les doublons sautent, l'ORDRE DE SAISIE tient : c'est celui dans lequel on
     relira les pièces imprimées ;
  3. un matricule fautif ne fait pas échouer le lot, mais il est NOMMÉ — un lot
     silencieusement amputé est pire qu'un lot refusé, on ne s'aperçoit de rien ;
  4. sans année, on retient la plus récente : un matricule ne désigne pas une
     inscription à lui seul.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.absence.models import Etudiant
from apps.ipgei.models import Classe, InscriptionIPGEI, NiveauIPGEI, SemestreIPGEI
from apps.ipgei.views import _inscriptions_par_matricule, _matricules_demandes
from apps.parametres.models import Institution, Niveau


class DecoupageMatriculesTest(TestCase):
    """Le champ unique : ce qu'on y colle doit passer."""

    def test_les_separateurs_qu_on_colle_vraiment(self):
        attendu = ['242001', '242002', '242003']
        for brut, origine in (
            ('242001\n242002\n242003',    'une colonne de tableur'),
            ('242001, 242002, 242003',    'une énumération à la main'),
            ('242001;242002;242003',      'un export CSV'),
            ('242001\t242002\t242003',    'un collage de cellules'),
            ('242001 242002 242003',      'une saisie au kilomètre'),
            ('242001\n\n  242002 ,\n242003  ', 'un collage sale'),
        ):
            self.assertEqual(_matricules_demandes(brut), attendu, origine)

    def test_une_liste_json_passe_aussi(self):
        self.assertEqual(_matricules_demandes(['242001', ' 242002 ']),
                         ['242001', '242002'])

    def test_doublons_ecartes_ordre_conserve(self):
        """L'ordre est celui de la saisie : on relira les pièces dedans."""
        self.assertEqual(
            _matricules_demandes('242003, 242001, 242003, 242002, 242001'),
            ['242003', '242001', '242002'])

    def test_rien_a_decouper(self):
        for vide in ('', '   ', '\n\n', ',,;', None, []):
            self.assertEqual(_matricules_demandes(vide), [], repr(vide))


class ResolutionMatriculesTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MP')
        NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                   libelle='MP', rang=2)
        cls.classes = {
            annee: Classe.objects.create(
                nom=f'MP A {annee}', institution=cls.inst,
                annee_universitaire=annee, niveau=cls.socle, groupe='A')
            for annee in ('2024-2025', '2025-2026')
        }
        cls.s3 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S3', annee_universitaire='2025-2026',
            date_debut=date(2025, 10, 6))

    def _inscrire(self, matricule, annee, *, actif=True):
        etudiant = (Etudiant.objects.filter(matricule=matricule).first()
                    or Etudiant.objects.create(matricule=matricule,
                                               nom=f'Etudiant {matricule}'))
        return InscriptionIPGEI.objects.create(
            etudiant=etudiant, classe=self.classes[annee],
            annee_universitaire=annee, actif=actif)

    def test_resolution_dans_l_ordre_demande(self):
        for m in ('242001', '242002', '242003'):
            self._inscrire(m, '2025-2026')
        trouvees, erreurs = _inscriptions_par_matricule(
            ['242003', '242001', '242002'], '2025-2026')
        self.assertEqual([i.etudiant.matricule for i in trouvees],
                         ['242003', '242001', '242002'])
        self.assertEqual(erreurs, [])

    def test_un_matricule_inconnu_est_nomme_sans_faire_echouer_le_lot(self):
        self._inscrire('242001', '2025-2026')
        self._inscrire('242002', '2025-2026')
        trouvees, erreurs = _inscriptions_par_matricule(
            ['242001', '999999', '242002'], '2025-2026')

        self.assertEqual([i.etudiant.matricule for i in trouvees],
                         ['242001', '242002'], 'les deux autres passent')
        self.assertEqual(len(erreurs), 1)
        self.assertEqual(erreurs[0]['etudiant'], '999999')
        self.assertIn('999999', erreurs[0]['motif'])
        self.assertIn('2025-2026', erreurs[0]['motif'])

    def test_une_inscription_inactive_ne_repond_pas(self):
        self._inscrire('242001', '2025-2026', actif=False)
        trouvees, erreurs = _inscriptions_par_matricule(['242001'], '2025-2026')
        self.assertEqual(trouvees, [])
        self.assertEqual(len(erreurs), 1)

    def test_l_annee_departage_deux_inscriptions(self):
        """Un matricule ne désigne pas une inscription : il en a une par année."""
        self._inscrire('242001', '2024-2025')
        self._inscrire('242001', '2025-2026')
        for annee in ('2024-2025', '2025-2026'):
            trouvees, _ = _inscriptions_par_matricule(['242001'], annee)
            self.assertEqual(trouvees[0].annee_universitaire, annee)

    def test_sans_annee_on_retient_la_plus_recente(self):
        self._inscrire('242001', '2024-2025')
        self._inscrire('242001', '2025-2026')
        trouvees, _ = _inscriptions_par_matricule(['242001'], '')
        self.assertEqual(len(trouvees), 1)
        self.assertEqual(trouvees[0].annee_universitaire, '2025-2026')

    def test_le_semestre_fournit_l_annee_a_defaut(self):
        """Demander le relevé de S3 2025-2026 dit de quelle année on parle."""
        self._inscrire('242001', '2024-2025')
        self._inscrire('242001', '2025-2026')
        trouvees, _ = _inscriptions_par_matricule(['242001'], '', self.s3)
        self.assertEqual(trouvees[0].annee_universitaire, '2025-2026')
