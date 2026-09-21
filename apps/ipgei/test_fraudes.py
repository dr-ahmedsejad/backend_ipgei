"""
Test — sanctions pour fraude (`apps/ipgei/fraudes.py`).

Ce que le test fige :
  1. matière entière : DS, TP, examen, rattrapage à zéro → note retenue 0 ;
  2. une seule épreuve : le reste de la ligne est intact ;
  3. seule une épreuve TENUE par la classe se sanctionne ; la copie non rendue
     est écrite à zéro, et le diviseur des camarades ne bouge pas ;
  4. motif, PV, éléments obligatoires ; note d'un autre étudiant refusée ;
     double sanction d'une même note refusée ; tout ou rien ;
  5. une note verrouillée par un jury validé est sanctionnée, et le jury est
     signalé à redélibérer — jusqu'à ce qu'il soit revalidé ;
  6. la levée rend EXACTEMENT les saisies d'origine (copie créée → supprimée) ;
  7. tant que la sanction tient, aucun chemin de saisie ne remet autre chose
     que zéro (grille, API unitaire, suppression, import, rattrapage) ;
  8. enseignement partagé : la rattachée n'offre que examen et rattrapage, et
     suit la porteuse quand ses DS sont sanctionnés ;
  9. l'API : création multipart, levée, PV authentifié, liste des sanctionnables.

Base SQLite en mémoire (siga.settings.test).
"""
import json
import shutil
import tempfile
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.absence.models import Etudiant
from apps.authentication.models import CustomUser
from apps.ipgei import fraudes, services
from apps.ipgei.models import (
    Classe, Deliberation, ElementSanctionne, Evaluation, InscriptionIPGEI,
    LigneDeliberation, Matiere, NiveauIPGEI, Note, SanctionFraude, SemestreIPGEI,
)
from apps.parametres.models import Institution, Niveau

MEDIA = tempfile.mkdtemp(prefix='fraudes_test_')
D = Decimal


def _pv(nom='pv.pdf'):
    return SimpleUploadedFile(nom, b'%PDF-1.4 test', content_type='application/pdf')


