"""
Test — « Non fait » réel ou « En attente » de pointage (apps/suivi/statut_pointage.py).

Le pointage d'une semaine naît à « Non fait » ; seule l'heure du pointage de la
grille (même semaine, département commun) dit si un « Non fait » a été constaté.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date, datetime, timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.departement.models import Departement
from apps.parametres.models import Creneau, Institution, Jour
from apps.prof.models import Prof, ProfTypeHistory
from apps.suivi.models import SuiviePointage, SuiviePointageDepartement
from apps.suivi.statut_pointage import EN_ATTENTE, NON_FAIT, fin_seance, statut_affiche

User = get_user_model()
ANNEE = '2025-2026'
LUNDI = date(2025, 10, 6)


def a(jour, h, m=0):
    return timezone.make_aware(datetime(jour.year, jour.month, jour.day, h, m))


class StatutPointageTest(APITestCase):
    @classmethod
    def setUpClass(cls):
        existantes = set(connection.introspection.table_names())
        for model in (ProfTypeHistory, SuiviePointageDepartement):
            if model._meta.db_table not in existantes:
                with connection.schema_editor() as se:
                    se.create_model(model)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test', est_principale=True)
        cls.dept_a = Departement.objects.create(nom='Dept A', institution=cls.inst)
        cls.dept_b = Departement.objects.create(nom='Dept B', institution=cls.inst)
        cls.matin = Creneau.objects.create(creneau='08:00-09:30', ordre=1)
        cls.aprem = Creneau.objects.create(creneau='15:00-16:30', ordre=2)
        cls.lundi = Jour.objects.create(jour='Lundi')
        cls.mardi = Jour.objects.create(jour='Mardi')
        cls.ens = User.objects.create_user(
            username='ens_pointage', email='ens@test.local', password='Xk93!plqz72', role='enseignant',
        )
        cls.prof = Prof.objects.create(NNI=333333, nom='Prof Pointage', type='vacataire', user=cls.ens)

    def _seance(self, jour_fk, jour, creneau, dept, commentaire='Non fait', pointe_le=None):
        sp = SuiviePointage.objects.create(
            prof=self.prof, institution=self.inst, annee_universitaire=ANNEE, numero_semaine=1,
            type_semestre='I', creneau_fk=creneau, jour_fk=jour_fk, date_suivie=jour,
            commentaire=commentaire, pointe_le=pointe_le,
        )
        sp.departements.add(dept)
        return sp

    def _statuts(self):
        self.client.force_authenticate(user=self.ens)
        r = self.client.get('/api/v1/suivi/pointages/grille/', {
            'annee_universitaire': ANNEE, 'prof': str(self.prof.pk), 'type_semestre': 'I', 'numero_semaine': '1',
        })
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        return {s['id']: s['statut'] for j in r.data['grille'].values() for c in j.values() for s in c}

    # ── Règle ────────────────────────────────────────────────────────────────
    def test_fin_de_seance_lue_dans_le_creneau(self):
        sp = self._seance(self.lundi, LUNDI, self.matin, self.dept_a)
        self.assertEqual(fin_seance(sp), a(LUNDI, 9, 30))

    def test_jamais_pointee_reste_en_attente(self):
        sp = self._seance(self.lundi, LUNDI, self.matin, self.dept_a)
        self.assertEqual(self._statuts()[sp.pk], EN_ATTENTE)

    def test_grille_pointee_apres_la_seance_confirme_le_non_fait(self):
        """Le surveillant pointe lundi 18h : il passe la séance de 15h en « Fait »
        et laisse celle de 8h en « Non fait » → c'est un vrai « Non fait »."""
        non_faite = self._seance(self.lundi, LUNDI, self.matin, self.dept_a)
        self._seance(self.lundi, LUNDI, self.aprem, self.dept_a, 'Fait', pointe_le=a(LUNDI, 18))
        self.assertEqual(self._statuts()[non_faite.pk], NON_FAIT)

    def test_seance_posterieure_au_pointage_reste_en_attente(self):
        self._seance(self.lundi, LUNDI, self.aprem, self.dept_a, 'Fait', pointe_le=a(LUNDI, 18))
        mardi = self._seance(self.mardi, LUNDI + timedelta(days=1), self.matin, self.dept_a)
        self.assertEqual(self._statuts()[mardi.pk], EN_ATTENTE)

    def test_pointage_pendant_la_seance_ne_compte_pas(self):
        """Pointage à 9h00, séance jusqu'à 9h30 : rien n'est constaté."""
        sp = self._seance(self.lundi, LUNDI, self.matin, self.dept_a)
        self._seance(self.mardi, LUNDI + timedelta(days=1), self.aprem, self.dept_a, 'Fait', pointe_le=a(LUNDI, 9))
        self.assertEqual(self._statuts()[sp.pk], EN_ATTENTE)

    def test_autre_departement_ne_compte_pas(self):
        sp = self._seance(self.lundi, LUNDI, self.matin, self.dept_b)
        self._seance(self.lundi, LUNDI, self.aprem, self.dept_a, 'Fait', pointe_le=a(LUNDI, 18))
        self.assertEqual(self._statuts()[sp.pk], EN_ATTENTE)

    def test_fait_et_reporte_inchanges(self):
        fait = self._seance(self.lundi, LUNDI, self.matin, self.dept_a, 'Fait')
        reporte = self._seance(self.lundi, LUNDI, self.aprem, self.dept_a, 'Reporté')
        st = self._statuts()
        self.assertEqual((st[fait.pk], st[reporte.pk]), ('Fait', 'Reporté'))

    def test_seance_sans_date_garde_l_ancien_comportement(self):
        sp = SuiviePointage(commentaire='Non fait', date_suivie=None)
        self.assertEqual(statut_affiche(sp, None), NON_FAIT)

    # ── Écritures : l'heure du pointage est retenue ──────────────────────────
    def test_bascule_et_mise_a_jour_en_masse_retiennent_l_heure(self):
        admin = User.objects.create_superuser(username='admin_pt', email='a@test.local', password='Xk93!plqz72')
        self.client.force_authenticate(user=admin)
        sp1 = self._seance(self.lundi, LUNDI, self.matin, self.dept_a)
        sp2 = self._seance(self.lundi, LUNDI, self.aprem, self.dept_a)

        r = self.client.patch(f'/api/v1/suivi/pointages/{sp1.pk}/toggle/')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        sp1.refresh_from_db()
        self.assertIsNotNone(sp1.pointe_le)

        # Ligne envoyée inchangée (« Non fait ») : elle est quand même pointée.
        r = self.client.post('/api/v1/suivi/pointages/bulk-update/',
                             {'updates': [{'id': sp2.pk, 'commentaire': 'Non fait'}]}, format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        sp2.refresh_from_db()
        self.assertIsNotNone(sp2.pointe_le)
        self.assertEqual(statut_affiche(sp2, sp2.pointe_le), NON_FAIT)
