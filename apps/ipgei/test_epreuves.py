"""
Test — l'épreuve, et la surimpression qu'elle projette sur l'emploi du temps.

Ce qu'on vérifie tient en une phrase : une épreuve dure 2h, 3h ou 4h, la grille
est faite de créneaux de 1h30, et aucune de ces durées n'est un multiple de
l'autre. Les créneaux recouverts doivent donc se CALCULER — et le recouvrement
partiel compte, parce qu'une classe qui compose jusqu'à 10h00 ne peut pas
commencer un cours à 09h45.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date, time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.ipgei.models import (
    Classe, EpreuveClasse, EpreuveIPGEI, Matiere, NiveauIPGEI, SemestreIPGEI,
    SessionEvaluationIPGEI, _bornes_creneau,
)
from apps.ipgei.serializers import EpreuveIPGEISerializer
from apps.ipgei.views import EpreuveIPGEIViewSet
from apps.parametres.models import Creneau, Institution, Jour, Niveau

# Les cinq créneaux réellement actifs à l'IPGEI, relevés dans la base.
CRENEAUX = [
    ('08h00-09h30', 1), ('09h45-11h15', 2), ('11h30-13h00', 3),
    ('15h10-16h40', 4), ('17h00-18h30', 5),
]


class LectureDesBornesTest(TestCase):
    """`creneau` est un libellé libre, partagé avec le socle. On le lit, donc."""

    def test_les_formes_courantes_se_lisent(self):
        self.assertEqual(_bornes_creneau('08h00-09h30'), (480, 570))
        self.assertEqual(_bornes_creneau('08:00 - 09:30'), (480, 570))
        self.assertEqual(_bornes_creneau('8h-9h30'), (480, 570))

    def test_un_libelle_illisible_ne_bloque_rien(self):
        """
        Mieux vaut ne rien neutraliser que tout neutraliser : un libellé
        fantaisiste dans le référentiel ne doit pas vider un emploi du temps.
        """
        for libelle in ('', 'Matinée', '08h00', 'de 8 à 9'):
            self.assertIsNone(_bornes_creneau(libelle), libelle)

    def test_des_bornes_a_l_envers_sont_refusees(self):
        self.assertIsNone(_bornes_creneau('11h00-09h00'))


class RecouvrementTest(TestCase):
    """Le cœur : quels créneaux une épreuve rend-elle indisponibles ?"""

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
        cls.classe = Classe.objects.create(
            institution=cls.inst, niveau=socle, groupe='A',
            annee_universitaire='2025-2026')
        cls.creneaux = [
            Creneau.objects.create(creneau=lib, ordre=ordre, duree=1.5)
            for lib, ordre in CRENEAUX
        ]

    def _epreuve(self, heure, minutes, type_epreuve='DS', numero=1):
        return EpreuveIPGEI(
            session=self.session, matiere=self.matiere, type_epreuve=type_epreuve,
            numero=numero, date=date(2025, 11, 18), heure_debut=heure,
            duree_minutes=minutes)

    def _couverts(self, epreuve):
        return [c.creneau for c in self.creneaux if epreuve.recouvre(c)]

    def test_un_ef_de_3h_couvre_les_deux_premiers_creneaux(self):
        """08h00 → 11h00 : le 3e créneau commence à 11h30, il reste libre."""
        self.assertEqual(
            self._couverts(self._epreuve(time(8, 0), 180)),
            ['08h00-09h30', '09h45-11h15'])

    def test_un_ds_de_2h_bloque_le_creneau_qu_il_effleure(self):
        """
        08h00 → 10h00. Le 2e créneau court de 09h45 à 11h15 : l'épreuve n'en
        mord que quinze minutes, et pourtant il est indisponible — la classe y
        compose encore. C'est le cas qui surprend et qui est juste.
        """
        self.assertEqual(
            self._couverts(self._epreuve(time(8, 0), 120)),
            ['08h00-09h30', '09h45-11h15'])

    def test_une_epreuve_de_4h_deborde_sur_trois_creneaux(self):
        self.assertEqual(
            self._couverts(self._epreuve(time(8, 0), 240)),
            ['08h00-09h30', '09h45-11h15', '11h30-13h00'])

    def test_une_epreuve_de_l_apres_midi_laisse_le_matin_libre(self):
        self.assertEqual(
            self._couverts(self._epreuve(time(15, 10), 120)),
            ['15h10-16h40', '17h00-18h30'])

    def test_une_epreuve_logee_dans_une_pause_ne_bloque_rien(self):
        """13h00-15h10 est une pause : aucun créneau ne la recouvre."""
        self.assertEqual(self._couverts(self._epreuve(time(13, 15), 60)), [])

    def test_le_contact_bout_a_bout_ne_compte_pas(self):
        """
        Une épreuve qui finit exactement quand un créneau commence ne le bloque
        pas : sinon toute épreuve du matin condamnerait l'après-midi entier.
        """
        epreuve = self._epreuve(time(6, 30), 90)          # 06h30 → 08h00 pile
        self.assertEqual(self._couverts(epreuve), [])

    def test_heure_fin_et_libelle_de_duree(self):
        e = self._epreuve(time(8, 0), 180)
        self.assertEqual(e.heure_fin, time(11, 0))
        self.assertEqual(e.duree_libelle, '3h')
        self.assertEqual(self._epreuve(time(8, 0), 150).duree_libelle, '2h30')

    def test_une_fin_apres_minuit_est_bornee(self):
        """
        Sans borne, 23h00 + 4h rendait 03h00 — une fin AVANT le début, que
        l'écran affichait tel quel.
        """
        self.assertEqual(self._epreuve(time(23, 0), 240).heure_fin, time(23, 59))


class ChevauchementTest(TestCase):
    """Deux épreuves à la même heure pour la même classe : la faute à éviter."""

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Test',
                                              est_principale=True)
        socle = Niveau.objects.create(niveau='MP')
        ref   = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                           libelle='MP', rang=2)
        semestre = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S3', annee_universitaire='2025-2026',
            date_debut=date(2025, 10, 6))
        cls.session = SessionEvaluationIPGEI.objects.create(
            semestre=semestre, type_session=SessionEvaluationIPGEI.TYPE_NORMALE)
        cls.maths = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Maths', code_semestre='S3',
            niveau_ref=ref, coefficient=Decimal('4'))
        cls.physique = Matiere.objects.create(
            institution=cls.inst, code='PHYS', intitule='Physique', code_semestre='S3',
            niveau_ref=ref, coefficient=Decimal('3'))
        cls.mp_a = Classe.objects.create(institution=cls.inst, niveau=socle,
                                         groupe='A', annee_universitaire='2025-2026')
        cls.mp_b = Classe.objects.create(institution=cls.inst, niveau=socle,
                                         groupe='B', annee_universitaire='2025-2026')

        cls.existante = EpreuveIPGEI.objects.create(
            session=cls.session, matiere=cls.maths, type_epreuve='DS', numero=1,
            date=date(2025, 11, 18), heure_debut=time(8, 0), duree_minutes=180)
        EpreuveClasse.objects.create(epreuve=cls.existante, classe=cls.mp_a)

    def _valider(self, **extra):
        donnees = {
            'session': self.session.pk, 'matiere': self.physique.pk,
            'type_epreuve': 'DS', 'numero': 1, 'date': '2025-11-18',
            'heure_debut': '10:00', 'duree_minutes': 120,
            'classes': [self.mp_a.pk],
        }
        donnees.update(extra)
        return EpreuveIPGEISerializer(data=donnees)

    def test_deux_epreuves_simultanees_sur_la_meme_classe_sont_refusees(self):
        s = self._valider()
        self.assertFalse(s.is_valid())
        self.assertIn('heure_debut', s.errors)
        self.assertIn('MATH', str(s.errors['heure_debut']))

    def test_une_autre_classe_ne_gene_pas(self):
        """MP B n'est pas convoquée à l'épreuve de maths : rien n'empêche."""
        self.assertTrue(self._valider(classes=[self.mp_b.pk]).is_valid())

    def test_a_la_suite_sans_chevauchement_c_est_permis(self):
        """L'épreuve de maths finit à 11h00 ; commencer à 11h00 est légitime."""
        self.assertTrue(self._valider(heure_debut='11:00').is_valid())

    def test_un_autre_jour_ne_gene_pas(self):
        self.assertTrue(self._valider(date='2025-11-19').is_valid())

    def test_modifier_une_epreuve_ne_la_fait_pas_se_heurter_a_elle_meme(self):
        s = EpreuveIPGEISerializer(
            self.existante, data={'duree_minutes': 240}, partial=True)
        self.assertTrue(s.is_valid(), s.errors)

    def test_une_epreuve_convoque_au_moins_une_classe(self):
        s = self._valider(classes=[])
        self.assertFalse(s.is_valid())
        self.assertIn('classes', s.errors)


