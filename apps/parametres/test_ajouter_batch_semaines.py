"""
Test bout-en-bout de l'action SemaineViewSet.ajouter_batch
(POST /api/v1/parametres/semaines/ajouter-batch/).

Vérifie le scénario du ticket : date_debut=2025-10-06, nombre_semaines=16,
type_semestre='I' → 16 semaines × 6 jours = 96 lignes Semaine.

Exécution :  pytest apps/parametres/test_ajouter_batch_semaines.py
Base SQLite en mémoire (siga.settings.test) — aucun contact avec gesafped26.
"""
from datetime import date

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.parametres.models import Jour, Semaine

User = get_user_model()

URL = '/api/v1/parametres/semaines/ajouter-batch/'
JOURS = ['Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi']


class AjouterBatchSemainesTest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        # Table de référence : 6 jours ouvrés, dans l'ordre (Lundi → Samedi).
        for nom in JOURS:
            Jour.objects.create(jour=nom)
        cls.admin = User.objects.create_user(
            username='admin_batch', email='admin_batch@test.local',
            password='Xk93!plqz72', role='admin',
        )

    def setUp(self):
        self.client.force_authenticate(user=self.admin)

    def test_genere_16_semaines_x_6_jours(self):
        """16 semaines × 6 jours = 96 lignes, numérotées 1→16, recalées au lundi."""
        resp = self.client.post(URL, {
            'annee_universitaire': '2025-2026',
            'type_semestre':       'I',
            'date_debut':          '2025-10-06',
            'nombre_semaines':     16,
        }, format='json')

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['created'], 96)
        self.assertEqual(resp.data['weeks'], 16)
        self.assertEqual(resp.data['numero_debut'], 1)
        self.assertEqual(resp.data['numero_fin'], 16)

        # Persistance réelle
        self.assertEqual(Semaine.objects.count(), 96)
        self.assertEqual(
            Semaine.objects.values('numero_semaine').distinct().count(), 16,
        )

        # date_debut recalée au lundi (weekday 0)
        premiere = Semaine.objects.order_by('date').first().date
        self.assertEqual(premiere.weekday(), 0, "La 1re date n'est pas un lundi.")
        self.assertEqual(premiere, date(2025, 10, 6))

        # 6 lignes (1 par jour) pour la semaine n°1
        self.assertEqual(
            Semaine.objects.filter(numero_semaine=1).count(), 6,
        )

    def test_second_batch_appende_a_la_suite(self):
        """Un 2ᵉ batch repart après les semaines existantes (Art. offset auto)."""
        payload = {
            'annee_universitaire': '2025-2026',
            'type_semestre':       'I',
            'date_debut':          '2025-10-06',
            'nombre_semaines':     4,
        }
        r1 = self.client.post(URL, payload, format='json')
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED, r1.data)
        self.assertEqual(r1.data['numero_debut'], 1)
        self.assertEqual(r1.data['numero_fin'], 4)

        r2 = self.client.post(URL, payload, format='json')
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED, r2.data)
        # Le 2ᵉ batch ne réécrit pas : il continue à 5→8.
        self.assertEqual(r2.data['numero_debut'], 5)
        self.assertEqual(r2.data['numero_fin'], 8)
        self.assertEqual(Semaine.objects.count(), 48)  # (4 + 4) × 6

    def test_refuse_non_admin(self):
        """Sécurité : un non-admin ne peut pas générer (IsAdmin)."""
        etu = User.objects.create_user(
            username='etu_batch', email='etu_batch@test.local',
            password='Xk93!plqz72', role='etudiant',
        )
        self.client.force_authenticate(user=etu)
        resp = self.client.post(URL, {
            'annee_universitaire': '2025-2026', 'type_semestre': 'I',
            'date_debut': '2025-10-06', 'nombre_semaines': 16,
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Semaine.objects.count(), 0)