@override_settings(MEDIA_ROOT=MEDIA)
class _Base(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MP')
        cls.ref = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                             libelle='MP', rang=2)
        cls.annee = '2025-2026'
        cls.s3 = SemestreIPGEI.objects.create(institution=cls.inst, code='S3',
                                              annee_universitaire=cls.annee,
                                              date_debut=date(2025, 9, 1))

        def matiere(code, **extra):
            return Matiere.objects.create(
                institution=cls.inst, code=code, intitule=code, code_semestre='S3',
                niveau_ref=cls.ref, coefficient=D('4'), has_tp=True,
                pct_ds=D('20'), pct_tp=D('10'), pct_exam=D('70'), **extra)

        cls.math = matiere('MATH', volume_cm=95)
        cls.math2 = matiere('MATH2', partage_avec=cls.math)
        cls.info = matiere('INFO', volume_cm=30)
        cls.classe = Classe.objects.create(nom='MP A', institution=cls.inst,
                                           annee_universitaire=cls.annee,
                                           niveau=cls.socle, groupe='A')
        cls.admin = CustomUser.objects.create_user(
            username='scolarite', email='s@t.l', password='Scol!2612mdp', role='admin')

    # ── Fabrication ─────────────────────────────────────────────────────────
    def _inscrit(self, matricule):
        e = Etudiant.objects.create(matricule=matricule, nom=f'Etudiant {matricule}')
        return InscriptionIPGEI.objects.create(etudiant=e, classe=self.classe,
                                               annee_universitaire=self.annee)

    def _note(self, inscription, matiere, *, ds=(), exam=(), tp=None, ratt=None):
        note, _ = Note.objects.get_or_create(inscription=inscription, matiere=matiere,
                                             semestre=self.s3)
        for numero, v in ds:
            Evaluation.objects.update_or_create(
                note=note, type_evaluation='ds', numero=numero, defaults={'valeur': D(str(v))})
        for numero, v in exam:
            Evaluation.objects.update_or_create(
                note=note, type_evaluation='exam', numero=numero, defaults={'valeur': D(str(v))})
        note.note_tp = None if tp is None else D(str(tp))
        note.note_rattrapage = None if ratt is None else D(str(ratt))
        note.save()
        return note

    def _recalculer(self, matiere):
        services.recalculer_groupe(matiere.pk, self.s3.pk, self.classe.pk)

    def _sanctionner(self, inscription, elements, **kw):
        donnees = dict(inscription=inscription, nature=SanctionFraude.NATURE_TELEPHONE,
                       date_faits=date(2026, 1, 10), motif='Téléphone allumé pendant l’épreuve',
                       pv_deliberation=_pv(), elements=elements, utilisateur=self.admin)
        donnees.update(kw)
        return fraudes.sanctionner(**donnees)

    def _etat(self, note):
        note.refresh_from_db()
        return {
            'evals': sorted((e.type_evaluation, e.numero, e.valeur) for e in note.evaluations.all()),
            'tp': note.note_tp, 'ratt': note.note_rattrapage, 'retenue': note.note_retenue,
        }

    def setUp(self):
        # Deux étudiants : A fraude, B est le témoin du diviseur de classe.
        self.a = self._inscrit('242001')
        self.b = self._inscrit('242002')
        self.na = self._note(self.a, self.info, ds=[(1, 14), (2, 12)], exam=[(1, 11)], tp=15, ratt=13)
        self.nb = self._note(self.b, self.info, ds=[(1, 10), (2, 10)], exam=[(1, 10)], tp=10)
        self._recalculer(self.info)
        self.na.refresh_from_db(); self.nb.refresh_from_db()


class SanctionMatiereEtEpreuveTest(_Base):
    def test_matiere_entiere_met_tout_a_zero(self):
        s = self._sanctionner(self.a, [{'note': self.na.pk, 'portee': 'matiere'}])
        etat = self._etat(self.na)
        self.assertEqual(etat['evals'], [('ds', 1, D('0')), ('ds', 2, D('0')), ('exam', 1, D('0'))])
        self.assertEqual((etat['tp'], etat['ratt'], etat['retenue']), (D('0'), D('0'), D('0')))
        self.assertEqual(s.elements.count(), 1)
        self.assertEqual(s.elements.get().note_retenue_avant, D('13.00'))

    def test_une_epreuve_ne_touche_que_elle(self):
        avant = self._etat(self.na)
        self._sanctionner(self.a, [{'note': self.na.pk, 'portee': 'epreuve',
                                    'composante': 'ds', 'numero': 2}])
        apres = self._etat(self.na)
        self.assertEqual(apres['evals'], [('ds', 1, D('14')), ('ds', 2, D('0')), ('exam', 1, D('11'))])
        self.assertEqual((apres['tp'], apres['ratt']), (avant['tp'], avant['ratt']))

    def test_le_temoin_ne_bouge_pas(self):
        avant = self._etat(self.nb)
        self._sanctionner(self.a, [{'note': self.na.pk, 'portee': 'matiere'}])
        self.assertEqual(self._etat(self.nb), avant)

    def test_copie_non_rendue_ecrite_a_zero_sans_changer_le_diviseur(self):
        # B a passé un DS 3 ; A ne l'a pas rendu — il compte déjà zéro.
        Evaluation.objects.create(note=self.nb, type_evaluation='ds', numero=3, valeur=D('10'))
        self._recalculer(self.info)
        tenues = services.epreuves_tenues(self.info.pk, self.s3.pk, self.classe.pk)
        self._sanctionner(self.a, [{'note': self.na.pk, 'portee': 'epreuve',
                                    'composante': 'ds', 'numero': 3}])
        self.assertTrue(self.na.evaluations.filter(type_evaluation='ds', numero=3,
                                                   valeur=D('0')).exists())
        self.assertEqual(services.epreuves_tenues(self.info.pk, self.s3.pk, self.classe.pk), tenues)

    def test_epreuve_non_tenue_refusee(self):
        with self.assertRaises(ValidationError):
            self._sanctionner(self.a, [{'note': self.na.pk, 'portee': 'epreuve',
                                        'composante': 'ds', 'numero': 3}])
        self.assertFalse(Evaluation.objects.filter(numero=3).exists())
        self.assertEqual(SanctionFraude.objects.count(), 0)

    def test_rattrapage_absent_non_sanctionnable(self):
        with self.assertRaises(ValidationError):
            self._sanctionner(self.b, [{'note': self.nb.pk, 'portee': 'epreuve',
                                        'composante': 'rattrapage'}])


