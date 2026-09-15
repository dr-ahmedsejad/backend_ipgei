"""
Test — recopier une semaine vers d'autres, et ne jamais écraser ce qu'on a saisi.

La grille type existait, mais personne ne la remplit : au relevé du 15/09/2026,
trois cases de grille pour dix-sept séances réelles. On bâtit la semaine 1 sur
l'écran de l'emploi du temps et l'on veut les suivantes identiques — ce geste
manquait, et son absence se lisait comme une panne de la duplication.

Ce que le test fige :

  1. la copie porte `dupliquee`, la SOURCE garde la sienne — sans quoi une
     séance manuelle recopiée deviendrait inécrasable, et re-dupliquer après
     avoir corrigé la semaine 1 ne changerait plus rien ;
  2. sans `ecraser`, une case occupée reste telle quelle ; avec, seules les
     copies et les séances de grille cèdent — une saisie manuelle et une
     permutation ne cèdent JAMAIS, c'est la promesse de la case à cocher ;
  3. une ÉPREUVE interdit sa case, et `ecraser` n'y change rien : la classe
     compose, elle ne peut pas être en cours. Le recouvrement partiel suffit ;
  4. une semaine source vide est refusée, avec son motif — pas un zéro
     silencieux, qui ressemble à une panne.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date, time

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.ipgei import services
from apps.ipgei.models import (
    Classe, EpreuveIPGEI, Matiere, NiveauIPGEI, SeanceReelle, SemaineIPGEI,
    SemestreIPGEI, SessionEvaluationIPGEI,
)
from apps.parametres.models import Creneau, Institution, Jour, Niveau, Seance


class DupliquerSemaineTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MPSI')
        cls.ref = NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI',
                                             libelle='MPSI', rang=1)
        cls.classe = Classe.objects.create(
            nom='MPSI A', institution=cls.inst, annee_universitaire='2025-2026',
            niveau=cls.socle, groupe='A')
        cls.s1 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S1', annee_universitaire='2025-2026',
            date_debut=date(2025, 10, 6))
        # Trois semaines de cours, lundi à samedi.
        cls.semaines = [
            SemaineIPGEI.objects.create(
                semestre=cls.s1, numero=n,
                date_debut=date(2025, 10, 6 + 7 * (n - 1)),
                date_fin=date(2025, 10, 11 + 7 * (n - 1)),
                type_semaine=SemaineIPGEI.TYPE_COURS)
            for n in (1, 2, 3)
        ]
        # Le référentiel porte « Lundi » en capitale, `JOURS_SEMAINE` rend
        # « lundi » : l'écart qui a fait passer le garde-fou des épreuves à
        # travers, et que ce jeu d'essai reproduit donc fidèlement.
        cls.lundi = Jour.objects.create(jour='Lundi')
        cls.matin = Creneau.objects.create(creneau='08h00-09h30', ordre=1)
        cls.fin_matin = Creneau.objects.create(creneau='09h45-11h15', ordre=2)
        cls.apres_midi = Creneau.objects.create(creneau='14h00-15h30', ordre=3)
        cls.type_cours = Seance.objects.create(type_seance='CM')
        cls.matiere = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Mathématiques',
            code_semestre='S1', niveau_ref=cls.ref)

    def _seance(self, semaine, creneau, *, origine=SeanceReelle.ORIGINE_MANUELLE):
        return SeanceReelle.objects.create(
            classe=self.classe, semaine=semaine, jour=self.lundi, creneau=creneau,
            matiere=self.matiere, type_seance=self.type_cours,
            date=semaine.date_debut, origine=origine)

    def _source(self):
        """Deux séances saisies à la main en semaine 1."""
        self._seance(self.semaines[0], self.matin)
        self._seance(self.semaines[0], self.apres_midi)
        return self.semaines[0]

    # ── 1. L'origine de la copie ────────────────────────────────────────────
    def test_la_copie_est_dupliquee_la_source_reste_manuelle(self):
        source = self._source()
        r = services.dupliquer_semaine(self.classe, source, self.semaines[1:])

        self.assertEqual(r['creees'], 4)
        copies = SeanceReelle.objects.filter(semaine__in=self.semaines[1:])
        self.assertEqual(
            set(copies.values_list('origine', flat=True)),
            {SeanceReelle.ORIGINE_DUPLIQUEE},
            'la copie ne doit hériter ni de la source ni de « grille »')
        self.assertEqual(
            set(SeanceReelle.objects.filter(semaine=source)
                .values_list('origine', flat=True)),
            {SeanceReelle.ORIGINE_MANUELLE},
            "l'original ne doit pas être requalifié")

    def test_la_source_n_est_jamais_sa_propre_cible(self):
        """Se dupliquer sur soi-même effacerait la source avec `ecraser`."""
        source = self._source()
        r = services.dupliquer_semaine(self.classe, source, self.semaines,
                                       ecraser=True)
        self.assertEqual(r['creees'], 4, 'deux cibles, pas trois')
        self.assertEqual(SeanceReelle.objects.filter(semaine=source).count(), 2)

    # ── 2. Ce que « écraser » touche, et ce qu'il épargne ───────────────────
    def test_sans_ecraser_une_case_occupee_reste_intacte(self):
        source = self._source()
        deja = self._seance(self.semaines[1], self.matin)
        r = services.dupliquer_semaine(self.classe, source, [self.semaines[1]])

        self.assertEqual(r['ignorees'], 1)
        self.assertEqual(r['creees'], 1)
        self.assertTrue(SeanceReelle.objects.filter(pk=deja.pk).exists())

    def test_ecraser_reprend_une_copie_mais_epargne_la_saisie(self):
        """
        Le cœur de la promesse : après correction de la semaine 1, on
        re-duplique et les COPIES cèdent — jamais ce qu'on a saisi à la main.
        """
        source = self._source()
        services.dupliquer_semaine(self.classe, source, [self.semaines[1]])
        manuelle = self._seance(self.semaines[1], self.fin_matin)
        permutee = self._seance(self.semaines[2], self.matin,
                                origine=SeanceReelle.ORIGINE_PERMUTATION)

        r = services.dupliquer_semaine(self.classe, source, self.semaines[1:],
                                       ecraser=True)

        self.assertEqual(r['remplacees'], 2, 'les deux copies de la semaine 2')
        self.assertEqual(r['ignorees'], 1, 'la permutation de la semaine 3')
        self.assertTrue(SeanceReelle.objects.filter(pk=manuelle.pk).exists(),
                        'la saisie manuelle survit')
        self.assertTrue(SeanceReelle.objects.filter(pk=permutee.pk).exists(),
                        'la permutation survit')

    # ── 3. Une épreuve interdit sa case ─────────────────────────────────────
    def _poser_epreuve(self, semaine, heure, duree):
        # Une SEULE session par semestre — la contrainte d'unicité porte sur
        # (semestre, type_session) —, et un numéro d'épreuve par semaine, qui
        # porte la sienne sur (session, matière, type, numéro).
        session, _ = SessionEvaluationIPGEI.objects.get_or_create(
            semestre=self.s1, type_session='normale')
        epreuve = EpreuveIPGEI.objects.create(
            session=session, matiere=self.matiere, type_epreuve='DS',
            numero=semaine.numero,
            date=semaine.date_debut, heure_debut=heure, duree_minutes=duree)
        epreuve.classes.add(self.classe)
        return epreuve

    def test_une_epreuve_bloque_la_case_meme_avec_ecraser(self):
        source = self._source()
        # 08h00 -> 10h00 : recouvre 08h00-09h30 ENTIÈREMENT et mord 15 minutes
        # sur 09h45-11h15. L'après-midi reste libre.
        self._poser_epreuve(self.semaines[1], time(8, 0), 120)

        r = services.dupliquer_semaine(self.classe, source, [self.semaines[1]],
                                       ecraser=True)

        self.assertEqual(r['creees'], 1, "seul l'après-midi passe")
        self.assertEqual(r['conflits'], 1)
        self.assertIn('la classe compose', r['details_conflits'][0])
        self.assertFalse(
            SeanceReelle.objects.filter(semaine=self.semaines[1],
                                        creneau=self.matin).exists(),
            'aucun cours ne doit se poser pendant une épreuve')

    def test_le_recouvrement_partiel_suffit(self):
        """
        Un DS de 08h00 à 10h00 ne mord que quinze minutes sur 09h45-11h15 —
        mais personne ne peut y commencer un cours, la classe est sur sa copie.
        """
        source = self._source()
        self._seance(self.semaines[0], self.fin_matin)
        self._poser_epreuve(self.semaines[1], time(8, 0), 120)

        r = services.dupliquer_semaine(self.classe, source, [self.semaines[1]])
        self.assertEqual(r['conflits'], 2, 'le créneau mordu compte aussi')

    def test_une_epreuve_d_une_autre_classe_ne_bloque_rien(self):
        source = self._source()
        autre = Classe.objects.create(
            nom='MPSI B', institution=self.inst, annee_universitaire='2025-2026',
            niveau=self.socle, groupe='B')
        session = SessionEvaluationIPGEI.objects.create(
            semestre=self.s1, type_session='normale')
        epreuve = EpreuveIPGEI.objects.create(
            session=session, matiere=self.matiere, type_epreuve='DS', numero=1,
            date=self.semaines[1].date_debut, heure_debut=time(8, 0),
            duree_minutes=240)
        epreuve.classes.add(autre)

        r = services.dupliquer_semaine(self.classe, source, [self.semaines[1]])
        self.assertEqual(r['conflits'], 0)
        self.assertEqual(r['creees'], 2)

    # ── 4. Le bilan se lit ──────────────────────────────────────────────────
    def test_deux_sous_groupes_donnent_deux_motifs_distincts(self):
        """
        Deux sous-groupes d'un même créneau produisaient DEUX LIGNES
        IDENTIQUES : on lisait deux fois la même chose sans savoir qu'il
        s'agissait de deux cases — et le navigateur refusait la clé en double.
        """
        from apps.ipgei.models import SousGroupeTP

        a = SousGroupeTP.objects.create(classe=self.classe, groupe='A')
        b = SousGroupeTP.objects.create(classe=self.classe, groupe='B')
        for groupe in (a, b):
            SeanceReelle.objects.create(
                classe=self.classe, semaine=self.semaines[0], jour=self.lundi,
                creneau=self.matin, sous_groupe=groupe, matiere=self.matiere,
                type_seance=self.type_cours, date=self.semaines[0].date_debut)
        self._poser_epreuve(self.semaines[1], time(8, 0), 120)

        r = services.dupliquer_semaine(self.classe, self.semaines[0],
                                       [self.semaines[1]])

        self.assertEqual(r['conflits'], 2, 'deux cases écartées')
        self.assertEqual(len(set(r['details_conflits'])), 2,
                         'et deux motifs DISTINCTS, pas la même phrase deux fois')
        self.assertTrue(any('(A)' in d for d in r['details_conflits']))
        self.assertTrue(any('(B)' in d for d in r['details_conflits']))

    def test_les_motifs_repetes_ne_se_lisent_qu_une_fois(self):
        """
        Le compte dit l'ampleur, la liste sert à LIRE : la même phrase répétée
        seize fois n'apprend rien de plus que la première.
        """
        self._seance(self.semaines[0], self.matin)
        for semaine in self.semaines[1:]:
            self._poser_epreuve(semaine, time(8, 0), 120)

        r = services.dupliquer_semaine(self.classe, self.semaines[0],
                                       self.semaines[1:])
        self.assertEqual(len(r['details_conflits']), len(set(r['details_conflits'])))

    # ── 5. Le refus motivé, plutôt que le zéro silencieux ───────────────────
    def test_une_semaine_source_vide_est_refusee(self):
        with self.assertRaises(ValidationError) as cas:
            services.dupliquer_semaine(self.classe, self.semaines[0],
                                       self.semaines[1:])
        self.assertIn('aucune séance', str(cas.exception))

    def test_aucune_cible_est_refuse(self):
        source = self._source()
        with self.assertRaises(ValidationError):
            services.dupliquer_semaine(self.classe, source, [])
