"""
Tests — cohérence maquette coefficient(module) = Σ coefficients(EM).
Convention Art. 14 Arrêté 562 / Art. 19 Décret 2018-070 (la MGS sur les EM
n'égale la moyenne pondérée des modules que sous cette convention).

Exécution : python -m pytest apps/evaluations/test_coherence_maquette.py
"""
from decimal import Decimal

from django.test import TestCase

from apps.parametres.models import Niveau, Semestre, Institution
from apps.scolarite.models import Filiere
from apps.modules.models import Module, ElementModule
from apps.em.models import EM
from apps.evaluations.services.coherence_maquette import (
    verifier_coherence_coefficients, warnings_coefficients,
)


class CoherenceCoefficientsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(
            acronyme='TST', nom='Institut Test', est_principale=True,
        )
        cls.niveau = Niveau.objects.create(niveau='1')
        cls.semestre = Semestre.objects.create(
            code_semestre='S1', semestre='Semestre 1',
            niveau_semestre=cls.niveau, type_semestre='I', credits=30,
        )
        cls.filiere = Filiere.objects.create(
            code='LPTEST', intitule_fr='Licence Test',
            type_diplome='LP', institution=cls.inst,
        )

    def _module(self, code, coefficient):
        return Module.objects.create(
            code=code, intitule_fr=f'Module {code}',
            semestre=self.semestre, filiere=self.filiere,
            credits=6, coefficient=Decimal(str(coefficient)),
        )

    def _em(self, module, code, coefficient):
        return EM.objects.create(
            code_em=code, intitule=f'EM {code}',
            semestre=self.semestre, module_lmd=module,
            coefficient=coefficient,
        )

    def test_module_incoherent_detecte(self):
        """coefficient=1.00 (défaut) avec 2 EM (1+2=3) → anomalie."""
        mod = self._module('MOD-A', '1.00')
        self._em(mod, 'EMA1', 1)
        self._em(mod, 'EMA2', 2)

        anomalies = verifier_coherence_coefficients(filiere=self.filiere)
        self.assertEqual(len(anomalies), 1)
        a = anomalies[0]
        self.assertEqual(a['module_code'], 'MOD-A')
        self.assertEqual(a['coefficient_module'], Decimal('1.00'))
        self.assertEqual(a['somme_coefficients_em'], Decimal('3'))
        self.assertEqual(a['nb_em'], 2)

    def test_module_coherent_ok(self):
        """coefficient=3.00 avec 2 EM (1+2=3) → aucune anomalie."""
        mod = self._module('MOD-B', '3.00')
        self._em(mod, 'EMB1', 1)
        self._em(mod, 'EMB2', 2)

        anomalies = verifier_coherence_coefficients(filiere=self.filiere)
        self.assertEqual(anomalies, [])

    def test_em_sans_coefficient_compte_pour_1(self):
        """EM coefficient NULL → effectif 1 (même fallback que le moteur)."""
        mod = self._module('MOD-C', '2.00')
        self._em(mod, 'EMC1', None)    # → 1
        self._em(mod, 'EMC2', 1)       # → 1   somme = 2 = coefficient → OK

        anomalies = verifier_coherence_coefficients(filiere=self.filiere)
        self.assertEqual(anomalies, [])

    def test_module_vide_ignore(self):
        """Module sans EM ni ElementModule → ignoré (pas une anomalie coeff)."""
        self._module('MOD-VIDE', '1.00')
        anomalies = verifier_coherence_coefficients(filiere=self.filiere)
        self.assertEqual(anomalies, [])

    def test_fallback_element_module(self):
        """Sans EM de planification, la somme se calcule sur les ElementModule."""
        mod = self._module('MOD-D', '1.00')
        ElementModule.objects.create(
            module=mod, code='ELD1', intitule_fr='El 1',
            credits=3, coefficient=Decimal('2.00'),
        )
        ElementModule.objects.create(
            module=mod, code='ELD2', intitule_fr='El 2',
            credits=3, coefficient=Decimal('2.00'),
        )
        anomalies = verifier_coherence_coefficients(filiere=self.filiere)
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]['somme_coefficients_em'], Decimal('4.00'))

    def test_warnings_formatage_et_cap(self):
        """warnings_coefficients : messages détaillés + résumé au-delà du cap."""
        for i in range(7):
            mod = self._module(f'MOD-W{i}', '1.00')
            self._em(mod, f'EMW{i}a', 1)
            self._em(mod, f'EMW{i}b', 2)

        anomalies = verifier_coherence_coefficients(filiere=self.filiere)
        self.assertEqual(len(anomalies), 7)
        msgs = warnings_coefficients(anomalies, max_detail=5)
        self.assertEqual(len(msgs), 6)               # 5 détaillés + 1 résumé
        self.assertIn('Art. 14', msgs[0])
        self.assertIn('verifier_maquettes', msgs[-1])