class ControlesTest(_Base):
    def test_motif_pv_elements_obligatoires(self):
        el = [{'note': self.na.pk, 'portee': 'matiere'}]
        for kw in ({'motif': '  '}, {'pv_deliberation': None}, {'elements': []},
                   {'nature': 'inconnue'}, {'date_faits': date.today() + timedelta(days=2)}):
            with self.subTest(kw=kw), self.assertRaises(ValidationError):
                self._sanctionner(self.a, el if 'elements' not in kw else kw['elements'],
                                  **{k: v for k, v in kw.items() if k != 'elements'})
        self.assertEqual(self._etat(self.na)['retenue'], D('13.00'))

    def test_note_d_un_autre_etudiant_refusee(self):
        with self.assertRaises(ValidationError):
            self._sanctionner(self.a, [{'note': self.nb.pk, 'portee': 'matiere'}])

    def test_double_sanction_refusee(self):
        self._sanctionner(self.a, [{'note': self.na.pk, 'portee': 'epreuve',
                                    'composante': 'exam', 'numero': 1}])
        with self.assertRaises(ValidationError):
            self._sanctionner(self.a, [{'note': self.na.pk, 'portee': 'epreuve',
                                        'composante': 'ds', 'numero': 1}])

    def test_tout_ou_rien(self):
        n2 = self._note(self.a, self.math, ds=[(1, 15)], exam=[(1, 15)])
        self._recalculer(self.math)
        avant = self._etat(n2)
        with self.assertRaises(ValidationError):
            self._sanctionner(self.a, [{'note': n2.pk, 'portee': 'matiere'},
                                       {'note': self.na.pk, 'portee': 'epreuve',
                                        'composante': 'ds', 'numero': 9}])
        self.assertEqual(self._etat(n2), avant)
        self.assertEqual(SanctionFraude.objects.count(), 0)


