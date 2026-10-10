"""
Les relevés IPGEI d'un ou de plusieurs élèves, tous leurs semestres, en un
seul PDF (même adresse qu'à l'ISS). Voir apps/documents/releves_etudiant.py.

Le rendu (wkhtmltopdf) et le signataire sont SIMULÉS : on vérifie la sélection
des semestres, l'ordre, la fusion, le registre et les droits.
"""
from datetime import date
from decimal import Decimal
from io import BytesIO

from django.db import connection
from django.test import TestCase
from pypdf import PdfReader, PdfWriter
from rest_framework.test import APIClient

from apps.absence.models import Etudiant
from apps.authentication.models import CustomUser
from apps.documents.models import DocumentOfficiel
from apps.ipgei.models import (Classe, InscriptionIPGEI, Matiere, NiveauIPGEI, Note,
                               SemestreIPGEI)
from apps.parametres.models import Institution, Niveau
from apps.prof.models import ProfTypeHistory

URL = '/api/v1/documents/officiels/releves-etudiant/'


def _page(texte):
    w = PdfWriter()
    w.add_blank_page(width=72, height=72)
    w.add_metadata({'/Title': texte})
    b = BytesIO()
    w.write(b)
    return b.getvalue()


class RelevesEtudiantIPGEITest(TestCase):

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
        niveau = Niveau.objects.get_or_create(niveau='MP')[0]
        cls.s3 = SemestreIPGEI.objects.create(institution=inst, code='S3', annee_universitaire='2025-2026',
                                              date_debut=date(2025, 10, 6))
        cls.s4 = SemestreIPGEI.objects.create(institution=inst, code='S4', annee_universitaire='2025-2026',
                                              date_debut=date(2026, 1, 26))
        cls.s1 = SemestreIPGEI.objects.create(institution=inst, code='S1', annee_universitaire='2024-2025',
                                              date_debut=date(2024, 10, 7))
        math = Matiere.objects.create(institution=inst, code='MATH', intitule='Mathématiques',
                                      code_semestre='S3', niveau_ref=ref, coefficient=Decimal('4'))
        ancienne = Classe.objects.create(nom='MPSI A', institution=inst, annee_universitaire='2024-2025',
                                         niveau=niveau, groupe='A')
        classe = Classe.objects.create(nom='MP A', institution=inst, annee_universitaire='2025-2026',
                                       niveau=niveau, groupe='A')

        def eleve(matricule):
            return Etudiant.objects.create(matricule=matricule, nom=f'Eleve {matricule}',
                                           nom_fr=f'Eleve {matricule}')

        def note(ins, sem, valeur):
            Note.objects.create(inscription=ins, matiere=math, semestre=sem, note_retenue=valeur,
                                moyenne=valeur, pct_ds=Decimal('30'), pct_tp=Decimal('0'),
                                pct_exam=Decimal('70'), coefficient=Decimal('4'))

        cls.etu = eleve('242001')
        i1 = InscriptionIPGEI.objects.create(etudiant=cls.etu, classe=ancienne, annee_universitaire='2024-2025')
        i2 = InscriptionIPGEI.objects.create(etudiant=cls.etu, classe=classe, annee_universitaire='2025-2026')
        note(i1, cls.s1, Decimal('12.00'))
        note(i2, cls.s3, Decimal('14.00'))
        note(i2, cls.s4, None)                    # semestre en cours : rien de retenu
        cls.sans = eleve('242002')                # aucun résultat
        InscriptionIPGEI.objects.create(etudiant=cls.sans, classe=classe, annee_universitaire='2025-2026')

        cls.admin = CustomUser.objects.create(username='adm', role='admin', email='a@t.l')
        cls.ens = CustomUser.objects.create(username='ens', role='enseignant', email='e@t.l')

    def setUp(self):
        from apps.ipgei import documents as docs_ipgei
        from apps.ipgei import services
        self._orig = (docs_ipgei._rendre, services.signataire_du_document)
        docs_ipgei._rendre = lambda type_doc, ctx: _page(type_doc)
        services.signataire_du_document = lambda *a, **k: {'nom': 'Signataire', 'titre': 'Directeur'}

    def tearDown(self):
        from apps.ipgei import documents as docs_ipgei
        from apps.ipgei import services
        docs_ipgei._rendre, services.signataire_du_document = self._orig

    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def test_ses_semestres_dans_l_ordre(self):
        from apps.documents.releves_etudiant import semestres_de
        lignes = semestres_de(self.etu)
        self.assertEqual([(l['annee_universitaire'], l['semestre_code'], l['a_des_resultats'])
                          for l in lignes],
                         [('2024-2025', 'S1', True), ('2025-2026', 'S3', True), ('2025-2026', 'S4', False)])
        # La « filière » est la classe de l'année (nom calculé par le modèle).
        classe_2024 = InscriptionIPGEI.objects.get(etudiant=self.etu, annee_universitaire='2024-2025').classe
        self.assertEqual((lignes[0]['moyenne'], lignes[0]['filiere']), (12.0, classe_2024.nom))
        self.assertIsNone(lignes[2]['moyenne'])

    def test_apercu_n_emet_rien(self):
        r = self._client(self.admin).get(URL, {'etudiant': self.etu.pk})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual([l['semestre_code'] for l in r.data['semestres']], ['S1', 'S3', 'S4'])
        self.assertFalse(DocumentOfficiel.objects.exists())

    def test_un_pdf_pour_plusieurs_dans_l_ordre(self):
        r = self._client(self.admin).post(URL, {'etudiants': [self.sans.pk, self.etu.pk]}, format='json')
        self.assertEqual(r.status_code, 200, getattr(r, 'data', None))
        self.assertEqual(len(PdfReader(BytesIO(r.content)).pages), 2)      # S1 et S3
        self.assertEqual((r['X-Generated'], r['X-Etudiants-Sans-Releve']), ('2', '242002'))
        docs = DocumentOfficiel.objects.filter(etudiant=self.etu, type_document='ipgei_releve_semestre')
        self.assertEqual(sorted(docs.values_list('semestre_code', flat=True)), ['S1', 'S3'])
        self.assertIn('releves_2_etudiants.pdf', r['Content-Disposition'])

    def test_aucun_resultat(self):
        r = self._client(self.admin).post(URL, {'etudiant': self.sans.pk}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_sans_droit(self):
        r = self._client(self.ens).post(URL, {'etudiant': self.etu.pk}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._client(self.admin).get(URL, {'etudiant': 999999}).status_code, 400)
