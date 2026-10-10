"""
Cache des consultations étudiantes (apps/portail/cache_portail.py), sur les
notes IPGEI (moteur IPGEI, visibles après le jury).

Ce qui compte : après une écriture, l'étudiant voit la NOUVELLE valeur.
  * enregistrement ordinaire (save) → signal → copie périmée sur-le-champ ;
  * écriture en masse (.update(), bulk) → invisible aux signaux, mais les
    compteurs d'écriture de PostgreSQL bougent : simulés ici (la base de test
    est SQLite, qui n'en a pas) ;
  * sans PostgreSQL ou cache coupé (0) : calcul direct, comme avant.
"""
from datetime import date
from decimal import Decimal

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.response import Response
from rest_framework.test import APIClient

from apps.absence.models import Etudiant
from apps.authentication.models import CustomUser
from apps.ipgei.models import (Classe, Deliberation, InscriptionIPGEI, LigneDeliberation,
                               Matiere, NiveauIPGEI, Note, SemestreIPGEI)
from apps.parametres.models import Institution, Niveau
from apps.portail import cache_portail
from apps.prof.models import ProfTypeHistory

ANNEE = '2025-2026'


class CompteursPG:
    """Compteurs d'écriture PostgreSQL simulés."""
    valeur = 1000

    def __call__(self):
        return self.valeur


@override_settings(PORTAIL_CACHE_SECONDES=600)
class CachePortailIPGEITest(TestCase):

    @classmethod
    def setUpClass(cls):
        if ProfTypeHistory._meta.db_table not in connection.introspection.table_names():
            with connection.schema_editor() as se:
                se.create_model(ProfTypeHistory)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        inst = Institution.objects.create(acronyme='TST', nom='Institut Test', est_principale=True)
        ref = NiveauIPGEI.objects.create(institution=inst, code='MP', libelle='MP', rang=2)
        s3 = SemestreIPGEI.objects.create(institution=inst, code='S3', annee_universitaire=ANNEE,
                                          date_debut=date(2025, 10, 6))
        math = Matiere.objects.create(institution=inst, code='MATH', intitule='Mathématiques',
                                      code_semestre='S3', niveau_ref=ref, coefficient=Decimal('4'))
        classe = Classe.objects.create(nom='MP A', institution=inst, annee_universitaire=ANNEE,
                                       niveau=Niveau.objects.get_or_create(niveau='MP')[0], groupe='A')
        etudiant = Etudiant.objects.create(matricule='242001', nom='Cobaye', nom_fr='Cobaye')
        cls.user = CustomUser.objects.create(username='242001', role='etudiant', email='c@t.l')
        etudiant.user = cls.user
        etudiant.save(update_fields=['user'])
        inscription = InscriptionIPGEI.objects.create(etudiant=etudiant, classe=classe,
                                                      annee_universitaire=ANNEE)
        cls.note = Note.objects.create(
            inscription=inscription, matiere=math, semestre=s3,
            note_retenue=Decimal('8.00'), moyenne=Decimal('8.00'),
            pct_ds=Decimal('30'), pct_tp=Decimal('0'), pct_exam=Decimal('70'),
            coefficient=Decimal('4'))
        jury = Deliberation.objects.create(
            institution=inst, niveau='MP', classe=classe, annee_universitaire=ANNEE,
            portee=Deliberation.PORTEE_ANNUELLE, statut=Deliberation.STATUT_VALIDEE,
            seuil_validation=Decimal('10'))
        LigneDeliberation.objects.create(deliberation=jury, inscription=inscription,
                                         moyenne_generale=Decimal('8.00'))

    def setUp(self):
        cache.clear()
        self.pg = CompteursPG()
        self._orig = cache_portail._compteurs_postgres
        cache_portail._compteurs_postgres = self.pg
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def tearDown(self):
        cache_portail._compteurs_postgres = self._orig
        cache.clear()

    def _notes(self):
        with CaptureQueriesContext(connection) as q:
            r = self.client.get('/api/v1/portail/notes/')
        self.assertEqual(r.status_code, 200, r.data)
        return [n['note_finale'] for n in r.data], len(q)

    def test_deuxieme_consultation_servie_par_la_copie(self):
        notes1, calcul = self._notes()
        notes2, copie = self._notes()
        self.assertEqual(notes1, [8.0])
        self.assertEqual(notes2, [8.0])
        self.assertLess(copie, calcul)

    def test_enregistrement_ordinaire_visible_tout_de_suite(self):
        self.assertEqual(self._notes()[0], [8.0])
        self.note.note_retenue = Decimal('14.00')
        self.note.save()                       # signal → empreinte changée
        self.assertEqual(self._notes()[0], [14.0])

    def test_ecriture_en_masse_visible_via_les_compteurs_postgres(self):
        self.assertEqual(self._notes()[0], [8.0])
        Note.objects.filter(pk=self.note.pk).update(note_retenue=Decimal('11.00'))  # aucun signal
        self.pg.valeur += 1                    # PostgreSQL compte la ligne modifiée
        self.assertEqual(self._notes()[0], [11.0])

    def test_une_notification_n_invalide_pas_les_copies(self):
        from apps.notifications.models import Notification
        self._notes()
        avant = cache_portail.empreinte()
        Notification.objects.create(destinataire=self.user, titre='x', message='y')
        self.assertEqual(cache_portail.empreinte(), avant)

    @override_settings(PORTAIL_CACHE_SECONDES=0)
    def test_cache_coupe(self):
        _, a = self._notes()
        _, b = self._notes()
        self.assertEqual(a, b)                 # recalculé à chaque fois

    def test_sans_postgresql_pas_de_cache(self):
        cache_portail._compteurs_postgres = self._orig
        self.assertIsNone(cache_portail.empreinte())   # SQLite
        _, a = self._notes()
        _, b = self._notes()
        self.assertEqual(a, b)

    def test_une_erreur_n_est_jamais_gardee(self):
        appels = []

        def calcul():
            appels.append(1)
            return Response({'detail': 'x'}, status=500)

        cache_portail.reponse_en_cache('essai', [1], calcul)
        cache_portail.reponse_en_cache('essai', [1], calcul)
        self.assertEqual(len(appels), 2)

    def test_emploi_du_temps_servi_par_la_copie(self):
        r1 = self.client.get('/api/v1/portail/emploi-du-temps/')
        r2 = self.client.get('/api/v1/portail/emploi-du-temps/')
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(r1.data, r2.data)

    def test_tables_exclues(self):
        self.assertTrue(cache_portail.table_exclue('token_blacklist_outstandingtoken'))
        self.assertTrue(cache_portail.table_exclue('authentication_customuser'))
        self.assertFalse(cache_portail.table_exclue('ipgei_note'))
        self.assertFalse(cache_portail.table_exclue('suivi_suivie'))