class JuryEtVerrouTest(_Base):
    def _jury_valide(self, portee=Deliberation.PORTEE_SEMESTRE):
        delib = Deliberation.objects.create(
            institution=self.inst, niveau='MP', classe=self.classe,
            annee_universitaire=self.annee, portee=portee,
            semestre=self.s3 if portee == Deliberation.PORTEE_SEMESTRE else None,
            statut=Deliberation.STATUT_CALCULEE)
        for i in (self.a, self.b):
            LigneDeliberation.objects.create(
                deliberation=delib, inscription=i, moyenne_generale=D('12'),
                decision=LigneDeliberation.DECISION_ADMIS,
                decision_auto=LigneDeliberation.DECISION_ADMIS)
        services.valider_deliberation(delib, self.admin)
        delib.refresh_from_db()
        return delib

    def test_note_verrouillee_sanctionnee_et_jury_signale(self):
        delib = self._jury_valide()
        self.na.refresh_from_db()
        self.assertTrue(self.na.verrouillee)
        s = self._sanctionner(self.a, [{'note': self.na.pk, 'portee': 'matiere'}])
        self.assertEqual(self._etat(self.na)['retenue'], D('0'))
        self.assertTrue(Note.objects.get(pk=self.na.pk).verrouillee, 'le verrou reste')
        self.assertEqual([d.pk for d in fraudes.jurys_a_redeliberer(s)], [delib.pk])
        self.assertEqual([x.pk for x in fraudes.sanctions_non_vues(delib)], [s.pk])

    def test_jury_revalide_n_est_plus_signale(self):
        # Jury annuel : un jury de semestre ne mémorise pas l'état des
        # inscriptions et ne se dévalide pas (comportement existant).
        delib = self._jury_valide(Deliberation.PORTEE_ANNUELLE)
        s = self._sanctionner(self.a, [{'note': self.na.pk, 'portee': 'matiere'}])
        services.devalider_deliberation(Deliberation.objects.get(pk=delib.pk))
        services.valider_deliberation(Deliberation.objects.get(pk=delib.pk), self.admin)
        delib.refresh_from_db()
        self.assertEqual(fraudes.jurys_a_redeliberer(s), [])
        self.assertEqual(fraudes.sanctions_non_vues(delib), [])

    def test_la_levee_resignale_le_jury(self):
        delib = self._jury_valide()
        s = self._sanctionner(self.a, [{'note': self.na.pk, 'portee': 'matiere'}])
        # Le jury revoit, puis la sanction est levée : il a de nouveau statué
        # sur d'autres notes que celles en base.
        Deliberation.objects.filter(pk=delib.pk).update(
            date_validation=s.cree_le + timedelta(microseconds=1))
        self.assertEqual(fraudes.jurys_a_redeliberer(s), [])
        self.assertEqual(fraudes.sanctions_non_vues(Deliberation.objects.get(pk=delib.pk)), [])
        fraudes.lever(s, motif_levee='Recours accepté', utilisateur=self.admin)
        s.refresh_from_db()
        self.assertEqual([d.pk for d in fraudes.jurys_a_redeliberer(s)], [delib.pk])
        self.assertEqual([x.pk for x in fraudes.sanctions_non_vues(
            Deliberation.objects.get(pk=delib.pk))], [s.pk])

    def test_jury_valide_avant_sans_ces_notes_non_signale(self):
        delib = self._jury_valide()
        autre = self._note(self.a, self.math, ds=[(1, 12)], exam=[(1, 12)])
        # La note MATH a été créée après le jury : elle n'est pas verrouillée,
        # mais le jury de S3 la couvre bien — il doit être signalé.
        s = self._sanctionner(self.a, [{'note': autre.pk, 'portee': 'matiere'}])
        self.assertEqual([d.pk for d in fraudes.jurys_a_redeliberer(s)], [delib.pk])
        # Un jury d'un autre semestre ne l'est pas.
        s4 = SemestreIPGEI.objects.create(institution=self.inst, code='S4',
                                          annee_universitaire=self.annee,
                                          date_debut=date(2026, 1, 15))
        Deliberation.objects.filter(pk=delib.pk).update(semestre=s4)
        self.assertEqual(fraudes.jurys_a_redeliberer(s), [])