class ConvocationsTest(TestCase):
    """La liste des classes s'écrit à plat ; la salle posée sur fiche survit."""

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Test',
                                              est_principale=True)
        socle = Niveau.objects.create(niveau='MP')
        ref   = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                           libelle='MP', rang=2)
        semestre = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S3', annee_universitaire='2025-2026',
            date_debut=date(2025, 10, 6))
        cls.session = SessionEvaluationIPGEI.objects.create(
            semestre=semestre, type_session=SessionEvaluationIPGEI.TYPE_NORMALE)
        cls.matiere = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Maths', code_semestre='S3',
            niveau_ref=ref, coefficient=Decimal('4'))
        cls.classes = [
            Classe.objects.create(institution=cls.inst, niveau=socle, groupe=g,
                                  annee_universitaire='2025-2026')
            for g in ('A', 'B', 'C')
        ]

    def _creer(self, groupes):
        s = EpreuveIPGEISerializer(data={
            'session': self.session.pk, 'matiere': self.matiere.pk,
            'type_epreuve': 'EF', 'numero': 1, 'date': '2025-12-15',
            'heure_debut': '08:00', 'duree_minutes': 180,
            'classes': [self.classes[i].pk for i in groupes],
        })
        self.assertTrue(s.is_valid(), s.errors)
        return s.save()

    def test_une_epreuve_convoque_plusieurs_classes_en_une_fois(self):
        epreuve = self._creer([0, 1, 2])
        self.assertEqual(epreuve.convocations.count(), 3)

    def test_retirer_une_classe_ne_touche_pas_aux_autres(self):
        """
        C'est tout l'intérêt de la table explicite : les lignes conservées
        gardent leur salle. Un `delete()` + `bulk_create` l'aurait effacée à
        chaque changement d'horaire.
        """
        from apps.salle.models import Salle
        epreuve = self._creer([0, 1, 2])
        salle = Salle.objects.create(nom='Amphi A')
        lien = epreuve.convocations.get(classe=self.classes[0])
        lien.salle = salle
        lien.save(update_fields=['salle'])

        s = EpreuveIPGEISerializer(epreuve, data={
            'classes': [self.classes[0].pk, self.classes[1].pk]}, partial=True)
        self.assertTrue(s.is_valid(), s.errors)
        s.save()

        self.assertEqual(epreuve.convocations.count(), 2)
        self.assertEqual(
            epreuve.convocations.get(classe=self.classes[0]).salle_id, salle.pk,
            'la salle posée sur fiche a survécu au changement de convocation')


