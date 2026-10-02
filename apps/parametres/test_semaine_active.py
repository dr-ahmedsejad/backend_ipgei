"""
Test — semaine active proposée à la connexion
(`GET /api/v1/parametres/semaines/actif/`, public).

La page de connexion pré-coche la période avec cette réponse, AVANT toute
authentification. Hors période, l'ancienne règle prenait toujours la dernière
semaine PASSÉE : entre deux années, elle proposait le semestre révolu depuis
trois mois plutôt que celui qui commençait quelques jours plus tard.

Ce que le test fige :
  1. la ligne du JOUR MÊME l'emporte, même si une voisine est plus proche ;
  2. hors période, la plus proche gagne — Y COMPRIS quand elle est à venir ;
  3. l'inverse tient : juste après une fin de semestre, c'est lui qu'on rouvre ;
  4. à égale distance, c'est celle qui VIENT ;
  5. avec le passé seul on prend le passé, avec l'avenir seul l'avenir ;
  6. calendrier vide : le défaut d'avant, inchangé ;
  7. l'adresse répond sans authentification.

Les dates sont posées EN RELATIF : figées, ces tests passeraient le jour de
leur écriture puis tomberaient d'eux-mêmes quelques semaines plus tard.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date, timedelta

from rest_framework.test import APITestCase

from apps.parametres.models import Jour, Semaine

URL = '/api/v1/parametres/semaines/actif/'


class SemaineActiveTest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.jour = Jour.objects.create(jour='Lundi')

    def _semaine(self, jours: int, *, type_semestre='I', annee='2026-2027'):
        """Une ligne de calendrier à `jours` du jour même (négatif = passé)."""
        return Semaine.objects.create(
            numero_semaine=1, jour_fk=self.jour, date=date.today() + timedelta(days=jours),
            annee_universitaire=annee, type_semestre=type_semestre)

    def _reponse(self):
        r = self.client.get(URL)
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    # ── Le jour même ────────────────────────────────────────────────────────
    def test_le_jour_meme_l_emporte(self):
        self._semaine(-1, type_semestre='P', annee='2025-2026')
        self._semaine(0, type_semestre='I', annee='2026-2027')
        self._semaine(1, type_semestre='P', annee='2025-2026')
        d = self._reponse()
        self.assertEqual((d['type_semestre'], d['annee_universitaire']), ('I', '2026-2027'))
        self.assertEqual(d['source'], 'exact')
        self.assertEqual(d['date_reference'], str(date.today()))

    # ── Hors période : la plus proche, dans les deux sens ───────────────────
    def test_hors_periode_l_avenir_proche_bat_le_passe_lointain(self):
        """Le cas de production : 104 jours en arrière contre 3 en avant."""
        self._semaine(-104, type_semestre='P', annee='2025-2026')
        self._semaine(3, type_semestre='I', annee='2026-2027')
        d = self._reponse()
        self.assertEqual((d['type_semestre'], d['annee_universitaire']), ('I', '2026-2027'))
        self.assertEqual(d['source'], 'fallback')

    def test_hors_periode_le_passe_proche_bat_l_avenir_lointain(self):
        """Deux jours après la fin d'un semestre, c'est lui qu'on rouvre."""
        self._semaine(-2, type_semestre='P', annee='2026-2027')
        self._semaine(90, type_semestre='I', annee='2027-2028')
        d = self._reponse()
        self.assertEqual((d['type_semestre'], d['annee_universitaire']), ('P', '2026-2027'))
        self.assertEqual(d['source'], 'fallback')

    def test_a_egale_distance_c_est_celle_qui_vient(self):
        self._semaine(-7, type_semestre='P', annee='2025-2026')
        self._semaine(7, type_semestre='I', annee='2026-2027')
        d = self._reponse()
        self.assertEqual((d['type_semestre'], d['annee_universitaire']), ('I', '2026-2027'))

    # ── Un seul côté ────────────────────────────────────────────────────────
    def test_avec_le_passe_seul_on_prend_le_passe(self):
        self._semaine(-30, type_semestre='P', annee='2025-2026')
        d = self._reponse()
        self.assertEqual((d['type_semestre'], d['annee_universitaire']), ('P', '2025-2026'))
        self.assertEqual(d['source'], 'fallback')

    def test_avec_l_avenir_seul_on_prend_l_avenir(self):
        self._semaine(30, type_semestre='I', annee='2026-2027')
        d = self._reponse()
        self.assertEqual((d['type_semestre'], d['annee_universitaire']), ('I', '2026-2027'))
        self.assertEqual(d['source'], 'fallback')

    # ── Calendrier vide ─────────────────────────────────────────────────────
    def test_calendrier_vide_rend_le_defaut(self):
        d = self._reponse()
        self.assertEqual(d, {'type_semestre': 'I', 'annee_universitaire': '', 'source': 'default'})

    # ── L'adresse elle-même ─────────────────────────────────────────────────
    def test_repond_sans_authentification(self):
        """La page de connexion l'appelle avant d'avoir un compte."""
        self._semaine(0)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(URL).status_code, 200)

    def test_la_reponse_garde_sa_forme(self):
        """Le front lit `type_semestre` ; `source` distingue certitude et proposition."""
        self._semaine(0, type_semestre='P', annee='2026-2027')
        self.assertEqual(
            set(self._reponse()),
            {'type_semestre', 'annee_universitaire', 'numero_semaine', 'date_reference', 'source'})