class LeveeTest(_Base):
    def test_levee_restaure_exactement(self):
        Evaluation.objects.create(note=self.nb, type_evaluation='ds', numero=3, valeur=D('10'))
        self._recalculer(self.info)
        avant = self._etat(self.na)
        s = self._sanctionner(self.a, [{'note': self.na.pk, 'portee': 'matiere'}])
        self.assertEqual(self._etat(self.na)['retenue'], D('0'))
        resultat = fraudes.lever(s, motif_levee='Erreur de matricule', utilisateur=self.admin)
        self.assertEqual(self._etat(self.na), avant)
        self.assertFalse(self.na.evaluations.filter(numero=3).exists(), 'copie créée → supprimée')
        self.assertEqual(resultat['ecarts'], [])
        s.refresh_from_db()
        self.assertEqual(s.statut, SanctionFraude.STATUT_LEVEE)
        self.assertEqual(s.levee_par, self.admin)
        self.assertTrue(ElementSanctionne.objects.filter(sanction=s).exists(), 'la fiche reste')

    def test_levee_d_epreuves_separees(self):
        avant = self._etat(self.na)
        s = self._sanctionner(self.a, [
            {'note': self.na.pk, 'portee': 'epreuve', 'composante': 'ds', 'numero': 1},
            {'note': self.na.pk, 'portee': 'epreuve', 'composante': 'tp'},
            {'note': self.na.pk, 'portee': 'epreuve', 'composante': 'rattrapage'}])
        self.assertEqual(s.elements.count(), 3)
        fraudes.lever(s, motif_levee='Recours', utilisateur=self.admin)
        self.assertEqual(self._etat(self.na), avant)

    def test_levee_motif_obligatoire_et_unique(self):
        s = self._sanctionner(self.a, [{'note': self.na.pk, 'portee': 'matiere'}])
        with self.assertRaises(ValidationError):
            fraudes.lever(s, motif_levee=' ')
        fraudes.lever(s, motif_levee='ok')
        with self.assertRaises(ValidationError):
            fraudes.lever(s, motif_levee='encore')

    def test_apres_levee_une_nouvelle_sanction_est_possible(self):
        s = self._sanctionner(self.a, [{'note': self.na.pk, 'portee': 'matiere'}])
        fraudes.lever(s, motif_levee='ok')
        self._sanctionner(self.a, [{'note': self.na.pk, 'portee': 'matiere'}])