class SurimpressionAPITest(TestCase):
    """`/epreuves/semaine/` : ce que l'écran reçoit pour dessiner le bandeau."""

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
        session = SessionEvaluationIPGEI.objects.create(
            semestre=cls.semestre, type_session=SessionEvaluationIPGEI.TYPE_NORMALE)
        matiere = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Maths', code_semestre='S3',
            niveau_ref=ref, coefficient=Decimal('4'))
        cls.classe = Classe.objects.create(
            institution=cls.inst, niveau=socle, groupe='A',
            annee_universitaire='2025-2026')
        cls.autre = Classe.objects.create(
            institution=cls.inst, niveau=socle, groupe='B',
            annee_universitaire='2025-2026')

        for lib in ('Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi'):
            Jour.objects.create(jour=lib)
        cls.creneaux = {
            lib: Creneau.objects.create(creneau=lib, ordre=ordre, duree=1.5)
            for lib, ordre in CRENEAUX
        }

        cls.semaine = cls.semestre.semaines.create(
            numero=7, date_debut=date(2025, 11, 17), date_fin=date(2025, 11, 23))

        # Mardi 18 novembre, 08h00, 3h.
        cls.epreuve = EpreuveIPGEI.objects.create(
            session=session, matiere=matiere, type_epreuve='EF', numero=1,
            date=date(2025, 11, 18), heure_debut=time(8, 0), duree_minutes=180)
        EpreuveClasse.objects.create(epreuve=cls.epreuve, classe=cls.classe)

        U = get_user_model()
        cls.admin = U.objects.create_superuser(
            username='patron', email='a@t.l', password='Admin!2612mdp')

    def _semaine(self, classe):
        vue = EpreuveIPGEIViewSet.as_view({'get': 'par_semaine'})
        demande = APIRequestFactory().get(
            f'/api/v1/ipgei/epreuves/semaine/?classe={classe.pk}&semaine={self.semaine.pk}')
        force_authenticate(demande, user=self.admin)
        return vue(demande)

    def test_l_epreuve_est_rendue_avec_ses_creneaux_et_son_jour(self):
        reponse = self._semaine(self.classe)
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(len(reponse.data), 1)

        ligne = reponse.data[0]
        self.assertEqual(ligne['jour_libelle'], 'Mardi')
        self.assertEqual(ligne['heure_fin'], '11:00:00')
        self.assertEqual(ligne['duree_libelle'], '3h')
        self.assertEqual(
            ligne['creneaux_couverts'],
            [self.creneaux['08h00-09h30'].pk, self.creneaux['09h45-11h15'].pk])
        self.assertEqual(ligne['classes_noms'], [self.classe.nom])

    def test_une_classe_non_convoquee_ne_voit_rien(self):
        self.assertEqual(len(self._semaine(self.autre).data), 0)

    def test_la_grille_pdf_porte_l_epreuve_meme_sans_aucune_seance(self):
        """
        Le cas qui a bloqué un utilisateur : une semaine d'examens ne porte
        AUCUNE séance. Le document refusait alors de se produire — or c'est
        précisément celui qu'on distribue.

        L'épreuve fusionne les colonnes que son intervalle recouvre, et la
        ligne garde exactement la largeur de la grille : une table HTML dont
        les `colspan` ne totalisent pas le nombre de colonnes se déforme.
        """
        from apps.ipgei import edt_pdf

        creneaux, lignes = edt_pdf.construire_grille(
            [], axe='classe', epreuves=[self.epreuve])

        mardi = next(l for l in lignes if l['jour'] == 'Mardi')
        largeur = sum(c['colspan'] for c in mardi['cells'])
        self.assertEqual(largeur, len(creneaux),
                         'la ligne doit faire exactement la largeur de la grille')

        fusionnee = next(c for c in mardi['cells'] if c['epreuve'])
        self.assertEqual(fusionnee['colspan'], 2, '3h couvrent deux créneaux de 1h30')
        self.assertEqual(fusionnee['epreuve']['type'], 'EF')
        self.assertIn('08h00', fusionnee['epreuve']['horaire'])
        self.assertIn('11h00', fusionnee['epreuve']['horaire'])

        # Les autres jours restent des lignes pleines, faites de cases simples.
        lundi = next(l for l in lignes if l['jour'] == 'Lundi')
        self.assertEqual(len(lundi['cells']), len(creneaux))
        self.assertTrue(all(c['colspan'] == 1 for c in lundi['cells']))

    def test_une_epreuve_hors_semaine_n_apparait_pas(self):
        self.epreuve.date = date(2025, 12, 2)
        self.epreuve.save(update_fields=['date'])
        self.assertEqual(len(self._semaine(self.classe).data), 0)