class StructureMaquetteTest(TestCase):
    """
    Art. 8 Arrêté 562 / Art. 13-14 Décret 2018-070 :
    3-5 modules par semestre (2 en S4 LP, 1 en S6), 3 éléments max par module.
    """

    @classmethod
    def setUpTestData(cls):
        from apps.parametres.models import Niveau, Semestre, Institution
        from apps.scolarite.models import Filiere
        cls.inst = Institution.objects.create(
            acronyme='TST2', nom='Institut Test 2', est_principale=True,
        )
        cls.niveau1 = Niveau.objects.create(niveau='1')
        cls.niveau3 = Niveau.objects.create(niveau='3')
        cls.s1 = Semestre.objects.create(
            code_semestre='S1', semestre='Semestre 1',
            niveau_semestre=cls.niveau1, type_semestre='I', credits=30,
        )
        cls.s6 = Semestre.objects.create(
            code_semestre='S6', semestre='Semestre 6',
            niveau_semestre=cls.niveau3, type_semestre='P', credits=30,
        )
        cls.filiere_lp = Filiere.objects.create(
            code='LPSTRUCT', intitule_fr='LP Structure',
            type_diplome='LP', institution=cls.inst,
        )
        cls.filiere_ing = Filiere.objects.create(
            code='INGSTRUCT', intitule_fr='ING Structure',
            type_diplome='ING', institution=cls.inst,
        )

    def _module(self, filiere, semestre, code):
        return Module.objects.create(
            code=code, intitule_fr=f'Module {code}',
            semestre=semestre, filiere=filiere, credits=6,
        )

    def test_s1_avec_2_modules_signale(self):
        """LP S1 : 2 modules < borne basse (3) → anomalie Art. 8."""
        from apps.evaluations.services.coherence_maquette import verifier_structure_maquette
        self._module(self.filiere_lp, self.s1, 'ST-A')
        self._module(self.filiere_lp, self.s1, 'ST-B')
        msgs = verifier_structure_maquette(self.filiere_lp)
        self.assertEqual(len(msgs), 1)
        self.assertIn('2 module(s)', msgs[0])
        self.assertIn('Art. 8', msgs[0])

    def test_s1_avec_3_modules_ok(self):
        from apps.evaluations.services.coherence_maquette import verifier_structure_maquette
        for c in ('ST-C', 'ST-D', 'ST-E'):
            self._module(self.filiere_lp, self.s1, c)
        self.assertEqual(verifier_structure_maquette(self.filiere_lp), [])

    def test_s6_lp_un_seul_module_ok_deux_signale(self):
        """LP S6 : 1 module (stage) attendu — 2 → anomalie."""
        from apps.evaluations.services.coherence_maquette import verifier_structure_maquette
        self._module(self.filiere_lp, self.s6, 'ST-S6A')
        self.assertEqual(verifier_structure_maquette(self.filiere_lp), [])
        self._module(self.filiere_lp, self.s6, 'ST-S6B')
        msgs = verifier_structure_maquette(self.filiere_lp)
        self.assertEqual(len(msgs), 1)
        self.assertIn('attendu 1', msgs[0])

    def test_module_avec_4_elements_signale(self):
        """4 EM rattachés à un module → anomalie « maximum 3 »."""
        from apps.evaluations.services.coherence_maquette import verifier_structure_maquette
        mods = [self._module(self.filiere_lp, self.s1, f'ST-F{i}') for i in range(3)]
        for i in range(4):
            EM.objects.create(
                code_em=f'EMS{i}', intitule=f'EM {i}',
                semestre=self.s1, module_lmd=mods[0],
            )
        msgs = verifier_structure_maquette(self.filiere_lp)
        self.assertEqual(len(msgs), 1)
        self.assertIn('maximum 3', msgs[0])

    def test_ing_pfe_s6_doit_avoir_3_elements(self):
        """ING S6 : module PFE à 2 EM → anomalie Art. 14 Décret."""
        from apps.evaluations.services.coherence_maquette import verifier_structure_maquette
        pfe = self._module(self.filiere_ing, self.s6, 'ST-PFE')
        for i in range(2):
            EM.objects.create(
                code_em=f'EMPFE{i}', intitule=f'PFE {i}',
                semestre=self.s6, module_lmd=pfe,
            )
        msgs = verifier_structure_maquette(self.filiere_ing)
        self.assertEqual(len(msgs), 1)
        self.assertIn('Art. 14 Décret 2018-070', msgs[0])

    def test_semestre_sans_module_ignore(self):
        """Aucun module → maquette non commencée, pas d'anomalie."""
        from apps.evaluations.services.coherence_maquette import verifier_structure_maquette
        self.assertEqual(verifier_structure_maquette(self.filiere_lp), [])