class GardesDeSaisieTest(_Base):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)
        self.sanction = self._sanctionner(self.a, [
            {'note': self.na.pk, 'portee': 'epreuve', 'composante': 'ds', 'numero': 1},
            {'note': self.na.pk, 'portee': 'epreuve', 'composante': 'tp'}])

    def _grille(self, ligne):
        return self.client.post('/api/v1/ipgei/notes/saisie-collective/', {
            'classe': self.classe.pk, 'matiere': self.info.pk, 'semestre': self.s3.pk,
            'lignes': [{'inscription': self.a.pk, **ligne}]}, format='json')

    def test_grille_refuse_une_valeur_non_nulle(self):
        r = self._grille({'evaluations': [{'type_evaluation': 'ds', 'numero': 1, 'valeur': '14'}]})
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn('fraude', json.dumps(r.data, ensure_ascii=False))
        self.assertEqual(self.na.evaluations.get(type_evaluation='ds', numero=1).valeur, D('0'))

    def test_grille_refuse_l_effacement(self):
        r = self._grille({'evaluations': [{'type_evaluation': 'ds', 'numero': 1, 'valeur': None}]})
        self.assertEqual(r.status_code, 400, r.data)

    def test_grille_refuse_le_tp(self):
        self.assertEqual(self._grille({'note_tp': '15'}).status_code, 400)

    def test_grille_accepte_zero_et_le_reste(self):
        r = self._grille({'note_tp': '0', 'evaluations': [
            {'type_evaluation': 'ds', 'numero': 1, 'valeur': '0'},
            {'type_evaluation': 'ds', 'numero': 2, 'valeur': '16'}]})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.na.evaluations.get(type_evaluation='ds', numero=2).valeur, D('16'))

    def test_api_evaluation_unitaire(self):
        ev = self.na.evaluations.get(type_evaluation='ds', numero=1)
        url = f'/api/v1/ipgei/evaluations/{ev.pk}/'
        self.assertEqual(self.client.patch(url, {'valeur': '12'}, format='json').status_code, 400)
        self.assertEqual(self.client.patch(url, {'numero': 5}, format='json').status_code, 400)
        self.assertEqual(self.client.delete(url).status_code, 400)
        self.assertTrue(Evaluation.objects.filter(pk=ev.pk, valeur=D('0'), numero=1).exists())

    def test_api_note_unitaire(self):
        r = self.client.patch(f'/api/v1/ipgei/notes/{self.na.pk}/', {'note_tp': '18'}, format='json')
        self.assertEqual(r.status_code, 400, r.data)

    def test_rattrapage_par_jury(self):
        s = self._sanctionner(self.b, [{'note': self.nb.pk, 'portee': 'matiere'}])
        self.assertIsNotNone(s)
        note = Note.objects.get(pk=self.nb.pk)
        with self.assertRaises(ValidationError):
            fraudes.controler_saisie_sanctionnee(note, 'rattrapage', None, D('12'))
        fraudes.controler_saisie_sanctionnee(note, 'rattrapage', None, D('0'))

    def test_import_ecarte_la_case_et_le_dit(self):
        import openpyxl
        from io import BytesIO
        from django.core.files.uploadedfile import SimpleUploadedFile as Fichier
        from apps.ipgei import import_notes

        classeur = openpyxl.Workbook()
        f = classeur.active
        f.append(['Matière :', self.info.intitule])
        f.append(['Matricule', 'Note /20'])
        f.append(['242001', 14])        # DS 1 sanctionné : écarté
        f.append(['242002', 17])        # le témoin : versé
        tampon = BytesIO(); classeur.save(tampon)
        bilan = import_notes.importer_classeur(
            Fichier('ds1.xlsx', tampon.getvalue()), semestre=self.s3, classe=self.classe,
            type_note=import_notes.TYPE_DS, numero=1, utilisateur=self.admin)
        self.assertEqual(bilan['saisies'], 1, bilan)
        self.assertEqual(len(bilan['erreurs']), 1, bilan)
        self.assertIn('fraude', bilan['erreurs'][0]['motif'])
        self.assertEqual(self.na.evaluations.get(type_evaluation='ds', numero=1).valeur, D('0'))
        self.assertEqual(self.nb.evaluations.get(type_evaluation='ds', numero=1).valeur, D('17'))

    def test_apres_levee_la_saisie_est_libre(self):
        fraudes.lever(self.sanction, motif_levee='Recours')
        r = self._grille({'evaluations': [{'type_evaluation': 'ds', 'numero': 1, 'valeur': '15'}]})
        self.assertEqual(r.status_code, 200, r.data)


class EnseignementPartageTest(_Base):
    def setUp(self):
        super().setUp()
        self.porteuse = self._note(self.a, self.math, ds=[(1, 16)], exam=[(1, 12)], tp=14)
        self.rattachee = self._note(self.a, self.math2, exam=[(1, 18)])
        self._note(self.b, self.math, ds=[(1, 10)], exam=[(1, 10)], tp=10)
        self._recalculer(self.math)

    def test_la_rattachee_n_offre_que_examen_et_rattrapage(self):
        # Un DS resté sur la rattachée d'avant le rattachement ne compte pas
        # (cf. `note_porteuse`) : il ne doit pas non plus s'offrir à la sanction.
        Evaluation.objects.create(note=self.rattachee, type_evaluation='ds', numero=1,
                                  valeur=D('9'))
        composantes = {e['composante'] for e in fraudes.epreuves_sanctionnables(
            Note.objects.get(pk=self.rattachee.pk))}
        self.assertEqual(composantes, {'exam'})

    def test_sanctionner_le_ds_de_la_porteuse_touche_la_rattachee(self):
        self.rattachee.refresh_from_db()
        self.assertEqual(self.rattachee.moy_ds, D('16.00'))
        self._sanctionner(self.a, [{'note': self.porteuse.pk, 'portee': 'epreuve',
                                    'composante': 'ds', 'numero': 1}])
        self.rattachee.refresh_from_db()
        self.assertEqual(self.rattachee.moy_ds, D('0.00'))


