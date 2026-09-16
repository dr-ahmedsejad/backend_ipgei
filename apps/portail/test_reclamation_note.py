"""
Test — un étudiant peut enfin désigner la note qu'il conteste.

`Reclamation.inscription_element` visait la couche LMD (`InscriptionElement`,
`SessionEvaluation`), dont les trois tables de la chaîne portent **zéro ligne**.
Trois conséquences, toutes silencieuses :

  * aucun étudiant ne pouvait rattacher sa réclamation à une note ;
  * `_periode_reclamation_active` remontait `InscriptionPedagogique` puis
    `InscriptionAdministrative` — elle rendait donc `False` pour tout le monde ;
  * elle exigeait de surcroît une `Year.est_active`, qu'AUCUNE année ne porte
    en base : même branchée, la garde aurait refusé toute réclamation ;
  * le périmètre de l'enseignant filtrait sur `inscription_element__em`, si
    bien qu'il ne voyait jamais rien.

Ce que le test fige :

  1. la réclamation pointe une note IPGEI, et le sérialiseur en tire la matière,
     le semestre et la classe ;
  2. une période ouverte rend la réclamation possible ; aucune, impossible ;
  3. la période est reconnue sur les cinq critères qu'elle déclare — année,
     parité, institution, filière, niveau — et « vide » y vaut « toutes » ;
  4. la parité se lit sur le CHIFFRE du semestre : S1/S3 impairs, S2/S4 pairs ;
  5. le niveau de la période se compare au RANG IPGEI, 1 pour MPSI, 2 pour MP.

Base SQLite en mémoire (siga.settings.test).
"""
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone
from django.test import TestCase

from apps.absence.models import Etudiant
from apps.ipgei.models import (
    Classe, InscriptionIPGEI, Matiere, NiveauIPGEI, Note, SemestreIPGEI,
)
from apps.parametres.models import Institution, Niveau, Year
from apps.portail.ipgei_source import (
    _parite, periodes_ouvertes, peut_reclamer,
)
from apps.reclamations.models import PeriodeReclamation, Reclamation
from apps.reclamations.serializers import ReclamationSerializer
from apps.scolarite.models import Filiere


