"""
Test — une épreuve se pointe dans « Suivi → Remplissage ».

Le Suivi ne lit que `emplois.Emplois` ; une épreuve n'est pas une séance et n'y
figurait pas. Elle restait donc impointable : rien à cocher le jour de
l'examen, et aucune heure de surveillance nulle part.

La chaîne est : épreuve → Emplois → (génération) → Suivie + SuiviePointage →
écran de remplissage. C'est le premier maillon qui manquait ; ce test le
verrouille, ainsi que les deux décisions qui le gouvernent :

  * UNE ligne par (épreuve, classe), ancrée sur le PREMIER créneau recouvert —
    une par créneau aurait demandé trois pointages pour un seul examen ;
  * ni enseignant ni salle, comme partout ailleurs.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date, time
from decimal import Decimal

from django.test import TestCase

from apps.emplois.models import Emplois
from apps.ipgei.miroir import projeter_semaine_reelle
from apps.ipgei.models import (
    Classe, EpreuveClasse, EpreuveIPGEI, Matiere, NiveauIPGEI, SeanceReelle,
    SemaineIPGEI, SemestreIPGEI, SessionEvaluationIPGEI,
)
from apps.parametres.models import Creneau, Institution, Jour, Niveau, Seance

CRENEAUX = [('08h00-09h30', 1), ('09h45-11h15', 2), ('11h30-13h00', 3),
            ('15h10-16h40', 4), ('17h00-18h30', 5)]


class EpreuvePointableTest(TestCase):
    @classmethod
    def setUpClass(cls):
        # Même précaution que les tests voisins : la table de liaison du
        # pointage peut manquer selon la fixture de session.
        from django.db import connection

        from apps.suivi.models import SuiviePointageDepartement

        if (SuiviePointageDepartement._meta.db_table
                not in connection.introspection.table_names()):
            with connection.schema_editor() as se:
                se.create_model(SuiviePointageDepartement)
        super().setUpClass()

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
        for lib, ordre in CRENEAUX:
            Creneau.objects.create(creneau=lib, ordre=ordre, duree=1.5)
        for code in ('CM', 'DS', 'EF', 'ER'):
            Seance.objects.create(type_seance=code)

        cls.semaine = cls.semestre.semaines.create(
            numero=7, date_debut=date(2025, 11, 17), date_fin=date(2025, 11, 23))

    def _epreuve(self, heure=time(8, 0), minutes=240, classes=None, jour=None):
        """Par défaut : mardi 18 novembre, 08h00, 4h — trois créneaux couverts."""
        epreuve = EpreuveIPGEI.objects.create(
            session=self.session, matiere=self.matiere, type_epreuve='EF',
            numero=1, date=jour or date(2025, 11, 18),
            heure_debut=heure, duree_minutes=minutes)
        for classe in (classes if classes is not None else [self.mp_a]):
            EpreuveClasse.objects.create(epreuve=epreuve, classe=classe)
        return epreuve

    def _projeter(self):
        return projeter_semaine_reelle('2025-2026', 'I', 7)

    # ── La projection ────────────────────────────────────────────────────────
    def test_une_semaine_sans_aucune_seance_projette_quand_meme_l_epreuve(self):
        """
        Le maillon qui manquait : la projection s'arrêtait net sans séance, et
        une semaine d'examens n'en a aucune.
        """
        self._epreuve()
        self.assertEqual(SeanceReelle.objects.count(), 0)

        self.assertEqual(self._projeter(), 1)
        self.assertEqual(Emplois.objects.count(), 1)

    def test_une_ligne_par_classe_convoquee(self):
        self._epreuve(classes=[self.mp_a, self.mp_b])
        self._projeter()
        self.assertEqual(
            sorted(Emplois.objects.values_list('departement_id', flat=True)),
            sorted([self.mp_a.pk, self.mp_b.pk]))

    def test_une_seule_ligne_malgre_trois_creneaux_recouverts(self):
        """
        Une épreuve de 4h couvre trois créneaux. Une ligne par créneau aurait
        demandé trois pointages pour un seul examen, et compté 4h30 de
        surveillance au lieu de 4h.
        """
        epreuve = self._epreuve(minutes=240)
        couverts = [c for c in Creneau.objects.all() if epreuve.recouvre(c)]
        self.assertEqual(len(couverts), 3, 'trois créneaux bien recouverts')

        self._projeter()
        self.assertEqual(Emplois.objects.count(), 1)

    def test_la_ligne_s_ancre_sur_le_PREMIER_creneau_recouvert(self):
        self._epreuve(heure=time(8, 0), minutes=240)
        self._projeter()
        ligne = Emplois.objects.get()
        self.assertEqual(ligne.creneau_fk.creneau, '08h00-09h30')

    def test_le_jour_se_deduit_de_la_date(self):
        self._epreuve()                                    # 18/11/2025 = mardi
        self._projeter()
        self.assertEqual(Emplois.objects.get().jour_fk.jour, 'Mardi')

    def test_ni_enseignant_ni_salle(self):
        """
        Le surveillant relève de la vacation, la salle des fiches. L'écran de
        remplissage affiche « — », ce qui est exact.
        """
        self._epreuve()
        self._projeter()
        ligne = Emplois.objects.get()
        self.assertIsNone(ligne.prof_id)
        self.assertIsNone(ligne.salle_id)

    def test_la_ligne_porte_le_type_et_la_matiere(self):
        """
        Les deux champs dont dépend l'écran de remplissage : il écarte toute
        ligne sans type ou sans intitulé de module.
        """
        self._epreuve()
        self._projeter()
        ligne = Emplois.objects.get()
        self.assertEqual(ligne.type_seance_fk.type_seance, 'EF')
        self.assertIsNotNone(ligne.em_id, "l'EM miroir de la matière est requis")
        self.assertEqual(ligne.em.intitule, 'Mathématiques')
        self.assertFalse(ligne.type_seance_fk.is_special,
                         'un type spécial serait écarté du remplissage')

    def test_une_epreuve_hors_semaine_n_est_pas_projetee(self):
        self._epreuve(jour=date(2025, 12, 2))
        self.assertEqual(self._projeter(), 0)

    def test_une_epreuve_logee_dans_une_pause_n_est_pas_projetee(self):
        """
        13h00-15h10 ne recouvre aucun créneau : il n'y a aucune case où se
        pointer, et mieux vaut rien qu'un ancrage arbitraire.
        """
        self._epreuve(heure=time(13, 15), minutes=60)
        self.assertEqual(self._projeter(), 0)

    # ── Cohabitation avec les séances ────────────────────────────────────────
    def test_seances_et_epreuves_cohabitent(self):
        SeanceReelle.objects.create(
            classe=self.mp_a, semaine=self.semaine, jour=Jour.objects.first(),
            creneau=Creneau.objects.first(), matiere=self.matiere,
            type_seance=Seance.objects.get(type_seance='CM'))
        self._epreuve()

        self.assertEqual(self._projeter(), 2)
        self.assertEqual(
            sorted(Emplois.objects.values_list('type_seance_fk__type_seance', flat=True)),
            ['CM', 'EF'])

    def test_une_reprojection_ne_double_pas_les_lignes(self):
        """La projection remplace : deux passages ne font pas deux épreuves."""
        self._epreuve()
        self._projeter()
        self._projeter()
        self.assertEqual(Emplois.objects.count(), 1)

    # ── La chaîne entière, jusqu'au pointage ─────────────────────────────────
    def _generer_le_suivi(self):
        """
        Appelle la VRAIE génération — celle du bouton « Ajouter un suivi ».

        Rejouer sa logique dans le test aurait prouvé le test, pas le produit :
        c'est précisément le passage d'`Emplois` à `Suivie` qu'on veut voir
        franchi par une ligne sans enseignant ni salle.
        """
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIRequestFactory, force_authenticate

        from apps.parametres.models import Semaine
        from apps.suivi.views import SuivieViewSet

        # Le socle date la semaine par `parametres.Semaine`, une ligne par JOUR.
        for index, jour in enumerate(Jour.objects.all().order_by('id')):
            Semaine.objects.get_or_create(
                annee_universitaire='2025-2026', numero_semaine=7,
                type_semestre='I', jour_fk=jour,
                defaults={'date': date(2025, 11, 17 + index)},
            )

        U = get_user_model()
        admin = U.objects.create_superuser(username='gen', email='g@t.l',
                                           password='Admin!2612mdp')
        vue = SuivieViewSet.as_view({'post': 'ajouter_suivie'})
        demande = APIRequestFactory().post('/api/v1/suivi/suivies/ajouter/', {
            'annee_universitaire': '2025-2026',
            'numero_semaine': 7,
            'type_semestre': 'I',
        }, format='json')
        force_authenticate(demande, user=admin)
        return vue(demande)

    def test_l_epreuve_devient_pointable_dans_le_suivi(self):
        """
        Le but de tout ceci : après génération, l'épreuve a SA ligne de suivi
        et son pointage. C'est elle qu'on coche dans « Remplissage ».
        """
        from apps.suivi.models import Suivie, SuiviePointage

        self._epreuve(classes=[self.mp_a, self.mp_b])
        reponse = self._generer_le_suivi()
        self.assertIn(reponse.status_code, (200, 201), getattr(reponse, 'data', ''))

        suivies = Suivie.objects.filter(type_seance_fk__type_seance='EF')
        self.assertEqual(suivies.count(), 2, 'une ligne par classe convoquée')

        pointages = SuiviePointage.objects.filter(type_seance_fk__type_seance='EF')
        self.assertTrue(pointages.exists(), "l'épreuve doit être pointable")
        self.assertEqual(pointages.first().commentaire, 'Non fait')

    def test_la_ligne_de_suivi_porte_la_VRAIE_duree(self):
        """
        La génération lit `duree_creneau` sur le créneau : 1h30, toujours. Une
        épreuve de 4h y comptait donc 1h30 de surveillance, et l'écran
        annonçait « 08h00-09h30 » pour un examen qui court jusqu'à midi.
        """
        from apps.suivi.models import Suivie, SuiviePointage

        self._epreuve(minutes=240)                       # 08h00 → 12h00
        self._generer_le_suivi()

        suivie = Suivie.objects.get(type_seance_fk__type_seance='EF')
        self.assertEqual(suivie.duree_creneau, 4.0)

        pointage = SuiviePointage.objects.get(type_seance_fk__type_seance='EF')
        self.assertEqual(pointage.duree_creneau, 4.0)

    def test_les_seances_ordinaires_gardent_la_duree_de_leur_creneau(self):
        """La reprise ne doit toucher QUE les épreuves."""
        from apps.suivi.models import Suivie

        SeanceReelle.objects.create(
            classe=self.mp_a, semaine=self.semaine, jour=Jour.objects.first(),
            creneau=Creneau.objects.first(), matiere=self.matiere,
            type_seance=Seance.objects.get(type_seance='CM'))
        self._epreuve(minutes=240)
        self._generer_le_suivi()

        self.assertEqual(
            Suivie.objects.get(type_seance_fk__type_seance='CM').duree_creneau, 1.5)

    def test_l_horaire_reel_se_lit_sur_le_pointage(self):
        """Ce que l'écran de remplissage affiche dans la colonne « Créneau »."""
        from apps.suivi.models import SuiviePointage
        from apps.suivi.serializers import SuiviePointageSerializer

        self._epreuve(minutes=240)
        self._generer_le_suivi()

        pointage = SuiviePointage.objects.get(type_seance_fk__type_seance='EF')
        donnees = SuiviePointageSerializer(pointage).data
        self.assertEqual(donnees['horaire_reel'], '08h00 - 12h00')

    def test_une_seance_ordinaire_n_a_pas_d_horaire_reel(self):
        """
        Vide quand la durée colle au créneau : réécrire « 08h00 - 09h30 »
        partout alourdirait la colonne sans rien apprendre.
        """
        from apps.suivi.models import SuiviePointage
        from apps.suivi.serializers import SuiviePointageSerializer

        SeanceReelle.objects.create(
            classe=self.mp_a, semaine=self.semaine, jour=Jour.objects.first(),
            creneau=Creneau.objects.first(), matiere=self.matiere,
            type_seance=Seance.objects.get(type_seance='CM'))
        self._generer_le_suivi()

        pointage = SuiviePointage.objects.filter(
            type_seance_fk__type_seance='CM').first()
        self.assertEqual(SuiviePointageSerializer(pointage).data['horaire_reel'], '')

    def test_l_epreuve_figure_sur_les_fiches_de_suivi(self):
        """
        Les fiches — individuelle comme collective — exigeaient une SALLE. Une
        épreuve n'en a pas : le devoir disparaissait de la feuille où on le
        pointe, alors que c'est la seule qui circule le jour de l'examen.
        """
        from rest_framework.test import APIRequestFactory, force_authenticate

        from apps.suivi.views import SuiviePointageViewSet

        self._epreuve(minutes=240)
        self._generer_le_suivi()

        from django.contrib.auth import get_user_model
        admin = get_user_model().objects.get(username='gen')

        # L'ÉCRAN — l'action `fiches`.
        vue = SuiviePointageViewSet.as_view({'get': 'fiches'})
        demande = APIRequestFactory().get('/x/', {
            'annee_universitaire': '2025-2026', 'numero_semaine': 7,
            'type_semestre': 'I', 'id_semestre': 'S3'})
        force_authenticate(demande, user=admin)
        reponse = vue(demande)
        self.assertEqual(reponse.status_code, 200)
        types = [f['type_seance'] for jour in reponse.data
                 for f in jour.get('fiches', [])]
        self.assertIn('EF', types, "l'épreuve doit figurer à l'écran")

        # LES DEUX PDF — ils partagent `_build_fiches_par_jour`, qui portait la
        # même condition et devait donc être corrigé lui aussi.
        jours = SuiviePointageViewSet()._build_fiches_par_jour(
            '2025-2026', 7, 'I', 'S3')
        types_pdf = [f['type_seance'] for jour in jours
                     for f in jour.get('fiches', [])]
        self.assertIn('EF', types_pdf, "l'épreuve doit figurer sur les PDF")

    # ── La cohérence de la semaine ───────────────────────────────────────────
    def test_une_semaine_qui_porte_une_epreuve_reste_ALIGNEE(self):
        """
        Le défaut le plus sournois de toute cette affaire. L'épreuve produit
        une ligne de `Suivie` — c'est ce qui la rend pointable — mais aucune
        `SeanceReelle`. Elle figurait donc dans l'empreinte du SUIVI et
        manquait à celle de l'EMPLOI DU TEMPS : les deux ne pouvaient plus
        jamais s'égaler, et la semaine se déclarait éternellement
        « modifiée après la génération du suivi ».

        Rien n'avait bougé. Le message poussait à régénérer un suivi qui allait
        bien — exactement le faux positif que l'empreinte par CONTENU, et non
        par horodatage, avait été écrite pour éviter.
        """
        self._epreuve(minutes=240)
        self._generer_le_suivi()

        self.semaine.refresh_from_db()
        self.assertEqual(self.semaine.etat_coherence_pour(self.mp_a),
                         self.semaine.ETAT_ALIGNE)

    def test_les_deux_empreintes_designent_la_meme_case(self):
        """
        La projection et l'empreinte doivent ancrer l'épreuve sur le MÊME
        créneau. Elles partagent `ancrage_epreuve` pour cette raison ; ce test
        constate l'égalité plutôt que de la supposer.
        """
        self._epreuve(minutes=240)
        self._generer_le_suivi()

        self.semaine.refresh_from_db()
        self.assertEqual(self.semaine._empreinte_edt(self.mp_a),
                         self.semaine._empreinte_suivi(self.mp_a))

    def test_une_semaine_sans_suivi_reste_previsionnelle(self):
        """Déclarer une épreuve ne publie rien : tant que le suivi n'est pas
        généré, la semaine reste prévisionnelle."""
        self._epreuve(minutes=240)
        self.semaine.refresh_from_db()
        self.assertEqual(self.semaine.etat_coherence_pour(self.mp_a),
                         self.semaine.ETAT_PREVISIONNEL)

    def test_deplacer_l_epreuve_APRES_le_suivi_rend_la_semaine_divergente(self):
        """
        L'envers du test précédent, et la raison d'être du signal : changer le
        créneau d'une épreuve après la génération DOIT se voir. Sans quoi on
        aurait échangé un faux positif permanent contre un silence.
        """
        from datetime import time as heure

        epreuve = self._epreuve(minutes=240)             # 08h00, trois créneaux
        self._generer_le_suivi()

        epreuve.heure_debut = heure(15, 10)              # l'après-midi
        epreuve.save(update_fields=['heure_debut'])

        self.semaine.refresh_from_db()
        self.assertEqual(self.semaine.etat_coherence_pour(self.mp_a),
                         self.semaine.ETAT_DIVERGENT)

    def test_le_calcul_EN_LOT_dit_la_meme_chose_que_l_unitaire(self):
        """
        Deux implémentations du même verdict : l'unitaire sert le PDF, celle en
        lot sert les écrans de liste — et c'est elle que l'API des semaines
        appelle. Elles se sont déjà séparées : le PDF disait « aligné » quand
        la liste disait encore « modifié après la génération du suivi ».

        L'écart est invisible à l'usage — personne ne regarde les deux à la
        fois — et c'est ce qui le rend durable.
        """
        self._epreuve(minutes=240)
        self._generer_le_suivi()
        self.semaine.refresh_from_db()

        en_lot = SemaineIPGEI.etats_en_lot([self.semaine], self.mp_a)
        self.assertEqual(en_lot[self.semaine.pk],
                         self.semaine.etat_coherence_pour(self.mp_a))
        self.assertEqual(en_lot[self.semaine.pk], self.semaine.ETAT_ALIGNE)

    def test_les_deux_calculs_s_accordent_aussi_sur_la_divergence(self):
        """Le même accord dans l'autre sens : une épreuve déplacée après le
        suivi doit être vue par les deux."""
        from datetime import time as heure

        epreuve = self._epreuve(minutes=240)
        self._generer_le_suivi()
        epreuve.heure_debut = heure(15, 10)
        epreuve.save(update_fields=['heure_debut'])
        self.semaine.refresh_from_db()

        en_lot = SemaineIPGEI.etats_en_lot([self.semaine], self.mp_a)
        self.assertEqual(en_lot[self.semaine.pk],
                         self.semaine.etat_coherence_pour(self.mp_a))
        self.assertEqual(en_lot[self.semaine.pk], self.semaine.ETAT_DIVERGENT)

    def test_les_fiches_portent_l_horaire_reel_de_l_epreuve(self):
        """
        Le créneau seul annonçait « 08h00-09h30 » pour un examen qui court
        jusqu'à midi — sur la feuille que le surveillant a en main.

        L'écran et les deux PDF passent par des chemins différents ; le
        calcul, lui, vient d'un seul endroit (`core.horaire`). Les deux sont
        vérifiés, sans quoi la correction aurait pu ne valoir que pour l'un.
        """
        from rest_framework.test import APIRequestFactory, force_authenticate

        from django.contrib.auth import get_user_model
        from apps.suivi.views import SuiviePointageViewSet

        self._epreuve(minutes=240)                       # 08h00 → 12h00
        self._generer_le_suivi()
        admin = get_user_model().objects.get(username='gen')

        vue = SuiviePointageViewSet.as_view({'get': 'fiches'})
        demande = APIRequestFactory().get('/x/', {
            'annee_universitaire': '2025-2026', 'numero_semaine': 7,
            'type_semestre': 'I', 'id_semestre': 'S3'})
        force_authenticate(demande, user=admin)
        ecran = [f for jour in vue(demande).data for f in jour.get('fiches', [])]
        self.assertEqual([f['creneau'] for f in ecran], ['08h00 - 12h00'])

        pdf = [f for jour in SuiviePointageViewSet()._build_fiches_par_jour(
                   '2025-2026', 7, 'I', 'S3')
               for f in jour.get('fiches', [])]
        self.assertEqual([f['creneau'] for f in pdf], ['08h00 - 12h00'])

    def test_une_seance_ordinaire_garde_le_libelle_de_son_creneau(self):
        """
        L'horaire réel ne s'affiche que là où il apprend quelque chose : une
        séance d'1h30 sur un créneau d'1h30 garde son libellé d'origine.
        """
        from rest_framework.test import APIRequestFactory, force_authenticate

        from django.contrib.auth import get_user_model
        from apps.suivi.views import SuiviePointageViewSet

        SeanceReelle.objects.create(
            classe=self.mp_a, semaine=self.semaine, jour=Jour.objects.first(),
            creneau=Creneau.objects.first(), matiere=self.matiere,
            salle=None, type_seance=Seance.objects.get(type_seance='CM'))
        self._generer_le_suivi()

        from apps.suivi.models import SuiviePointage
        from apps.suivi.serializers import SuiviePointageSerializer

        cm = SuiviePointage.objects.get(type_seance_fk__type_seance='CM')
        self.assertEqual(SuiviePointageSerializer(cm).data['horaire_reel'], '')

    def test_une_SEANCE_sans_salle_reste_ecartee_des_fiches(self):
        """
        L'exception est nommément limitée aux épreuves. Une séance de cours
        sans salle est une ligne d'emploi du temps incomplète : elle n'a rien
        à faire sur une feuille de présence, et la règle d'origine tient.
        """
        from rest_framework.test import APIRequestFactory, force_authenticate

        from django.contrib.auth import get_user_model
        from apps.suivi.views import SuiviePointageViewSet

        # Un CM sans salle, et une épreuve : seule la seconde doit passer.
        SeanceReelle.objects.create(
            classe=self.mp_a, semaine=self.semaine, jour=Jour.objects.first(),
            creneau=Creneau.objects.first(), matiere=self.matiere,
            type_seance=Seance.objects.get(type_seance='CM'))
        self._epreuve(minutes=240)
        self._generer_le_suivi()

        admin = get_user_model().objects.get(username='gen')
        vue = SuiviePointageViewSet.as_view({'get': 'fiches'})
        demande = APIRequestFactory().get('/x/', {
            'annee_universitaire': '2025-2026', 'numero_semaine': 7,
            'type_semestre': 'I', 'id_semestre': 'S3'})
        force_authenticate(demande, user=admin)
        types = [f['type_seance'] for jour in vue(demande).data
                 for f in jour.get('fiches', [])]

        self.assertIn('EF', types, "l'épreuve passe sans salle")
        self.assertNotIn('CM', types, 'une séance sans salle reste écartée')

    def test_les_lignes_vides_restent_ecartees_des_fiches(self):
        """
        Ce que la condition visait vraiment : la génération pose une ligne sur
        chaque créneau libre, sans EM. Les laisser entrer noierait la feuille
        sous des lignes vides.
        """
        from rest_framework.test import APIRequestFactory, force_authenticate

        from apps.suivi.views import SuiviePointageViewSet

        self._epreuve(minutes=240)
        self._generer_le_suivi()

        from django.contrib.auth import get_user_model
        admin = get_user_model().objects.get(username='gen')

        vue = SuiviePointageViewSet.as_view({'get': 'fiches'})
        demande = APIRequestFactory().get('/x/', {
            'annee_universitaire': '2025-2026', 'numero_semaine': 7,
            'type_semestre': 'I', 'id_semestre': 'S3'})
        force_authenticate(demande, user=admin)
        fiches = [f for jour in vue(demande).data for f in jour.get('fiches', [])]

        self.assertTrue(fiches, "au moins la ligne de l'épreuve")
        self.assertTrue(all(f['em_intitule'] for f in fiches),
                        'aucune ligne sans module ne doit passer')

    def test_la_ligne_de_suivi_satisfait_le_filtre_du_remplissage(self):
        """
        L'écran écarte toute ligne sans type, sans intitulé de module, ou de
        type spécial. Une épreuve doit passer les trois.
        """
        from apps.suivi.models import SuiviePointage

        self._epreuve()
        self._generer_le_suivi()

        pointage = SuiviePointage.objects.filter(
            type_seance_fk__type_seance='EF').first()
        self.assertIsNotNone(pointage)
        self.assertTrue(pointage.type_seance_fk.type_seance)
        self.assertTrue(pointage.em and pointage.em.intitule)
        self.assertFalse(pointage.type_seance_fk.is_special)
        # Ni enseignant ni salle : l'écran affiche « — », ce qui est exact.
        self.assertIsNone(pointage.prof_id)
        self.assertIsNone(pointage.salle_id)