@override_settings(MEDIA_ROOT=MEDIA)
class ApiTest(_Base):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def _creer(self, **extra):
        donnees = {'inscription': self.a.pk, 'nature': 'telephone', 'date_faits': '2026-01-10',
                   'motif': 'Téléphone', 'pv_deliberation': _pv(),
                   'elements': json.dumps([{'note': self.na.pk, 'portee': 'matiere'}])}
        donnees.update(extra)
        return self.client.post('/api/v1/ipgei/fraudes/', donnees, format='multipart')

    def test_creation_lecture_levee(self):
        r = self._creer()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['etudiant_matricule'], '242001')
        self.assertEqual(r.data['elements'][0]['note_retenue'], '0.00')

        pv = self.client.get(f"/api/v1/ipgei/fraudes/{r.data['id']}/pv/")
        self.assertEqual(pv.status_code, 200)
        self.assertEqual(b''.join(pv.streaming_content), b'%PDF-1.4 test')

        liste = self.client.get('/api/v1/ipgei/fraudes/?statut=appliquee')
        self.assertEqual(liste.data['count'], 1)

        lev = self.client.post(f"/api/v1/ipgei/fraudes/{r.data['id']}/lever/",
                               {'motif_levee': 'Recours'}, format='json')
        self.assertEqual(lev.status_code, 200, lev.data)
        self.assertEqual(lev.data['statut'], 'levee')
        self.assertEqual(lev.data['ecarts'], [])

    def test_pv_refuse_si_format_interdit(self):
        r = self._creer(pv_deliberation=SimpleUploadedFile('pv.exe', b'MZ',
                                                           content_type='application/x-msdownload'))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(SanctionFraude.objects.count(), 0)

    def test_pv_manquant(self):
        donnees = {'inscription': self.a.pk, 'nature': 'telephone', 'date_faits': '2026-01-10',
                   'motif': 'x', 'elements': json.dumps([{'note': self.na.pk, 'portee': 'matiere'}])}
        r = self.client.post('/api/v1/ipgei/fraudes/', donnees, format='multipart')
        self.assertEqual(r.status_code, 400)

    def test_pas_de_suppression(self):
        r = self._creer()
        self.assertEqual(self.client.delete(f"/api/v1/ipgei/fraudes/{r.data['id']}/").status_code, 405)

    def test_sanctionnables(self):
        r = self.client.get(f'/api/v1/ipgei/fraudes/sanctionnables/?inscription={self.a.pk}')
        self.assertEqual(r.status_code, 200, r.data)
        ligne = next(n for n in r.data['notes'] if n['matiere_code'] == 'INFO')
        self.assertEqual([e['libelle'] for e in ligne['epreuves']],
                         ['Devoir surveillé 1', 'Devoir surveillé 2', 'Examen 1', 'TP', 'Rattrapage'])

    def test_anonyme_refuse(self):
        self.client.force_authenticate(user=None)
        self.assertIn(self.client.get('/api/v1/ipgei/fraudes/').status_code, (401, 403))

    def test_deliberation_expose_les_fraudes_non_vues(self):
        delib = Deliberation.objects.create(
            institution=self.inst, niveau='MP', classe=self.classe,
            annee_universitaire=self.annee, portee=Deliberation.PORTEE_SEMESTRE,
            semestre=self.s3, statut=Deliberation.STATUT_CALCULEE)
        LigneDeliberation.objects.create(
            deliberation=delib, inscription=self.a, moyenne_generale=D('12'),
            decision=LigneDeliberation.DECISION_ADMIS, decision_auto=LigneDeliberation.DECISION_ADMIS)
        services.valider_deliberation(delib, self.admin)
        self.assertEqual(self._creer().status_code, 201)
        r = self.client.get(f'/api/v1/ipgei/deliberations/{delib.pk}/')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(len(r.data['fraudes_non_vues']), 1)
        self.assertEqual(r.data['fraudes_non_vues'][0]['etudiant_matricule'], '242001')