class ReclamationSurNoteIPGEITest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MP')
        cls.ref = NiveauIPGEI.objects.create(institution=cls.inst, code='MP',
                                             libelle='MP', rang=2)
        cls.filiere = Filiere.objects.create(code='IPGEI', intitule_fr='IPGEI')
        # `Year` existe et n'est PAS active : c'est l'état réel de la base, et
        # l'ancienne garde y échouait pour tout le monde.
        cls.annee = Year.objects.create(annee='2025-2026', est_active=False)

        cls.s4 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S4', annee_universitaire='2025-2026',
            date_debut=date(2026, 1, 26))
        cls.s3 = SemestreIPGEI.objects.create(
            institution=cls.inst, code='S3', annee_universitaire='2025-2026',
            date_debut=date(2025, 10, 6))
        cls.math = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Mathematiques',
            code_semestre='S4', niveau_ref=cls.ref, coefficient=Decimal('4'))
        cls.classe = Classe.objects.create(
            nom='MP A', institution=cls.inst, annee_universitaire='2025-2026',
            niveau=cls.socle, groupe='A')

        cls.etudiant = Etudiant.objects.create(matricule='242011', nom='Etudiant')
        cls.inscription = InscriptionIPGEI.objects.create(
            etudiant=cls.etudiant, classe=cls.classe,
            annee_universitaire='2025-2026')
        cls.note = Note.objects.create(inscription=cls.inscription,
                                       matiere=cls.math, semestre=cls.s4)

    def _periode(self, **surcharges):
        maintenant = timezone.now()
        valeurs = dict(
            annee_univ=self.annee, type_session='rattrapage', type_semestre='P',
            institution=self.inst, filiere=None, niveau=None,
            date_ouverture=maintenant - timedelta(days=1),
            date_fermeture=maintenant + timedelta(days=1),
            actif=True, motif='Après le jury de rattrapage.')
        valeurs.update(surcharges)
        return PeriodeReclamation.objects.create(**valeurs)

    # ── 1. La réclamation désigne une note ──────────────────────────────────
    def test_la_reclamation_pointe_une_note_ipgei(self):
        reclamation = Reclamation.objects.create(
            etudiant=self.etudiant, type_reclamation='note',
            note_ipgei=self.note, motif='Erreur de report sur le DS 2.')
        donnees = ReclamationSerializer(reclamation).data
        self.assertEqual(donnees['em_code'], 'MATH')
        self.assertEqual(donnees['em_intitule'], 'Mathematiques')
        self.assertEqual(donnees['semestre_code'], 'S4')
        self.assertEqual(donnees['classe_nom'], 'MP A')

    def test_une_reclamation_sans_note_reste_possible(self):
        """Le type « autre » ne vise aucune note : rien ne doit l'empêcher."""
        reclamation = Reclamation.objects.create(
            etudiant=self.etudiant, type_reclamation='autre',
            motif='Demande de duplicata.')
        donnees = ReclamationSerializer(reclamation).data
        self.assertIsNone(donnees['em_code'])
        self.assertIsNone(donnees['semestre_code'])

    # ── 2. La période fait foi ──────────────────────────────────────────────
    def test_sans_periode_ouverte_on_ne_reclame_pas(self):
        self.assertFalse(peut_reclamer(self.note))
        self.assertEqual(periodes_ouvertes(self.etudiant), [])

    def test_une_periode_ouverte_autorise_la_reclamation(self):
        self._periode()
        self.assertTrue(peut_reclamer(self.note),
                        "l'année n'est pas 'active' en base, et c'est sans objet : "
                        'la période porte déjà son année')
        self.assertEqual(len(periodes_ouvertes(self.etudiant)), 1)

    def test_une_periode_fermee_ou_inactive_ne_compte_pas(self):
        passee = timezone.now() - timedelta(days=10)
        self._periode(date_ouverture=passee, date_fermeture=passee + timedelta(days=1))
        self.assertFalse(peut_reclamer(self.note))

        PeriodeReclamation.objects.all().delete()
        self._periode(actif=False)
        self.assertFalse(peut_reclamer(self.note))

    # ── 3-4. Les cinq critères, et la parité ────────────────────────────────
    def test_la_parite_se_lit_sur_le_chiffre_du_semestre(self):
        self.assertEqual(_parite('S1'), 'I')
        self.assertEqual(_parite('S2'), 'P')
        self.assertEqual(_parite('S3'), 'I')
        self.assertEqual(_parite('S4'), 'P')
        self.assertEqual(_parite('bruit'), '')

    def test_une_periode_impaire_ne_couvre_pas_une_note_de_s4(self):
        self._periode(type_semestre='I')
        self.assertFalse(peut_reclamer(self.note))

        note_s3 = Note.objects.create(inscription=self.inscription,
                                      matiere=self.math, semestre=self.s3)
        self.assertTrue(peut_reclamer(note_s3),
                        'la même période couvre bien le S3')

    def test_une_autre_annee_ne_couvre_pas(self):
        autre = Year.objects.create(annee='2024-2025')
        self._periode(annee_univ=autre)
        self.assertFalse(peut_reclamer(self.note))

    def test_une_autre_institution_ne_couvre_pas(self):
        ailleurs = Institution.objects.create(acronyme='XXX', nom='Ailleurs')
        self._periode(institution=ailleurs)
        self.assertFalse(peut_reclamer(self.note))

    def test_une_filiere_vide_vaut_toutes_les_filieres(self):
        """Le cas courant à l'IPGEI, qui n'a qu'une filière technique."""
        self._periode(filiere=None)
        self.assertTrue(peut_reclamer(self.note))

    # ── 5. Le niveau se compare au rang IPGEI ───────────────────────────────
    def test_le_niveau_de_la_periode_est_le_rang_ipgei(self):
        self._periode(niveau=2)                     # MP
        self.assertTrue(peut_reclamer(self.note))

        PeriodeReclamation.objects.all().delete()
        self._periode(niveau=1)                     # MPSI
        self.assertFalse(peut_reclamer(self.note),
                         'une période visant la 1re année ne couvre pas un MP')

    def test_un_niveau_vide_vaut_tous_les_niveaux(self):
        self._periode(niveau=None)
        self.assertTrue(peut_reclamer(self.note))

    # ── Garde-fous ──────────────────────────────────────────────────────────
    def test_sans_note_on_ne_reclame_pas(self):
        self.assertFalse(peut_reclamer(None))

    def test_un_etudiant_sans_inscription_n_a_aucune_periode(self):
        self._periode()
        orphelin = Etudiant.objects.create(matricule='999999', nom='Orphelin')
        self.assertEqual(periodes_ouvertes(orphelin), [])
