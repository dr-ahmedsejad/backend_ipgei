"""Emploi du temps du portail (IPGEI) : les séances du sous-groupe de TP.

La copie de l'emploi du temps range les TP sous l'identifiant du sous-groupe ;
l'étudiant doit voir ceux de SON sous-groupe, pas ceux des autres.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.absence.models import Etudiant
from apps.authentication.models import CustomUser
from apps.em.models import EM
from apps.ipgei.models import Classe, InscriptionIPGEI, SousGroupeTP
from apps.parametres.models import Creneau, Institution, Jour, Niveau, Seance, Semestre
from apps.suivi.models import Suivie

ANNEE = '2025-2026'


class EmploiSousGroupeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        inst = Institution.objects.create(acronyme='TST', nom='Institut Test', est_principale=True)
        niveau = Niveau.objects.create(niveau='MPSI')
        semestre = Semestre.objects.create(code_semestre='S1', semestre='S1', niveau_semestre=niveau, type_semestre='I')
        cls.classe = Classe.objects.create(nom='MPSI A', institution=inst, annee_universitaire=ANNEE, niveau=niveau, groupe='A')
        cls.g1 = SousGroupeTP.objects.create(nom='MPSI A — G1', institution=inst, annee_universitaire=ANNEE,
                                             niveau=niveau, groupe='G1', classe=cls.classe)
        cls.g2 = SousGroupeTP.objects.create(nom='MPSI A — G2', institution=inst, annee_universitaire=ANNEE,
                                             niveau=niveau, groupe='G2', classe=cls.classe)
        user = CustomUser.objects.create_user(username='etu_tp', password='x', role='etudiant')
        etudiant = Etudiant.objects.create(matricule='272001', nom='Test', user=user, departement=cls.classe)
        InscriptionIPGEI.objects.create(etudiant=etudiant, classe=cls.classe, sous_groupe=cls.g1, annee_universitaire=ANNEE)
        em = EM.objects.create(code_em='INFO', intitule='Informatique', CM=2, TD=0, TP=2, PR=0,
                               departement=cls.classe, semestre=semestre)
        jour = Jour.objects.create(jour='Lundi')
        cm, tp = Seance.objects.create(type_seance='CM'), Seance.objects.create(type_seance='TP')
        c1 = Creneau.objects.create(creneau='08:00-10:00', ordre=1)
        c2 = Creneau.objects.create(creneau='10:00-12:00', ordre=2)
        for dept, creneau, ts in ((cls.classe, c1, cm), (cls.g1, c2, tp), (cls.g2, c2, tp)):
            Suivie.objects.create(institution=inst, annee_universitaire=ANNEE, numero_semaine=1, departement=dept, em=em,
                                  creneau_fk=creneau, jour_fk=jour, type_seance_fk=ts)
        cls.user = user

    def test_cm_de_la_classe_et_tp_de_son_sous_groupe(self):
        c = APIClient()
        c.force_authenticate(self.user)
        r = c.get('/api/v1/portail/emploi-du-temps/', {'semaine': 1})
        self.assertEqual(r.status_code, 200, r.data)
        seances = [s for creneaux in r.data['grille'].values() for liste in creneaux.values() for s in liste]
        self.assertEqual(sorted(s['type_seance'] for s in seances), ['CM', 'TP'])
