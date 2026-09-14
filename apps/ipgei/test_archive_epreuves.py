"""
Test — l'épreuve est ARCHIVÉE avec l'emploi du temps.

La garantie vérifiée ici : déplacer un examen après coup ne récrit pas le
document déjà remis aux étudiants. Sans archive, un emploi du temps réimprimé
dans deux ans aurait montré les séances telles qu'elles étaient et l'épreuve
telle qu'elle est — un document qui n'a jamais existé.

Le second point, moins évident : une semaine d'examens ne porte souvent AUCUNE
séance. L'archivage l'écartait pour cette raison, laissant sans prise de vue
précisément le document qu'on distribue le plus.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date, time
from decimal import Decimal

from django.test import TestCase

from apps.ipgei.archive import archiver_semaine
from apps.ipgei.models import (
    Classe, EmploiArchive, EpreuveArchive, EpreuveClasse, EpreuveIPGEI, Matiere,
    NiveauIPGEI, SeanceReelle, SemaineIPGEI, SemestreIPGEI, SessionEvaluationIPGEI,
)
from apps.ipgei.views import _epreuves_de_la_semaine
from apps.parametres.models import Creneau, Institution, Jour, Niveau, Seance

CRENEAUX = [('08h00-09h30', 1), ('09h45-11h15', 2), ('11h30-13h00', 3),
            ('15h10-16h40', 4), ('17h00-18h30', 5)]


class ArchivageDesEpreuvesTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Test',
                                              est_principale=True)
        socle = Niveau.objects.create(niveau='MP')
        ref   = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                           libelle='MP', rang=2)
        cls.semestre = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S3', annee_universitaire='2025-2026',
            date_debut=date(2025, 10, 6))
        cls.session = SessionEvaluationIPGEI.objects.create(
            semestre=cls.semestre, type_session=SessionEvaluationIPGEI.TYPE_NORMALE)
        cls.matiere = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Mathématiques',
            code_semestre='S3', niveau_ref=ref, coefficient=Decimal('4'))
        cls.mp_a = Classe.objects.create(institution=cls.inst, niveau=socle,
                                         groupe='A', annee_universitaire='2025-2026')
        cls.mp_b = Classe.objects.create(institution=cls.inst, niveau=socle,
                                         groupe='B', annee_universitaire='2025-2026')

        for lib in ('Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi'):
            Jour.objects.create(jour=lib)
        cls.creneaux = [Creneau.objects.create(creneau=lib, ordre=o, duree=1.5)
                        for lib, o in CRENEAUX]
        cls.cm = Seance.objects.create(type_seance='CM')

        cls.semaine = cls.semestre.semaines.create(
            numero=7, date_debut=date(2025, 11, 17), date_fin=date(2025, 11, 23))

        # Mardi 18 novembre, 08h00, 3h, pour MP A et MP B.
        cls.epreuve = EpreuveIPGEI.objects.create(
            session=cls.session, matiere=cls.matiere, type_epreuve='EF', numero=1,
            date=date(2025, 11, 18), heure_debut=time(8, 0), duree_minutes=180,
            observations='Aucun document autorisé')
        EpreuveClasse.objects.create(epreuve=cls.epreuve, classe=cls.mp_a)
        EpreuveClasse.objects.create(epreuve=cls.epreuve, classe=cls.mp_b)

    def _archiver(self):
        return archiver_semaine('2025-2026', 'I', 7)

    # ── La prise de vue ──────────────────────────────────────────────────────
    def test_une_semaine_sans_aucune_seance_est_quand_meme_archivee(self):
        """
        Le cas qui manquait. L'archivage écartait toute classe sans séance :
        une semaine d'examens n'en a aucune, et se retrouvait sans prise de vue.
        """
        self.assertEqual(SeanceReelle.objects.count(), 0, 'aucune séance, bien')

        self.assertEqual(self._archiver(), 2, 'une ligne par classe convoquée')
        self.assertEqual(EmploiArchive.objects.count(), 0)
        self.assertEqual(EpreuveArchive.objects.count(), 2)

    def test_chaque_classe_convoquee_a_sa_ligne(self):
        self._archiver()
        self.assertEqual(
            sorted(EpreuveArchive.objects.values_list('classe_nom', flat=True)),
            sorted([self.mp_a.nom, self.mp_b.nom]))

    def test_l_archive_fige_des_VALEURS_et_non_des_liens(self):
        self._archiver()
        ligne = EpreuveArchive.objects.filter(classe=self.mp_a).first()
        self.assertEqual(ligne.matiere_code, 'MATH')
        self.assertEqual(ligne.matiere_intitule, 'Mathématiques')
        self.assertEqual(ligne.observations, 'Aucun document autorisé')
        self.assertEqual(ligne.duree_minutes, 180)

    def test_deplacer_l_epreuve_ne_recrit_pas_l_archive(self):
        """
        La garantie qui justifie toute cette table : le document déjà remis ne
        bouge pas quand l'examen est reporté.
        """
        self._archiver()

        self.epreuve.date = date(2025, 11, 20)
        self.epreuve.heure_debut = time(15, 10)
        self.epreuve.duree_minutes = 120
        self.epreuve.save()

        ligne = EpreuveArchive.objects.filter(classe=self.mp_a).first()
        self.assertEqual(ligne.date, date(2025, 11, 18))
        self.assertEqual(ligne.heure_debut, time(8, 0))
        self.assertEqual(ligne.duree_minutes, 180)

    def test_supprimer_l_epreuve_laisse_l_archive_intacte(self):
        self._archiver()
        self.epreuve.convocations.all().delete()
        self.epreuve.delete()
        self.assertEqual(EpreuveArchive.objects.count(), 2,
                         "l'archive ne suit pas la suppression")

    def test_une_regeneration_ajoute_une_version(self):
        self._archiver()
        self._archiver()
        versions = sorted(EpreuveArchive.objects
                          .filter(classe=self.mp_a)
                          .values_list('version', flat=True))
        self.assertEqual(versions, [1, 2])

    def test_la_version_suit_celle_des_seances_de_la_meme_classe(self):
        """
        Le maximum se prend sur les DEUX tables. Une semaine d'examens a pu
        n'archiver que des épreuves ; repartir de 1 côté séances mêlerait deux
        prises de vue sous le même numéro.
        """
        self._archiver()                       # épreuves en version 1

        SeanceReelle.objects.create(
            classe=self.mp_a, semaine=self.semaine, jour=Jour.objects.first(),
            creneau=self.creneaux[0], matiere=self.matiere, type_seance=self.cm)
        self._archiver()

        self.assertEqual(
            EmploiArchive.objects.filter(classe=self.mp_a).first().version, 2)

    # ── La lecture ───────────────────────────────────────────────────────────
    def test_l_archive_prime_sur_l_epreuve_vivante(self):
        self._archiver()
        self.epreuve.heure_debut = time(15, 10)
        self.epreuve.save(update_fields=['heure_debut'])

        lues = _epreuves_de_la_semaine(self.mp_a, self.semaine)
        self.assertEqual(len(lues), 1)
        self.assertIsInstance(lues[0], EpreuveArchive)
        self.assertEqual(lues[0].heure_debut, time(8, 0))

    def test_sans_archive_on_lit_l_epreuve_vivante(self):
        lues = _epreuves_de_la_semaine(self.mp_a, self.semaine)
        self.assertEqual(len(lues), 1)
        self.assertIsInstance(lues[0], EpreuveIPGEI)

    def test_seule_la_derniere_version_est_rendue(self):
        self._archiver()
        self._archiver()
        lues = _epreuves_de_la_semaine(self.mp_a, self.semaine)
        self.assertEqual(len(lues), 1, 'deux versions ne font pas deux épreuves')
        self.assertEqual(lues[0].version, 2)

    def test_l_archive_recouvre_les_memes_creneaux_que_l_original(self):
        """
        Le calcul vient d'un mixin partagé. Le recopier aurait laissé les deux
        dériver, et un document réimprimé n'aurait plus fusionné les mêmes
        colonnes — ce qui vide l'archive de son sens.
        """
        avant = [c.creneau for c in self.creneaux if self.epreuve.recouvre(c)]
        self._archiver()
        ligne = EpreuveArchive.objects.filter(classe=self.mp_a).first()
        apres = [c.creneau for c in self.creneaux if ligne.recouvre(c)]

        self.assertEqual(avant, apres)
        self.assertEqual(apres, ['08h00-09h30', '09h45-11h15'])

    def test_la_grille_pdf_se_construit_aussi_depuis_l_archive(self):
        from apps.ipgei import edt_pdf

        self._archiver()
        lignes = edt_pdf.construire_grille(
            [], axe='classe',
            epreuves=list(EpreuveArchive.objects.filter(classe=self.mp_a)))[1]

        mardi = next(l for l in lignes if l['jour'] == 'Mardi')
        fusionnee = next(c for c in mardi['cells'] if c['epreuve'])
        self.assertEqual(fusionnee['colspan'], 2)
        self.assertEqual(fusionnee['epreuve']['intitule'], 'Mathématiques')
        self.assertIn('08h00', fusionnee['epreuve']['horaire'])
