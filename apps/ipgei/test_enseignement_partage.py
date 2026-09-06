"""
Test — enseignement partagé entre deux matières (`Matiere.partage_avec`).

La règle de l'IPGEI, précisée par la scolarité : en S3, « Mathématiques » et
« Mathématiques 2 » sont un SEUL cours, scindé en deux lignes de maquette. Les
devoirs surveillés — il peut y en avoir plusieurs — et le TP sont composés une
seule fois sur MATH et valent pour les deux ; seuls les EXAMENS diffèrent, et
chaque ligne garde son coefficient.

Ce que le test fige :
  1. la rattachée lit les DS et le TP de la porteuse, jamais les siens ;
  2. plusieurs DS partagés fonctionnent, pas seulement un ;
  3. chacune garde son examen et son coefficient — ce sont deux notes ;
  4. saisir un DS sur la porteuse recalcule la rattachée dans la foulée ;
  5. le dénominateur des DS est celui de la porteuse (épreuve manquée = zéro) ;
  6. la porteuse n'est jamais influencée par la rattachée ;
  7. sans ligne chez la porteuse, DS et TP sont absents — pas de repli sur
     d'anciennes évaluations de la rattachée ;
  8. l'API refuse un DS ou un TP saisi sur la rattachée, et sa grille renvoie
     les lignes de la porteuse pour affichage.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.absence.models import Etudiant
from apps.ipgei import services
from apps.ipgei.models import (
    Classe, Evaluation, InscriptionIPGEI, Matiere, NiveauIPGEI, Note, SemestreIPGEI,
)
from apps.ipgei.serializers import MatiereSerializer
from apps.parametres.models import Institution, Niveau


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle_mp = Niveau.objects.create(niveau='MP')
        cls.ref_mp = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                                libelle='MP', rang=2)
        cls.annee = '2025-2026'
        cls.s3 = SemestreIPGEI.objects.create(institution=cls.inst, code='S3',
                                              annee_universitaire=cls.annee,
                                              date_debut=date(2025, 9, 1))
        cls.s4 = SemestreIPGEI.objects.create(institution=cls.inst, code='S4',
                                              annee_universitaire=cls.annee,
                                              date_debut=date(2026, 1, 15))

        def matiere(code, coef, **extra):
            return Matiere.objects.create(
                institution=cls.inst, code=code, intitule=code, code_semestre='S3',
                niveau_ref=cls.ref_mp, coefficient=Decimal(coef), has_tp=True,
                pct_ds=Decimal('20'), pct_tp=Decimal('10'), pct_exam=Decimal('70'),
                **extra)

        # Mêmes proportions que la maquette réelle : MATH porte 95 h et coef 4,
        # MATH2 aucun volume et coef 5.
        cls.math  = matiere('MATH', 4, volume_cm=95)
        cls.math2 = matiere('MATH2', 5, partage_avec=cls.math)
        cls.info  = matiere('INFO', 2, volume_cm=30)      # matière ordinaire, témoin

        cls.classe = Classe.objects.create(nom='MP A', institution=cls.inst,
                                           annee_universitaire=cls.annee,
                                           niveau=cls.socle_mp, groupe='A')

    def _inscrit(self, matricule):
        etudiant = Etudiant.objects.create(matricule=matricule, nom=f'Etudiant {matricule}')
        return InscriptionIPGEI.objects.create(etudiant=etudiant, classe=self.classe,
                                               annee_universitaire=self.annee)

    def _note(self, inscription, matiere):
        note, _ = Note.objects.get_or_create(inscription=inscription, matiere=matiere,
                                             semestre=self.s3)
        return note

    def _saisir(self, note, *, ds=(), exam=(), tp=None):
        for numero, valeur in enumerate(ds, start=1):
            Evaluation.objects.update_or_create(
                note=note, type_evaluation=Evaluation.TYPE_DS, numero=numero,
                defaults={'valeur': Decimal(str(valeur))})
        for numero, valeur in enumerate(exam, start=1):
            Evaluation.objects.update_or_create(
                note=note, type_evaluation=Evaluation.TYPE_EXAM, numero=numero,
                defaults={'valeur': Decimal(str(valeur))})
        if tp is not None:
            note.note_tp = Decimal(str(tp))
            note.save(update_fields=['note_tp'])
        return note

    def _recalculer(self):
        services.recalculer_groupe(self.math.pk, self.s3.pk, self.classe.pk)


class LectureDesNotesPartageesTest(_Base):
    def test_la_rattachee_lit_les_ds_et_le_tp_de_la_porteuse(self):
        i = self._inscrit('700001')
        porteuse  = self._saisir(self._note(i, self.math),  ds=[12, 14], tp=16, exam=[10])
        rattachee = self._saisir(self._note(i, self.math2), exam=[18])

        self._recalculer()
        porteuse.refresh_from_db(); rattachee.refresh_from_db()

        self.assertEqual(rattachee.moy_ds, Decimal('13.00'), 'les DS de MATH : (12+14)/2')
        self.assertEqual(rattachee.note_tp, Decimal('16.00'), 'le TP de MATH')
        self.assertEqual(rattachee.moy_exam, Decimal('18.00'), 'son examen à elle')
        # 20 % × 13 + 10 % × 16 + 70 % × 18 = 2,60 + 1,60 + 12,60 = 16,80
        self.assertEqual(rattachee.moyenne, Decimal('16.80'))
        # La porteuse : 20 % × 13 + 10 % × 16 + 70 % × 10 = 11,20
        self.assertEqual(porteuse.moyenne, Decimal('11.20'))

    def test_plusieurs_ds_partages(self):
        # « Plusieurs DS possibles, mais la même note pour MATH et MATH2. »
        i = self._inscrit('700010')
        self._saisir(self._note(i, self.math), ds=[8, 12, 16, 20], tp=10, exam=[10])
        self._saisir(self._note(i, self.math2), exam=[10])
        self._recalculer()

        rattachee = Note.objects.get(inscription=i, matiere=self.math2)
        self.assertEqual(rattachee.moy_ds, Decimal('14.00'), '(8+12+16+20)/4')
        self.assertEqual(Note.objects.get(inscription=i, matiere=self.math).moy_ds,
                         rattachee.moy_ds, 'strictement la même note de DS')

    def test_chacune_garde_son_coefficient(self):
        i = self._inscrit('700002')
        self._saisir(self._note(i, self.math), ds=[12], tp=16, exam=[10])
        self._saisir(self._note(i, self.math2), exam=[18])
        self._recalculer()

        self.assertEqual(Note.objects.get(inscription=i, matiere=self.math).coefficient,
                         Decimal('4.00'))
        self.assertEqual(Note.objects.get(inscription=i, matiere=self.math2).coefficient,
                         Decimal('5.00'), 'MATH2 pèse 5 : ce sont bien deux notes')

    def test_les_deux_notes_comptent_dans_la_moyenne_du_semestre(self):
        i = self._inscrit('700011')
        self._saisir(self._note(i, self.math),  ds=[10], tp=10, exam=[10])   # 10,00
        self._saisir(self._note(i, self.math2), exam=[20])                    # 17,00
        self._recalculer()
        Note.objects.filter(inscription=i, matiere=self.info).delete()

        moyenne, poids, _ = services.moyenne_semestrielle(i, self.s3)
        self.assertEqual(poids, Decimal('9'), 'coef 4 + coef 5 : deux lignes distinctes')
        # (10,00 × 4 + 17,00 × 5) / 9 = 125 / 9 = 13,89
        self.assertEqual(moyenne, Decimal('13.89'))

    def test_les_ds_saisis_sur_la_rattachee_sont_ignores(self):
        # Anciens doublons saisis avant le rattachement : ils ne doivent pas
        # ressurgir, sans quoi deux étudiants n'auraient pas la même règle.
        i = self._inscrit('700003')
        self._saisir(self._note(i, self.math), ds=[10], tp=10, exam=[10])
        self._saisir(self._note(i, self.math2), ds=[20, 20], tp=20, exam=[10])
        self._recalculer()

        rattachee = Note.objects.get(inscription=i, matiere=self.math2)
        self.assertEqual(rattachee.moy_ds, Decimal('10.00'), 'ceux de MATH, pas les 20')
        self.assertEqual(rattachee.note_tp, Decimal('10.00'), 'le TP de MATH écrase le doublon')
        self.assertEqual(rattachee.moyenne, Decimal('10.00'))

    def test_sans_ligne_chez_la_porteuse_les_composantes_sont_absentes(self):
        i = self._inscrit('700004')
        Note.objects.filter(inscription=i, matiere=self.math).delete()
        self._saisir(self._note(i, self.math2), exam=[15])
        services.recalculer_groupe(self.math2.pk, self.s3.pk, self.classe.pk)

        rattachee = Note.objects.get(inscription=i, matiere=self.math2)
        self.assertIsNone(rattachee.moy_ds)
        self.assertIsNone(rattachee.note_tp)
        # DS et TP neutralisés : leur part se redistribue sur l'examen seul.
        self.assertEqual(rattachee.moyenne, Decimal('15.00'))

    def test_une_matiere_ordinaire_n_est_pas_touchee(self):
        i = self._inscrit('700005')
        self._saisir(self._note(i, self.info), ds=[8], exam=[12])
        services.recalculer_groupe(self.info.pk, self.s3.pk, self.classe.pk)
        note = Note.objects.get(inscription=i, matiere=self.info)
        self.assertEqual(note.moy_ds, Decimal('8.00'))
        # Sans TP saisi, sa part se redistribue : (20×8 + 70×12) / 90 = 11,11
        self.assertEqual(note.moyenne, Decimal('11.11'))


class DenominateurPartageTest(_Base):
    def test_le_denominateur_des_ds_est_celui_de_la_porteuse(self):
        # Deux DS tenus par la classe sous MATH ; un élève n'en a composé qu'un.
        absent  = self._inscrit('710001')
        present = self._inscrit('710002')
        self._saisir(self._note(present, self.math), ds=[10, 20], exam=[10])
        self._saisir(self._note(absent,  self.math), ds=[10],     exam=[10])
        self._saisir(self._note(present, self.math2), exam=[10])
        self._saisir(self._note(absent,  self.math2), exam=[10])
        self._recalculer()

        epreuves = services.epreuves_tenues(self.math2.pk, self.s3.pk, self.classe.pk)
        self.assertEqual(epreuves.get(Evaluation.TYPE_DS), 2,
                         'les 2 DS de MATH comptent pour MATH2')
        self.assertEqual(epreuves.get(Evaluation.TYPE_EXAM), 1,
                         "l'examen de MATH2 est le sien")
        # L'absent : (10 + 0) / 2 = 5,00 — la manquée compte zéro, ici aussi.
        self.assertEqual(Note.objects.get(inscription=absent, matiere=self.math2).moy_ds,
                         Decimal('5.00'))
        self.assertEqual(Note.objects.get(inscription=present, matiere=self.math2).moy_ds,
                         Decimal('15.00'))

    def test_porteuse_de_rend_l_identifiant_attendu(self):
        self.assertEqual(services.porteuse_de(self.math2.pk), self.math.pk)
        self.assertEqual(services.porteuse_de(self.math.pk), self.math.pk)
        self.assertEqual(services.porteuse_de(self.info.pk), self.info.pk)


class CascadeDeRecalculTest(_Base):
    def test_saisir_un_ds_sur_la_porteuse_recalcule_la_rattachee(self):
        i = self._inscrit('720001')
        self._saisir(self._note(i, self.math), ds=[10], tp=10, exam=[10])
        self._saisir(self._note(i, self.math2), exam=[10])
        self._recalculer()
        self.assertEqual(Note.objects.get(inscription=i, matiere=self.math2).moy_ds,
                         Decimal('10.00'))

        # Un second DS arrive sur la porteuse : la rattachée doit suivre SANS
        # qu'on rouvre sa grille.
        self._saisir(Note.objects.get(inscription=i, matiere=self.math), ds=[10, 20])
        self._recalculer()
        self.assertEqual(Note.objects.get(inscription=i, matiere=self.math2).moy_ds,
                         Decimal('15.00'), 'la rattachée a suivi')

    def test_recalculer_la_rattachee_ne_touche_pas_la_porteuse(self):
        i = self._inscrit('720002')
        self._saisir(self._note(i, self.math), ds=[12], tp=14, exam=[16])
        self._saisir(self._note(i, self.math2), exam=[4])
        self._recalculer()
        avant = Note.objects.get(inscription=i, matiere=self.math).moyenne

        services.recalculer_groupe(self.math2.pk, self.s3.pk, self.classe.pk)
        self.assertEqual(Note.objects.get(inscription=i, matiere=self.math).moyenne, avant)


class ValidationDuRattachementTest(_Base):
    def _valider(self, instance, donnees):
        s = MatiereSerializer(instance, data=donnees, partial=True)
        return s.is_valid(), s.errors

    def test_une_matiere_ne_se_rattache_pas_a_elle_meme(self):
        ok, erreurs = self._valider(self.math2, {'partage_avec': self.math2.pk})
        self.assertFalse(ok)
        self.assertIn('partage_avec', erreurs)

    def test_pas_de_chaine_de_rattachement(self):
        troisieme = Matiere.objects.create(
            institution=self.inst, code='MATH3', intitule='MATH3', code_semestre='S3',
            niveau_ref=self.ref_mp, coefficient=Decimal(1))
        ok, erreurs = self._valider(troisieme, {'partage_avec': self.math2.pk})
        self.assertFalse(ok, "MATH2 est déjà rattachée : on ne s'y rattache pas")
        self.assertIn('partage_avec', erreurs)

    def test_une_porteuse_ne_devient_pas_rattachee(self):
        autre = Matiere.objects.create(
            institution=self.inst, code='AUTRE', intitule='AUTRE', code_semestre='S3',
            niveau_ref=self.ref_mp, coefficient=Decimal(1))
        ok, erreurs = self._valider(self.math, {'partage_avec': autre.pk})
        self.assertFalse(ok, 'MATH porte déjà MATH2')
        self.assertIn('partage_avec', erreurs)

    def test_le_semestre_doit_etre_le_meme(self):
        ailleurs = Matiere.objects.create(
            institution=self.inst, code='MATHS4', intitule='MATHS4', code_semestre='S4',
            niveau_ref=self.ref_mp, coefficient=Decimal(1))
        ok, erreurs = self._valider(ailleurs, {'partage_avec': self.math.pk})
        self.assertFalse(ok)
        self.assertIn('partage_avec', erreurs)

    def test_un_rattachement_valide_passe(self):
        neuve = Matiere.objects.create(
            institution=self.inst, code='PHYSB', intitule='PHYSB', code_semestre='S3',
            niveau_ref=self.ref_mp, coefficient=Decimal(1))
        ok, erreurs = self._valider(neuve, {'partage_avec': self.info.pk})
        self.assertTrue(ok, erreurs)


class ApiSaisieTest(_Base):
    def _vue(self, action, methode='get', donnees=None):
        from rest_framework.test import APIRequestFactory, force_authenticate

        from apps.authentication.models import CustomUser
        from apps.ipgei.views import NoteViewSet

        admin, _ = CustomUser.objects.get_or_create(
            username='adm_partage', defaults={'email': 'a@t.l', 'role': 'admin'})
        vue = NoteViewSet(action_map={methode: action})
        fabrique = APIRequestFactory()
        requete = (fabrique.post('/', donnees, format='json') if methode == 'post'
                   else fabrique.get('/', donnees))
        force_authenticate(requete, user=admin)
        vue.request = vue.initialize_request(requete)
        vue.action = action
        vue.format_kwarg = None
        return vue

    def test_l_api_refuse_un_ds_saisi_sur_la_rattachee(self):
        i = self._inscrit('730001')
        vue = self._vue('saisie_collective', 'post', {
            'classe': self.classe.pk, 'matiere': self.math2.pk, 'semestre': self.s3.pk,
            'lignes': [{'inscription': i.pk,
                        'evaluations': [{'type_evaluation': 'ds', 'numero': 1,
                                         'valeur': '15'}]}],
        })
        with self.assertRaises(DRFValidationError) as capture:
            vue.saisie_collective(vue.request)
        self.assertIn('MATH', str(capture.exception))

    def test_l_api_refuse_un_tp_saisi_sur_la_rattachee(self):
        i = self._inscrit('730002')
        vue = self._vue('saisie_collective', 'post', {
            'classe': self.classe.pk, 'matiere': self.math2.pk, 'semestre': self.s3.pk,
            'lignes': [{'inscription': i.pk, 'note_tp': '15'}],
        })
        with self.assertRaises(DRFValidationError):
            vue.saisie_collective(vue.request)

    def test_l_api_accepte_l_examen_de_la_rattachee(self):
        i = self._inscrit('730003')
        vue = self._vue('saisie_collective', 'post', {
            'classe': self.classe.pk, 'matiere': self.math2.pk, 'semestre': self.s3.pk,
            'lignes': [{'inscription': i.pk,
                        'evaluations': [{'type_evaluation': 'exam', 'numero': 1,
                                         'valeur': '15'}]}],
        })
        reponse = vue.saisie_collective(vue.request)
        self.assertEqual(reponse.data['lignes_traitees'], 1)

    def test_la_grille_de_la_rattachee_renvoie_les_lignes_de_la_porteuse(self):
        i = self._inscrit('730004')
        self._saisir(self._note(i, self.math), ds=[11, 13], tp=15, exam=[9])
        self._note(i, self.math2)

        vue = self._vue('grille', 'get', {'classe': self.classe.pk,
                                          'matiere': self.math2.pk,
                                          'semestre': self.s3.pk})
        donnees = vue.grille(vue.request).data
        self.assertIsNotNone(donnees['partage'], 'la grille annonce le partage')
        self.assertEqual(donnees['partage']['matiere']['code'], 'MATH')
        self.assertEqual(donnees['nb_ds'], 2, 'les colonnes DS sont celles de MATH')
        ligne = next(n for n in donnees['partage']['notes'] if n['inscription'] == i.pk)
        self.assertEqual(len([e for e in ligne['evaluations']
                              if e['type_evaluation'] == 'ds']), 2)

    def test_la_grille_d_une_matiere_ordinaire_n_annonce_aucun_partage(self):
        vue = self._vue('grille', 'get', {'classe': self.classe.pk,
                                          'matiere': self.info.pk,
                                          'semestre': self.s3.pk})
        self.assertIsNone(vue.grille(vue.request).data['partage'])
