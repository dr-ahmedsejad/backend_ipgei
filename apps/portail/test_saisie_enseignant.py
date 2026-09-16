"""
Test — la saisie en ligne rend enfin une grille remplie.

Elle lisait `build_feuille` (`apps/evaluations`), donc `InscriptionElement`,
`SessionEvaluation` et `evaluations.Note` — la couche LMD que le fork a
remplacée, et dont les tables portent **zéro ligne**. Tout enseignant qui
ouvrait l'écran voyait une grille vide, sans erreur. Face à cela : 5 580
`ipgei.Note` en base.

Ce que le test fige :

  1. le périmètre vient de l'EMPLOI DU TEMPS — un enseignant sans séance n'a
     aucune feuille, et celui qui en a ne voit que les siennes ;
  2. la grille porte les vrais inscrits et les vraies notes IPGEI ;
  3. une feuille qui n'est pas la sienne est refusée, sur le TRIPLET complet ;
  4. un brouillon recouvre la note officielle et se signale comme tel ;
  5. une case vidée EFFACE le brouillon — c'est ainsi qu'on se corrige ;
  6. le dépôt recopie son instantané, sans quoi la restauration du miroir le
     rendrait illisible ;
  7. une note d'une AUTRE feuille est rejetée même si son identifiant est passé
     en clair ;
  8. `numero` sépare le DS 1 du DS 2 — sans lui, le second écraserait le premier.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.db import connection
from django.test import TestCase

from apps.absence.models import Etudiant
from apps.ipgei.models import (
    Classe, Evaluation, InscriptionIPGEI, Matiere, NiveauIPGEI, Note,
    SeanceReelle, SemaineIPGEI, SemestreIPGEI,
)
from apps.parametres.models import Creneau, Institution, Jour, Niveau, Seance
from apps.portail import ipgei_enseignant as source
from apps.prof.models import Prof, ProfTypeHistory
from apps.reclamations.models import SaisieNoteEnLigne


class SaisieEnseignantTest(TestCase):

    @classmethod
    def setUpClass(cls):
        # `prof_type_history` naît d'une migration SQL brute, absente sous
        # `--no-migrations`. Même parade que `test_duplication_conflits` : la
        # vérification d'existence vaut pour SQLite comme pour PostgreSQL.
        if ProfTypeHistory._meta.db_table not in connection.introspection.table_names():
            with connection.schema_editor() as se:
                se.create_model(ProfTypeHistory)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MPSI')
        cls.ref = NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI',
                                             libelle='MPSI', rang=1)
        cls.s1 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S1', annee_universitaire='2025-2026',
            date_debut=date(2025, 10, 6))
        cls.semaine = SemaineIPGEI.objects.create(
            semestre=cls.s1, numero=1, date_debut=date(2025, 10, 6),
            date_fin=date(2025, 10, 11), type_semaine=SemaineIPGEI.TYPE_COURS)
        cls.lundi   = Jour.objects.create(jour='Lundi')
        cls.matin   = Creneau.objects.create(creneau='08h00-09h30', ordre=1)
        cls.cm      = Seance.objects.create(type_seance='CM')

        cls.math = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Mathematiques',
            code_semestre='S1', niveau_ref=cls.ref, coefficient=Decimal('14'),
            has_tp=False, pct_ds=Decimal('30'), pct_tp=Decimal('0'),
            pct_exam=Decimal('70'))
        cls.phys = Matiere.objects.create(
            institution=cls.inst, code='PHYS', intitule='Physique',
            code_semestre='S1', niveau_ref=cls.ref, coefficient=Decimal('12'),
            has_tp=False, pct_ds=Decimal('30'), pct_tp=Decimal('0'),
            pct_exam=Decimal('70'))

        cls.mpsi_a = Classe.objects.create(
            nom='MPSI A', institution=cls.inst, annee_universitaire='2025-2026',
            niveau=cls.socle, groupe='A')
        cls.mpsi_b = Classe.objects.create(
            nom='MPSI B', institution=cls.inst, annee_universitaire='2025-2026',
            niveau=cls.socle, groupe='B')

        cls.prof = Prof.objects.create(nom='Ahmed SEJAD', NNI='1234567890',
                                       type='permanent')
        cls.autre_prof = Prof.objects.create(nom='El Veth SIDI',
                                             NNI='0987654321', type='permanent')

        # LE PÉRIMÈTRE : une séance planifiée, et rien d'autre. C'est la seule
        # source de qui enseigne quoi — l'IPGEI n'a pas de table d'affectation.
        SeanceReelle.objects.create(
            classe=cls.mpsi_a, semaine=cls.semaine, jour=cls.lundi,
            creneau=cls.matin, matiere=cls.math, prof=cls.prof,
            type_seance=cls.cm, date=cls.semaine.date_debut)

        cls.notes = {}
        for rang, matricule in enumerate(('250001', '250002'), start=1):
            etudiant = Etudiant.objects.create(matricule=matricule, nom='E' + matricule)
            inscription = InscriptionIPGEI.objects.create(
                etudiant=etudiant, classe=cls.mpsi_a,
                annee_universitaire='2025-2026', numero_ordre=rang)
            note = Note.objects.create(inscription=inscription, matiere=cls.math,
                                       semestre=cls.s1)
            Evaluation.objects.create(note=note, type_evaluation='ds',
                                      numero=1, valeur=Decimal('12'))
            cls.notes[matricule] = note

        # Une note d'une AUTRE classe, pour éprouver le rejet.
        etranger = Etudiant.objects.create(matricule='250099', nom='Etranger')
        insc_b = InscriptionIPGEI.objects.create(
            etudiant=etranger, classe=cls.mpsi_b, annee_universitaire='2025-2026')
        cls.note_hors_feuille = Note.objects.create(
            inscription=insc_b, matiere=cls.math, semestre=cls.s1)

    # ── 1. Le périmètre vient de l'emploi du temps ──────────────────────────
    def test_les_feuilles_sont_celles_de_l_emploi_du_temps(self):
        feuilles = source.feuilles_de(self.prof, '2025-2026')
        self.assertEqual(len(feuilles), 1)
        self.assertEqual(
            (feuilles[0]['classe_nom'], feuilles[0]['matiere_code'],
             feuilles[0]['semestre_code']),
            ('MPSI A', 'MATH', 'S1'))

    def test_un_enseignant_sans_seance_n_a_aucune_feuille(self):
        self.assertEqual(source.feuilles_de(self.autre_prof, '2025-2026'), [])
        self.assertEqual(source.feuilles_de(None), [])

    # ── 2. La grille porte les vraies notes ─────────────────────────────────
    def test_la_grille_rend_les_inscrits_et_leurs_notes(self):
        grille = source.feuille(self.prof, self.mpsi_a.pk, self.math.pk, self.s1.pk)
        self.assertEqual(len(grille['lignes']), 2)
        self.assertEqual(grille['matiere_code'], 'MATH')
        self.assertEqual(grille['classe_nom'], 'MPSI A')

        premiere = grille['lignes'][0]
        self.assertEqual(premiere['etudiant_matricule'], '250001')
        self.assertEqual(premiere['ds'][0]['valeur'], '12.00')
        self.assertFalse(premiere['ds'][0]['brouillon'])
        self.assertTrue(premiere['ouverte'])

    def test_la_colonne_tp_suit_la_ponderation_de_la_classe(self):
        """MATH n'a pas de TP : la colonne ne doit pas s'afficher."""
        grille = source.feuille(self.prof, self.mpsi_a.pk, self.math.pk, self.s1.pk)
        self.assertFalse(grille['has_tp'])
        self.assertIsNone(grille['lignes'][0]['tp'])

    # ── 3. Une feuille qui n'est pas la sienne ──────────────────────────────
    def test_le_controle_porte_sur_le_triplet_entier(self):
        self.assertTrue(source.peut_saisir(
            self.prof, self.mpsi_a.pk, self.math.pk, self.s1.pk))
        # Bonne matière, mauvaise classe.
        self.assertFalse(source.peut_saisir(
            self.prof, self.mpsi_b.pk, self.math.pk, self.s1.pk))
        # Bonne classe, mauvaise matière.
        self.assertFalse(source.peut_saisir(
            self.prof, self.mpsi_a.pk, self.phys.pk, self.s1.pk))
        self.assertFalse(source.peut_saisir(
            self.autre_prof, self.mpsi_a.pk, self.math.pk, self.s1.pk))

    # ── 4-5. Le brouillon recouvre, et s'efface ─────────────────────────────
    def test_un_brouillon_recouvre_la_note_et_se_signale(self):
        note = self.notes['250001']
        source.deposer(self.prof, None, note, 'ds', 1, Decimal('17'))

        grille = source.feuille(self.prof, self.mpsi_a.pk, self.math.pk, self.s1.pk)
        case = grille['lignes'][0]['ds'][0]
        self.assertEqual(case['valeur'], '17.00')
        self.assertTrue(case['brouillon'],
                        "l'enseignant doit voir que ce n'est pas encore officiel")
        # La note OFFICIELLE n'a pas bougé : on n'écrit jamais dans ipgei_note.
        note.refresh_from_db()
        self.assertEqual(note.evaluations.get(type_evaluation='ds', numero=1).valeur,
                         Decimal('12'))

    def test_une_case_videe_efface_le_brouillon(self):
        note = self.notes['250001']
        source.deposer(self.prof, None, note, 'ds', 1, Decimal('17'))
        self.assertEqual(source.deposer(self.prof, None, note, 'ds', 1, None),
                         'efface')
        self.assertFalse(SaisieNoteEnLigne.objects.filter(note_ipgei_id=note.pk).exists())

        grille = source.feuille(self.prof, self.mpsi_a.pk, self.math.pk, self.s1.pk)
        self.assertEqual(grille['lignes'][0]['ds'][0]['valeur'], '12.00')
        self.assertFalse(grille['lignes'][0]['ds'][0]['brouillon'])

    def test_redeposer_remplace_au_lieu_d_empiler(self):
        note = self.notes['250001']
        source.deposer(self.prof, None, note, 'ds', 1, Decimal('15'))
        source.deposer(self.prof, None, note, 'ds', 1, Decimal('16'))
        brouillons = SaisieNoteEnLigne.objects.filter(note_ipgei_id=note.pk)
        self.assertEqual(brouillons.count(), 1)
        self.assertEqual(brouillons.first().valeur, Decimal('16'))

    # ── 6. L'instantané, recopié au dépôt ───────────────────────────────────
    def test_le_depot_recopie_son_instantane(self):
        """
        Sans lui, la ligne serait illisible après la restauration du miroir :
        elle survit au dump, mais tout ce à quoi elle pourrait se joindre est
        remplacé.
        """
        note = self.notes['250002']
        source.deposer(self.prof, None, note, 'ds', 1, Decimal('14'))
        b = SaisieNoteEnLigne.objects.get(note_ipgei_id=note.pk)
        self.assertEqual(b.etudiant_matricule, '250002')
        self.assertEqual(b.matiere_code, 'MATH')
        self.assertEqual(b.classe_nom, 'MPSI A')
        self.assertEqual(b.semestre_code, 'S1')
        self.assertEqual(b.annee_universitaire, '2025-2026',
                         "l'année manquait : rien n'aurait séparé deux promotions")
        self.assertEqual(b.prof_nom, 'Ahmed SEJAD')

    # ── 7-8. Les garde-fous du dépôt ────────────────────────────────────────
    def test_un_type_inconnu_est_refuse(self):
        with self.assertRaises(ValueError):
            source.deposer(self.prof, None, self.notes['250001'], 'colle', 1,
                           Decimal('10'))

    def test_le_rattrapage_n_est_pas_saisissable_en_ligne(self):
        """Il se compose APRÈS le jury : c'est la scolarité qui l'ouvre."""
        self.assertNotIn('ratt', source.TYPES_SAISISSABLES)
        with self.assertRaises(ValueError):
            source.deposer(self.prof, None, self.notes['250001'], 'ratt', 1,
                           Decimal('10'))

    def test_le_numero_separe_le_ds_1_du_ds_2(self):
        note = self.notes['250001']
        source.deposer(self.prof, None, note, 'ds', 1, Decimal('11'))
        source.deposer(self.prof, None, note, 'ds', 2, Decimal('19'))
        self.assertEqual(
            SaisieNoteEnLigne.objects.filter(note_ipgei_id=note.pk).count(), 2,
            'sans `numero`, le DS 2 aurait écrasé le DS 1')

    def test_une_note_hors_bornes_est_refusee(self):
        for brut in ('21', '-1', 'douze'):
            with self.assertRaises(ValueError, msg=brut):
                source.valeur_note(brut)

    def test_une_case_vide_vaut_effacement_pas_zero(self):
        self.assertIsNone(source.valeur_note(''))
        self.assertIsNone(source.valeur_note(None))
        self.assertEqual(source.valeur_note('12,5'), Decimal('12.5'),
                         'la virgule décimale est celle que tape la scolarité')
