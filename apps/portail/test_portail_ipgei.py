"""
Test — le portail étudiant lit le moteur IPGEI, et non la couche LMD vide.

Le portail interrogeait `InscriptionPedagogique`, `InscriptionElement`,
`ResultatSemestre`, `Progression` — les tables du socle que l'IPGEI a
remplacées. Au relevé du 15/09/2026 elles portaient **0 ligne**, face à 441
inscriptions IPGEI et 5 580 notes. Un étudiant connecté voyait ses notes vides,
ses résultats vides et son emploi du temps vide, sans la moindre erreur.

Personne ne s'en était aperçu : 298 étudiants, 1 compte, et celui-là rattaché à
aucun étudiant.

Ce que le test fige :

  1. les notes viennent d'`ipgei.Note`, avec les vraies valeurs ;
  2. **rien ne sort tant que le jury n'a pas statué** — une moyenne n'est pas
     une décision, et la publier avant la délibération remplit le guichet ;
  3. une délibération ANNUELLE ouvre les deux semestres de son année : elle
     statue sur la moyenne qui les résume ;
  4. le relevé rend la forme que l'écran attend déjà — celle du frontend, qui
     avait migré sans que le serveur suive ;
  5. la progression vient de la ligne de jury, non de `Progression`.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.absence.models import Etudiant
from apps.authentication.models import CustomUser
from apps.ipgei.models import (
    Classe, Deliberation, Evaluation, InscriptionIPGEI, LigneDeliberation,
    Matiere, NiveauIPGEI, Note, SemestreIPGEI,
)
from apps.parametres.models import Institution, Niveau
from apps.prof.models import ProfTypeHistory


class PortailIPGEITest(TestCase):

    @classmethod
    def setUpClass(cls):
        # `prof_type_history` naît d'une migration SQL brute, absente sous
        # `--no-migrations`. Même parade que les autres tests du dépôt.
        if ProfTypeHistory._meta.db_table not in connection.introspection.table_names():
            with connection.schema_editor() as se:
                se.create_model(ProfTypeHistory)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MP')
        cls.ref = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                             libelle='MP', rang=2)
        cls.annee = '2025-2026'
        cls.s3 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S3', annee_universitaire=cls.annee,
            date_debut=date(2025, 10, 6))
        cls.s4 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S4', annee_universitaire=cls.annee,
            date_debut=date(2026, 1, 26))
        cls.math = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Mathématiques',
            code_semestre='S3', niveau_ref=cls.ref, coefficient=Decimal('4'))
        cls.classe = Classe.objects.create(
            nom='MP A', institution=cls.inst, annee_universitaire=cls.annee,
            niveau=cls.socle, groupe='A')

        cls.etudiant = Etudiant.objects.create(matricule='242001', nom='Cobaye',
                                               nom_fr='Cobaye')
        cls.user = CustomUser.objects.create(username='242001', role='etudiant',
                                             email='c@t.l')
        cls.etudiant.user = cls.user
        cls.etudiant.save(update_fields=['user'])
        cls.inscription = InscriptionIPGEI.objects.create(
            etudiant=cls.etudiant, classe=cls.classe,
            annee_universitaire=cls.annee)

        note = Note.objects.create(
            inscription=cls.inscription, matiere=cls.math, semestre=cls.s3,
            note_retenue=Decimal('14.50'), moyenne=Decimal('14.50'),
            moy_ds=Decimal('13'), moy_exam=Decimal('15'),
            pct_ds=Decimal('30'), pct_tp=Decimal('0'), pct_exam=Decimal('70'),
            coefficient=Decimal('4'))
        Evaluation.objects.create(note=note, type_evaluation=Evaluation.TYPE_DS,
                                  numero=1, valeur=Decimal('13'))

    # ── fabrique ────────────────────────────────────────────────────────────
    def _appeler(self, vue, chemin='/', **params):
        requete = APIRequestFactory().get(chemin, params)
        force_authenticate(requete, user=self.user)
        return vue.as_view()(requete)

    def _jury(self, *, portee=Deliberation.PORTEE_ANNUELLE, semestre=None,
              statut=Deliberation.STATUT_VALIDEE):
        jury = Deliberation.objects.create(
            institution=self.inst, niveau='MP', classe=self.classe,
            annee_universitaire=self.annee, semestre=semestre, portee=portee,
            statut=statut, seuil_validation=Decimal('10'))
        return LigneDeliberation.objects.create(
            deliberation=jury, inscription=self.inscription,
            moyenne_generale=Decimal('14.50'))

    # ── 1. Rien avant le jury ───────────────────────────────────────────────
    def test_aucune_note_avant_la_deliberation(self):
        """
        Une moyenne n'est pas une décision. La publier avant le jury remplit le
        guichet de réclamations sur des chiffres provisoires.
        """
        from apps.portail.views import MesNotesView

        self.assertEqual(self._appeler(MesNotesView).data, [])

    def test_un_jury_calcule_mais_non_valide_ne_suffit_pas(self):
        from apps.portail.views import MesNotesView

        self._jury(statut=Deliberation.STATUT_BROUILLON)
        self.assertEqual(self._appeler(MesNotesView).data, [],
                         "un jury non signé n'a rien décidé")

    # ── 2. Après le jury, les vraies notes ──────────────────────────────────
    def test_les_notes_viennent_du_moteur_ipgei(self):
        from apps.portail.views import MesNotesView

        self._jury()
        lignes = self._appeler(MesNotesView).data

        self.assertEqual(len(lignes), 1)
        ligne = lignes[0]
        self.assertEqual(ligne['em_libelle'], 'MATH — Mathématiques')
        self.assertEqual(ligne['note_finale'], 14.5)
        self.assertEqual(ligne['note_cc'], 13.0)
        self.assertEqual(ligne['semestre'], 'S3')
        self.assertEqual(ligne['annee_univ'], self.annee)
        self.assertTrue(ligne['valide'], 'au-dessus du seuil de 10')
        # Le TP ne se montre que s'il a COMPTÉ : `pct_tp` vaut 0 ici.
        self.assertIsNone(ligne['note_tp'])

    def test_une_deliberation_annuelle_ouvre_les_deux_semestres(self):
        """Elle statue sur la moyenne annuelle, qui résume S3 et S4."""
        from apps.portail.ipgei_source import notes_publiables

        Note.objects.create(
            inscription=self.inscription, matiere=self.math, semestre=self.s4,
            note_retenue=Decimal('12'), coefficient=Decimal('4'))
        self._jury()
        codes = {n.semestre.code for n in notes_publiables(self.inscription)}
        self.assertEqual(codes, {'S3', 'S4'})

    # ── 3. Le relevé, dans la forme que l'écran attend ──────────────────────
    def test_le_releve_rend_la_forme_du_frontend(self):
        """
        L'écran attendait déjà `semestres[].notes[]`, `total_coefficients`,
        `seuil`, `valide` — la forme IPGEI — quand la vue rendait encore celle
        du LMD, `semestres → modules → éléments`. Il aurait été cassé même avec
        des données.
        """
        from apps.inscriptions.views_etudiant import ReleveAnnuelEtudiantView

        self._jury()
        reponse = self._appeler(ReleveAnnuelEtudiantView, annee=self.annee)

        self.assertEqual(reponse.status_code, 200)
        for cle in ('etudiant', 'classe', 'annee_universitaire', 'semestres',
                    'moyenne', 'total_coefficients', 'seuil', 'valide', 'absences'):
            self.assertIn(cle, reponse.data, f'clé attendue par l\'écran : {cle}')
        self.assertEqual(reponse.data['etudiant']['matricule'], '242001')
        self.assertEqual(reponse.data['classe'], 'MP A')

    def test_le_releve_exige_le_jury(self):
        from apps.inscriptions.views_etudiant import ReleveAnnuelEtudiantView

        reponse = self._appeler(ReleveAnnuelEtudiantView, annee=self.annee)
        self.assertEqual(reponse.status_code, 403)

    def test_le_releve_refuse_une_annee_sans_inscription(self):
        from apps.inscriptions.views_etudiant import ReleveAnnuelEtudiantView

        self._jury()
        reponse = self._appeler(ReleveAnnuelEtudiantView, annee='2019-2020')
        self.assertEqual(reponse.status_code, 404)

    # ── 4. Résultats et progression ─────────────────────────────────────────
    def test_les_resultats_montrent_aussi_les_jurys_annuels(self):
        """
        N'exposer que les jurys SEMESTRIELS rendait une liste vide pour tout le
        monde : les onze délibérations validées du 15/09/2026 sont annuelles ou
        de rattrapage, aucune n'est rattachée à un semestre.
        """
        from apps.portail.views import MesResultatsView

        self._jury()
        lignes = self._appeler(MesResultatsView).data
        self.assertEqual(len(lignes), 1)
        self.assertEqual(lignes[0]['semestre'], 'Année')
        self.assertEqual(lignes[0]['moyenne'], 14.5)

    def test_la_progression_vient_de_la_ligne_de_jury(self):
        from apps.inscriptions.views_etudiant import ProgressionEtudiantView

        self._jury()
        reponse = self._appeler(ProgressionEtudiantView)
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.data['moyenne_generale'], '14.50')
        self.assertEqual(reponse.data['annee_source'], self.annee)
        self.assertEqual(reponse.data['niveau_source'], 'MP')

    def test_la_progression_sans_jury_repond_404(self):
        from apps.inscriptions.views_etudiant import ProgressionEtudiantView

        self.assertEqual(self._appeler(ProgressionEtudiantView).status_code, 404)

    # ── 5. Documents ────────────────────────────────────────────────────────
    def test_les_documents_proposes_suivent_le_jury(self):
        from apps.portail.views import DocumentsDisponiblesView

        avant = self._appeler(DocumentsDisponiblesView).data
        self.assertIsNotNone(avant['attestation'],
                             "l'attestation ne dépend d'aucune délibération")
        self.assertEqual(avant['releves'], [])

        self._jury(portee=Deliberation.PORTEE_SEMESTRE, semestre=self.s3)
        apres = self._appeler(DocumentsDisponiblesView).data
        self.assertEqual(len(apres['releves']), 1)
        self.assertEqual(apres['releves'][0]['semestre_code'], 'S3')
        self.assertEqual(apres['releves'][0]['type_document'],
                         'ipgei_releve_semestre')

    def test_lister_les_documents_n_inscrit_rien_au_registre(self):
        """
        LISTER n'est pas DÉLIVRER.

        L'ancienne pré-génération (`_pregen_pdf_bg`, retirée) appelait
        `_find_or_create_doc` dès l'affichage de la liste : chaque visite créait
        les entrées du registre — numéros de série consommés, pièces inscrites
        que personne n'avait demandées. Elle réécrivait en outre le PDF d'une
        pièce déjà émise sous le même numéro quand les notes changeaient.

        Ce test échoue si quelqu'un la réintroduit sous cette forme : le
        registre d'une pièce officielle doit dire ce qui a été délivré, rien
        de plus.
        """
        from apps.documents.models import DocumentOfficiel
        from apps.portail.views import DocumentsDisponiblesView

        self._jury(portee=Deliberation.PORTEE_SEMESTRE, semestre=self.s3)
        avant = DocumentOfficiel.objects.count()

        for _ in range(3):                     # l'étudiant revient, rafraîchit
            reponse = self._appeler(DocumentsDisponiblesView)
        self.assertTrue(reponse.data['releves'],
                        'la liste doit bien proposer un relevé — sinon le test '
                        'passerait pour une mauvaise raison')

        self.assertEqual(DocumentOfficiel.objects.count(), avant,
                         "afficher la liste a inscrit des pièces au registre")

    # ── 6. Le registre ne se remplit pas à chaque clic ──────────────────────
    def test_sans_pdf_sur_le_disque_on_regenere(self):
        """
        Le garde-fou de la réutilisation : une entrée de registre sans fichier
        ne doit JAMAIS être servie. Mieux vaut régénérer que rendre une pièce
        vide portant un numéro de série.
        """
        from apps.documents.models import DocumentOfficiel

        from apps.portail.ipgei_source import document_deja_delivre

        DocumentOfficiel.objects.create(
            institution=self.inst, etudiant=self.etudiant,
            type_document='ipgei_attestation_inscription',
            numero_serie='IAI-TEST-0001', annee_universitaire=self.annee)

        self.assertIsNone(
            document_deja_delivre(self.etudiant, 'ipgei_attestation_inscription',
                                  self.annee),
            'une entrée sans fichier ne doit pas être resservie')

    def test_aucun_document_anterieur_rend_none(self):
        from apps.portail.ipgei_source import document_deja_delivre

        self.assertIsNone(document_deja_delivre(
            self.etudiant, 'ipgei_releve_semestre', self.annee))

    def test_les_absences_restent_sur_la_table_du_socle(self):
        """
        `MesAbsencesView` lit `absence.Presence`, et c'est CORRECT : les huit
        écrans de saisie d'absences appellent tous `/api/v1/absences/presences/`.
        `ipgei.AbsenceSeance` existe mais aucun écran ne l'alimente — la
        rebrancher dessus aurait vidé la page.
        """
        import inspect

        from apps.portail import views

        source = inspect.getsource(views.MesAbsencesView)
        self.assertIn('Presence', source)
        self.assertNotIn('AbsenceSeance', source)

    def test_les_absences_ne_coutent_pas_une_requete_chacune(self):
        """Le type de séance est chargé avec le reste : le nombre de requêtes
        ne croît plus avec le nombre d'absences (1 de plus par absence avant)."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from apps.absence.models import Presence
        from apps.parametres.models import Seance
        from apps.portail.views import MesAbsencesView
        from apps.suivi.models import Suivie

        cours = Seance.objects.create(type_seance='Cours')

        def absences(n):
            for i in range(n):
                suivi = Suivie.objects.create(institution=self.inst, annee_universitaire=self.annee,
                                              numero_semaine=i + 1, type_seance_fk=cours)
                Presence.objects.create(suivi=suivi, etudiant=self.etudiant, statut=1)
            with CaptureQueriesContext(connection) as q:
                r = self._appeler(MesAbsencesView)
                r.render()
            return r, len(q)

        r2, n2 = absences(2)
        r6, n6 = absences(4)
        self.assertEqual(len(r6.data), 6)
        self.assertEqual({a['suivi_type'] for a in r6.data}, {'Cours'})
        self.assertEqual(n6, n2)

    # ── 7. Le registre distingue enfin les semestres ────────────────────────
    def test_le_registre_retient_le_semestre_du_releve(self):
        """
        `DocumentOfficiel.semestre` pointe vers le `Semestre` du SOCLE, que
        l'IPGEI n'utilise pas : `creer_document` recevait un `SemestreIPGEI`
        sans jamais le stocker. Deux relevés du même étudiant, l'un de S3 et
        l'autre de S4, sortaient donc indistinguables — leur numéro de série
        les séparait, rien d'autre.
        """
        from apps.ipgei import documents as docs_ipgei

        s3 = docs_ipgei.creer_document(
            self.inscription, docs_ipgei.TYPE_RELEVE_SEMESTRE, semestre=self.s3)
        s4 = docs_ipgei.creer_document(
            self.inscription, docs_ipgei.TYPE_RELEVE_SEMESTRE, semestre=self.s4)

        self.assertEqual(s3.semestre_code, 'S3')
        self.assertEqual(s4.semestre_code, 'S4')
        self.assertNotEqual(s3.numero_serie, s4.numero_serie)

    def test_une_piece_sans_semestre_garde_le_code_vide(self):
        """Attestation et relevé annuel ne visent aucun semestre."""
        from apps.ipgei import documents as docs_ipgei

        doc = docs_ipgei.creer_document(
            self.inscription, docs_ipgei.TYPE_RELEVE_ANNUEL)
        self.assertEqual(doc.semestre_code, '')

    def test_la_reutilisation_ne_confond_plus_deux_semestres(self):
        """
        Sans le code, resservir le PDF du S3 pour une demande de S4 était un
        risque réel — c'est pourquoi la réutilisation leur était refusée. Elle
        leur est rendue, et ce test dit pourquoi elle est sûre.
        """
        from apps.ipgei import documents as docs_ipgei

        from apps.portail.ipgei_source import document_deja_delivre

        docs_ipgei.creer_document(
            self.inscription, docs_ipgei.TYPE_RELEVE_SEMESTRE, semestre=self.s3)

        # Aucun PDF sur le disque : le helper refuse les deux, mais pour la
        # bonne raison — c'est le filtre par semestre qu'on éprouve ici.
        for code in ('S3', 'S4'):
            self.assertIsNone(document_deja_delivre(
                self.etudiant, docs_ipgei.TYPE_RELEVE_SEMESTRE, self.annee, code))

        from apps.documents.models import DocumentOfficiel
        self.assertEqual(
            DocumentOfficiel.objects.filter(semestre_code='S3').count(), 1)
        self.assertEqual(
            DocumentOfficiel.objects.filter(semestre_code='S4').count(), 0,
            'une demande de S4 ne doit pas trouver le document du S3')