class BlocageQuatriemeElementTest(TestCase):
    """
    Les serializers refusent l'AJOUT d'un 4e élément à un module
    (Art. 8 Arrêté 562 / Art. 13 Décret 2018-070), mais laissent
    modifier un élément existant d'un module legacy surchargé.
    """

    @classmethod
    def setUpTestData(cls):
        from apps.parametres.models import Niveau, Semestre, Institution
        from apps.scolarite.models import Filiere
        cls.inst = Institution.objects.create(
            acronyme='TST3', nom='Institut Test 3', est_principale=True,
        )
        niveau = Niveau.objects.create(niveau='1')
        cls.s1 = Semestre.objects.create(
            code_semestre='S1', semestre='Semestre 1',
            niveau_semestre=niveau, type_semestre='I', credits=30,
        )
        filiere = Filiere.objects.create(
            code='LPBLOC', intitule_fr='LP Blocage',
            type_diplome='LP', institution=cls.inst,
        )
        cls.module = Module.objects.create(
            code='MOD-BLOC', intitule_fr='Module Blocage',
            semestre=cls.s1, filiere=filiere, credits=6,
        )

    def test_em_serializer_refuse_4e_em(self):
        from apps.em.serializers import EMSerializer
        from apps.departement.models import Departement
        dept = Departement.objects.create(nom='Dept Bloc', institution=self.inst)
        for i in range(3):
            EM.objects.create(
                code_em=f'EMB{i}', intitule=f'EM {i}',
                semestre=self.s1, module_lmd=self.module,
            )
        ser = EMSerializer(data={
            'code_em': 'EMB3', 'intitule': 'EM 3 — un de trop',
            'semestre': self.s1.pk, 'module_lmd': self.module.pk,
            'departement': dept.pk,
        })
        self.assertFalse(ser.is_valid())
        self.assertIn('module_lmd', ser.errors)

    def test_em_serializer_update_meme_module_autorise(self):
        """Modifier un EM existant (même module surchargé) reste possible."""
        from apps.em.serializers import EMSerializer
        ems = [
            EM.objects.create(
                code_em=f'EMC{i}', intitule=f'EM {i}',
                semestre=self.s1, module_lmd=self.module,
            )
            for i in range(4)   # module legacy déjà surchargé (créé hors API)
        ]
        ser = EMSerializer(ems[0], data={'intitule': 'EM renommé'}, partial=True)
        self.assertTrue(ser.is_valid(), ser.errors)

    def test_element_module_serializer_refuse_4e_element(self):
        from apps.modules.serializers import ElementModuleSerializer
        for i in range(3):
            ElementModule.objects.create(
                module=self.module, code=f'ELB{i}', intitule_fr=f'El {i}',
                credits=2,
            )
        ser = ElementModuleSerializer(data={
            'module': self.module.pk, 'code': 'ELB3',
            'intitule_fr': 'El 3 — un de trop', 'credits': 2,
            'poids_cc': '0.30', 'poids_tp': '0.20', 'poids_exam': '0.50',
        })
        self.assertFalse(ser.is_valid())
        self.assertIn('module', ser.errors)

    # ── Exception admin : peut dépasser le plafond de 3 éléments ──────────────

    def _fake_request(self, *, is_superuser=False, role=''):
        from types import SimpleNamespace
        user = SimpleNamespace(is_superuser=is_superuser, role=role)
        return SimpleNamespace(user=user)

    def test_em_serializer_admin_autorise_4e_em(self):
        """Un admin (role='admin') peut ajouter un 4e EM (exception au plafond)."""
        from apps.em.serializers import EMSerializer
        from apps.departement.models import Departement
        dept = Departement.objects.create(nom='Dept Adm', institution=self.inst)
        for i in range(3):
            EM.objects.create(
                code_em=f'EMD{i}', intitule=f'EM {i}',
                semestre=self.s1, module_lmd=self.module,
            )
        ser = EMSerializer(
            data={
                'code_em': 'EMD3', 'intitule': 'EM 3 — admin override',
                'semestre': self.s1.pk, 'module_lmd': self.module.pk,
                'departement': dept.pk,
            },
            context={'request': self._fake_request(role='admin')},
        )
        self.assertTrue(ser.is_valid(), ser.errors)

    def test_em_serializer_non_admin_bloque_4e_em(self):
        """Un rôle non-admin (ex. enseignant) reste bloqué malgré le contexte."""
        from apps.em.serializers import EMSerializer
        from apps.departement.models import Departement
        dept = Departement.objects.create(nom='Dept Ens', institution=self.inst)
        for i in range(3):
            EM.objects.create(
                code_em=f'EME{i}', intitule=f'EM {i}',
                semestre=self.s1, module_lmd=self.module,
            )
        ser = EMSerializer(
            data={
                'code_em': 'EME3', 'intitule': 'EM 3 — refusé',
                'semestre': self.s1.pk, 'module_lmd': self.module.pk,
                'departement': dept.pk,
            },
            context={'request': self._fake_request(role='enseignant')},
        )
        self.assertFalse(ser.is_valid())
        self.assertIn('module_lmd', ser.errors)

    def test_element_module_serializer_admin_autorise_4e(self):
        """Un superuser peut ajouter un 4e ElementModule."""
        from apps.modules.serializers import ElementModuleSerializer
        for i in range(3):
            ElementModule.objects.create(
                module=self.module, code=f'ELD{i}', intitule_fr=f'El {i}', credits=2,
            )
        ser = ElementModuleSerializer(
            data={
                'module': self.module.pk, 'code': 'ELD3',
                'intitule_fr': 'El 3 — admin', 'credits': 2,
                'poids_cc': '0.30', 'poids_tp': '0.20', 'poids_exam': '0.50',
            },
            context={'request': self._fake_request(is_superuser=True)},
        )
        self.assertTrue(ser.is_valid(), ser.errors)
