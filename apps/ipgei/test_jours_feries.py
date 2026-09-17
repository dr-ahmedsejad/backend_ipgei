"""
Test — les jours fériés et leur effet sur l'emploi du temps.

LA CONVENTION : un jour férié ISOLÉ garde le numéro de sa semaine et prend le
type `ferie` ; une semaine ENTIÈRE hors cours perd son numéro, comme avant.
Appartenir à la séquence, c'est avoir un numéro.

Le calendrier qui fait foi est `parametres.Semaine`, une ligne par JOUR. Les
séances vivent sur `ipgei.SemaineIPGEI`, une ligne par SEMAINE, reflet du
premier. Le jeu d'essai reproduit les deux, et passe par la vraie
synchronisation plutôt que de fabriquer le reflet à la main : c'est elle qui,
avant correction, coupait en deux une semaine contenant un férié.

Le 28/11/2026 est un samedi, dans la semaine 2 du jeu d'essai.

Enveloppe d'erreur, relevée dans `core/exceptions.py` avant d'écrire les
assertions : un 400 STRUCTURÉ range ses messages sous `errors` ; un 400 en
liste, un 404 ou un 409 levés par exception sous `error`, aplatis ; et les
`Response({'error': …})` directes de `SemaineViewSet` restent à la racine.

Base SQLite en mémoire (siga.settings.test).
"""
import importlib
from datetime import date, timedelta

from django.apps import apps as registre
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.authentication.models import CustomUser
from apps.ipgei import feries, miroir, services
from apps.ipgei.models import (
    Classe, GrilleType, Matiere, NiveauIPGEI, PermutationProf, SeanceReelle,
    SeanceType, SemaineIPGEI, SemestreIPGEI,
)
from apps.parametres.models import (
    Creneau, Institution, Jour, JourFerieFixe, Niveau, Seance, Semaine,
)
from apps.suivi.models import Suivie

ANNEE = '2026-2027'
LUNDI_S1 = date(2026, 11, 16)
SAMEDI = date(2026, 11, 28)          # semaine 2
VENDREDI = date(2026, 11, 27)        # semaine 2


class _Calendrier(TestCase):
    """Trois semaines de cours, lundi → samedi, et une classe qui y a cours."""

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(acronyme='TST', nom='Institut Test',
                                              est_principale=True)
        cls.socle = Niveau.objects.create(niveau='MPSI')
        cls.ref = NiveauIPGEI.objects.create(institution=cls.inst, code='MPSI',
                                             libelle='MPSI', rang=1)
        cls.classe = Classe.objects.create(
            nom='MPSI A', institution=cls.inst, annee_universitaire=ANNEE,
            niveau=cls.socle, groupe='A')
        cls.jours = {nom: Jour.objects.create(jour=nom) for nom in
                     ('Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi')}
        cls.matin = Creneau.objects.create(creneau='08h00-09h30', ordre=1)
        cls.soir  = Creneau.objects.create(creneau='15h10-16h40', ordre=2)
        cls.cm = Seance.objects.create(type_seance='CM')
        cls.math = Matiere.objects.create(
            institution=cls.inst, code='MATH', intitule='Mathematiques',
            code_semestre='S1', niveau_ref=cls.ref)
        cls.admin = CustomUser.objects.create_user(
            username='admin', email='a@t.l', password='Admin!2612mdp',
            role='admin', is_superuser=True)

    def setUp(self):
        self._calendrier(3)
        self.s1 = SemestreIPGEI.objects.create(
            institution=self.inst, code='S1', annee_universitaire=ANNEE,
            date_debut=LUNDI_S1)
        services.synchroniser_calendrier(self.s1)
        self.semaines = {s.numero: s for s in self.s1.semaines.all()}

    # ── fabriques ───────────────────────────────────────────────────────────
    def _calendrier(self, nb, depart=LUNDI_S1, premier_numero=1):
        for i in range(nb):
            for j, jour in enumerate(self.jours.values()):
                Semaine.objects.create(
                    numero_semaine=premier_numero + i, jour_fk=jour,
                    date=depart + timedelta(weeks=i, days=j),
                    annee_universitaire=ANNEE, type_semestre='I')

    def _seance(self, numero, nom_jour, creneau=None, **extra):
        semaine = self.semaines[numero]
        jour = self.jours[nom_jour]
        return SeanceReelle.objects.create(
            classe=self.classe, semaine=semaine, jour=jour,
            creneau=creneau or self.matin, matiere=self.math, type_seance=self.cm,
            date=services._date_seance(semaine, jour), **extra)

    def _ligne(self, d):
        return Semaine.objects.get(annee_universitaire=ANNEE, type_semestre='I', date=d)

    def _api(self, vue, methode, donnees=None, pk=None, **params):
        fabrique = APIRequestFactory()
        if methode == 'get':
            requete = fabrique.get('/', params)
        else:
            requete = getattr(fabrique, methode)('/', donnees or {}, format='json')
        force_authenticate(requete, user=self.admin)
        return vue(requete, pk=pk) if pk is not None else vue(requete)


# ═════════════════════════════════════════════════════════════════════════════
# Règles 1 à 4 — marquer, annuler, retirer, bloquer
# ═════════════════════════════════════════════════════════════════════════════
class MarquageTest(_Calendrier):

    def test_le_jour_garde_son_numero_et_rien_d_autre_ne_bouge(self):
        avant = {l.pk: (l.numero_semaine, l.type_semaine)
                 for l in Semaine.objects.all()}
        bilan = feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, "Fête de l'Indépendance")

        ligne = self._ligne(SAMEDI)
        self.assertEqual((ligne.numero_semaine, ligne.type_semaine), (2, 'ferie'))
        self.assertEqual(ligne.description, "Fête de l'Indépendance")
        apres = {l.pk: (l.numero_semaine, l.type_semaine)
                 for l in Semaine.objects.exclude(pk=ligne.pk)}
        self.assertEqual(apres, {k: v for k, v in avant.items() if k != ligne.pk},
                         'aucun autre jour ne doit être renuméroté ni retypé')
        self.assertTrue(bilan['changed'])

    def test_ses_seances_sont_annulees_pas_supprimees_et_sortent_de_la_projection(self):
        a = self._seance(2, 'Samedi')
        b = self._seance(2, 'Samedi', self.soir)
        c = self._seance(2, 'Vendredi')

        bilan = feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')
        self.assertEqual(bilan['seances_annulees'], 2)
        self.assertEqual(bilan['message'],
                         'Samedi 28/11/2026 marqué férié — 2 séances annulées')

        for s in (a, b):
            s.refresh_from_db()
            self.assertTrue(s.annulee)
            self.assertEqual(s.motif_annulation, 'ferie')
        c.refresh_from_db()
        self.assertFalse(c.annulee, 'le vendredi ne doit pas être touché')
        self.assertEqual(SeanceReelle.objects.count(), 3, 'rien de supprimé')

        from apps.emplois.models import Emplois
        miroir.projeter_semaine_reelle(ANNEE, 'I', 2)
        projetes = Emplois.objects.filter(annee_universitaire=ANNEE)
        self.assertEqual(projetes.count(), 1, 'seul le vendredi est projeté')
        self.assertEqual(projetes.get().jour_fk, self.jours['Vendredi'])

    def test_le_retrait_retablit_le_ferie_pas_l_annulation_manuelle(self):
        du_ferie = self._seance(2, 'Samedi')
        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')
        # Annulée à la main APRÈS coup, sur une autre case du même jour : le
        # rétablissement ne doit pas la toucher.
        a_la_main = self._seance(2, 'Samedi', self.soir, annulee=True)

        bilan = feries.retirer_jour_ferie(ANNEE, 'I', SAMEDI)
        du_ferie.refresh_from_db()
        a_la_main.refresh_from_db()
        self.assertFalse(du_ferie.annulee)
        self.assertEqual(du_ferie.motif_annulation, '')
        self.assertTrue(a_la_main.annulee, 'une annulation manuelle reste annulée')
        self.assertEqual(self._ligne(SAMEDI).type_semaine, 'cours')
        self.assertEqual(bilan['message'], 'Samedi 28/11/2026 rendu aux cours — '
                         '1 rétablie (1 annulée à la main reste annulée)')

    def test_une_annulation_puis_un_retablissement_a_la_main_effacent_le_motif(self):
        from apps.ipgei.serializers import SeanceReelleSerializer

        seance = self._seance(2, 'Vendredi', annulee=True,
                              motif_annulation=SeanceReelle.MOTIF_FERIE)
        entree = SeanceReelleSerializer(seance, data={'annulee': False}, partial=True)
        self.assertTrue(entree.is_valid(), entree.errors)
        entree.save()
        seance.refresh_from_db()
        self.assertEqual(seance.motif_annulation, '')

    def test_un_suivi_genere_bloque_marquage_et_retrait(self):
        Suivie.objects.create(annee_universitaire=ANNEE, type_semestre='I',
                              numero_semaine=2, institution=self.inst)
        with self.assertRaises(feries.MarquageBloque) as refus:
            feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')
        self.assertIn('suivi', str(refus.exception))
        self.assertIn('Supprimez', str(refus.exception), 'le message dit quoi faire')

        # Posé en base directement : on éprouve le RETRAIT sous suivi.
        Semaine.objects.filter(date=SAMEDI).update(type_semaine='ferie',
                                                   description='Indépendance')
        with self.assertRaises(feries.MarquageBloque):
            feries.retirer_jour_ferie(ANNEE, 'I', SAMEDI)

    def test_refus_motives_par_l_api(self):
        from apps.parametres.views import SemaineViewSet

        marquer = SemaineViewSet.as_view({'post': 'marquer_jour_ferie'})
        inconnu = self._api(marquer, 'post', {'annee_universitaire': ANNEE,
                                              'type_semestre': 'I',
                                              'date': '2031-01-01', 'libelle': 'X'})
        self.assertEqual(inconnu.status_code, 404)
        self.assertIn('Aucun jour du calendrier', inconnu.data['error'])

        vide = self._api(marquer, 'post', {'annee_universitaire': ANNEE,
                                           'type_semestre': 'I',
                                           'date': SAMEDI.isoformat(), 'libelle': '  '})
        self.assertEqual(vide.status_code, 400)
        self.assertIn('obligatoire', vide.data['error'])

        Suivie.objects.create(annee_universitaire=ANNEE, type_semestre='I',
                              numero_semaine=2, institution=self.inst)
        bloque = self._api(marquer, 'post', {'annee_universitaire': ANNEE,
                                             'type_semestre': 'I',
                                             'date': SAMEDI.isoformat(),
                                             'libelle': 'Indépendance'})
        self.assertEqual(bloque.status_code, 409)

    def test_l_api_de_marquage_reussit_et_dit_combien(self):
        from apps.parametres.views import SemaineViewSet

        self._seance(2, 'Samedi')
        reponse = self._api(SemaineViewSet.as_view({'post': 'marquer_jour_ferie'}),
                            'post', {'annee_universitaire': ANNEE, 'type_semestre': 'I',
                                     'date': SAMEDI.isoformat(), 'libelle': 'Indépendance'})
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.data['seances_annulees'], 1)

    def test_la_semaine_ipgei_reste_de_cours_et_garde_ses_dates(self):
        """Avant correction, la synchronisation la passait en vacances, réduite au samedi."""
        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')
        services.synchroniser_calendrier(self.s1)
        semaine = SemaineIPGEI.objects.get(semestre=self.s1, numero=2)
        self.assertEqual(semaine.type_semaine, 'cours')
        self.assertEqual((semaine.date_debut, semaine.date_fin),
                         (date(2026, 11, 23), SAMEDI))


# ═════════════════════════════════════════════════════════════════════════════
# Règle 5 — ce qu'un jour fermé refuse, côté serveur
# ═════════════════════════════════════════════════════════════════════════════
class JourFermeTest(_Calendrier):

    def _vue(self, actions):
        from apps.ipgei.views import SeanceReelleViewSet
        return SeanceReelleViewSet.as_view(actions)

    def test_ajouter_une_seance_est_refuse_un_jour_ferie(self):
        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')
        reponse = self._api(self._vue({'post': 'create'}), 'post', {
            'classe': self.classe.pk, 'semaine': self.semaines[2].pk,
            'jour': self.jours['Samedi'].pk, 'creneau': self.matin.pk,
            'matiere': self.math.pk, 'type_seance': self.cm.pk})
        self.assertEqual(reponse.status_code, 400)
        self.assertIn("n'est pas un jour de cours", ' '.join(reponse.data['errors']['jour']))
        self.assertFalse(SeanceReelle.objects.exists())

    def test_retablir_une_seance_est_refuse_un_jour_ferie(self):
        seance = self._seance(2, 'Samedi')
        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')
        reponse = self._api(self._vue({'patch': 'partial_update'}), 'patch',
                            {'annulee': False}, pk=seance.pk)
        self.assertEqual(reponse.status_code, 400)
        self.assertIn('férié', ' '.join(reponse.data['errors']['jour']))
        seance.refresh_from_db()
        self.assertTrue(seance.annulee)

    def test_modifier_supprimer_reporter_refuses_puis_seance_intacte_retablie(self):
        seance = self._seance(2, 'Samedi')
        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')

        modif = self._api(self._vue({'patch': 'partial_update'}), 'patch',
                          {'observations': 'changée'}, pk=seance.pk)
        self.assertEqual(modif.status_code, 400)
        self.assertIn('impossible de modifier', ' '.join(modif.data['errors']['jour']))

        # 400 en LISTE (ValidationError de service) : aplati sous `error`.
        suppr = self._api(self._vue({'delete': 'destroy'}), 'delete', pk=seance.pk)
        self.assertEqual(suppr.status_code, 400)
        self.assertIn('impossible de supprimer', suppr.data['error'])
        self.assertTrue(SeanceReelle.objects.filter(pk=seance.pk).exists())

        lot = self._api(self._vue({'post': 'appliquer_lot'}), 'post',
                        {'nb_semaines': 2, 'annulee': False}, pk=seance.pk)
        self.assertEqual(lot.status_code, 400)
        self.assertIn('impossible de reporter', lot.data['error'])

        feries.retirer_jour_ferie(ANNEE, 'I', SAMEDI)
        seance.refresh_from_db()
        self.assertFalse(seance.annulee)
        self.assertEqual(seance.observations, '', 'la séance revient telle quelle')

    def test_permuter_une_seance_d_un_ferie_isole_est_refuse(self):
        a = self._seance(2, 'Samedi')
        b = self._seance(2, 'Samedi', self.soir)
        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')
        permutation = PermutationProf(seance_a=a, seance_b=b, nb_semaines=1,
                                      statut='validee', motif='essai')
        with self.assertRaises(ValidationError) as refus:
            services.appliquer_permutation_prof(permutation)
        self.assertIn('férié', ' '.join(refus.exception.messages),
                      'refusée pour le férié, pas pour son statut')

    def test_supprimer_est_permis_sur_une_semaine_entiere_de_vacances(self):
        seance = self._seance(3, 'Lundi')
        SemaineIPGEI.objects.filter(pk=self.semaines[3].pk).update(
            type_semaine=SemaineIPGEI.TYPE_VACANCES)
        reponse = self._api(self._vue({'delete': 'destroy'}), 'delete', pk=seance.pk)
        self.assertEqual(reponse.status_code, 204)
        self.assertFalse(SeanceReelle.objects.filter(pk=seance.pk).exists())

    def test_la_duplication_du_patron_ne_place_rien_un_jour_ferie(self):
        grille = GrilleType.objects.create(classe=self.classe, type_semestre='I')
        SeanceType.objects.create(grille=grille, jour=self.jours['Samedi'],
                                  creneau=self.matin, matiere=self.math, type_seance=self.cm)
        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')
        bilan = services.dupliquer_grille(grille, list(self.semaines.values()))
        self.assertEqual(bilan['creees'], 2, 'samedis des semaines 1 et 3 seulement')
        self.assertFalse(SeanceReelle.objects.filter(date=SAMEDI).exists())
        self.assertTrue(any('Indépendance' in d for d in bilan['details_conflits']))


# ═════════════════════════════════════════════════════════════════════════════
# Règle 6 — la reprise en patron
# ═════════════════════════════════════════════════════════════════════════════
class RepriseEnPatronTest(_Calendrier):

    def test_la_reprise_garde_le_jour_ferie_pas_l_annulation_manuelle(self):
        self._seance(2, 'Samedi')                             # annulée par le férié
        self._seance(2, 'Vendredi', self.soir, annulee=True)  # annulée à la main
        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')

        grille = GrilleType.objects.create(classe=self.classe, type_semestre='I')
        bilan = services.alimenter_patron_depuis_semaine(grille, self.semaines[2])
        self.assertEqual(bilan['creees'], 1)
        modele = grille.seances.get()
        self.assertEqual(modele.jour, self.jours['Samedi'],
                         'le samedi du 28 novembre est un vrai cours du modèle')


# ═════════════════════════════════════════════════════════════════════════════
# Les trois pièges du marquage d'une semaine entière
# ═════════════════════════════════════════════════════════════════════════════
class SemaineEntiereTest(_Calendrier):

    def _marquer_type(self, lundi, nouveau_type, description=''):
        from apps.parametres.views import SemaineViewSet
        return self._api(SemaineViewSet.as_view({'post': 'marquer_type'}), 'post', {
            'annee_universitaire': ANNEE, 'type_semestre': 'I',
            'date_debut': lundi.isoformat(), 'nouveau_type': nouveau_type,
            'description': description})

    def test_une_semaine_contenant_un_ferie_isole_peut_etre_marquee(self):
        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')
        reponse = self._marquer_type(date(2026, 11, 23), 'vacances', 'Pause')
        self.assertEqual(reponse.status_code, 200, reponse.data)

    def test_marquer_la_semaine_entiere_renumerote_aussi_les_feries_isoles_suivants(self):
        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')   # semaine 2
        reponse = self._marquer_type(LUNDI_S1, 'vacances')               # semaine 1
        self.assertEqual(reponse.status_code, 200, reponse.data)
        self.assertEqual(self._ligne(SAMEDI).numero_semaine, 1,
                         'le férié isolé suit sa semaine dans la renumérotation')
        self.assertEqual(
            set(Semaine.objects.filter(date__gte=date(2026, 11, 23),
                                       date__lte=SAMEDI)
                .values_list('numero_semaine', flat=True)), {1},
            'la semaine ne doit pas être coupée en deux numéros')

    def test_changer_la_description_n_ecrase_pas_le_nom_du_ferie(self):
        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')
        reponse = self._marquer_type(date(2026, 11, 23), 'cours', 'Semaine banalisée')
        self.assertEqual(reponse.status_code, 200, reponse.data)
        self.assertEqual(self._ligne(SAMEDI).description, 'Indépendance')
        self.assertEqual(self._ligne(VENDREDI).description, 'Semaine banalisée')

    def test_remettre_la_semaine_en_cours_retablit_les_seances_du_ferie(self):
        du_ferie = self._seance(2, 'Samedi')
        a_la_main = self._seance(2, 'Vendredi', annulee=True)
        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')
        self.assertEqual(self._marquer_type(date(2026, 11, 23), 'vacances').status_code, 200)

        reponse = self._marquer_type(date(2026, 11, 23), 'cours')
        self.assertEqual(reponse.status_code, 200, reponse.data)
        du_ferie.refresh_from_db()
        a_la_main.refresh_from_db()
        self.assertFalse(du_ferie.annulee)
        self.assertTrue(a_la_main.annulee)

    def test_la_vue_regroupee_ne_coupe_pas_la_semaine(self):
        from apps.parametres.views import SemaineViewSet

        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')
        reponse = self._api(SemaineViewSet.as_view({'get': 'grouped'}), 'get',
                            annee_universitaire=ANNEE, type_semestre='I')
        self.assertEqual(len(reponse.data), 3, 'trois semaines, pas quatre')
        deuxieme = next(g for g in reponse.data if g['numero_semaine'] == 2)
        self.assertEqual(deuxieme['type_semaine'], 'cours')
        self.assertEqual(len(deuxieme['ids']), 6)
        self.assertEqual([j['date'] for j in deuxieme['jours_feries']], [SAMEDI.isoformat()])


# ═════════════════════════════════════════════════════════════════════════════
# Règle 7 — les fériés fixes
# ═════════════════════════════════════════════════════════════════════════════
class FeriesFixesTest(_Calendrier):

    def setUp(self):
        super().setUp()
        JourFerieFixe.objects.all().delete()
        JourFerieFixe.objects.create(jour=28, mois=11, libelle='Indépendance')
        JourFerieFixe.objects.create(jour=16, mois=11, libelle='Inactif', actif=False)

    def test_bornes_aux_lignes_designees_et_inactifs_ignores(self):
        semaine_1 = Semaine.objects.filter(date__lt=date(2026, 11, 23))
        bilan = feries.appliquer_feries_fixes(semaine_1)
        self.assertEqual(bilan['appliques'], [], 'le 28/11 est hors des lignes désignées')
        self.assertEqual(self._ligne(SAMEDI).type_semaine, 'cours')
        self.assertEqual(self._ligne(LUNDI_S1).type_semaine, 'cours',
                         'un férié fixe inactif est ignoré')

        bilan = feries.appliquer_feries_fixes(Semaine.objects.all())
        self.assertEqual([a['date'] for a in bilan['appliques']], [SAMEDI.isoformat()])

    def test_idempotent(self):
        feries.appliquer_feries_fixes(Semaine.objects.all())
        self.assertEqual(feries.appliquer_feries_fixes(Semaine.objects.all())['appliques'], [])

    def test_un_jour_bloque_par_un_suivi_est_ecarte_sans_empecher_les_autres(self):
        JourFerieFixe.objects.create(jour=1, mois=12, libelle='Autre')   # mardi, semaine 3
        Suivie.objects.create(annee_universitaire=ANNEE, type_semestre='I',
                              numero_semaine=2, institution=self.inst)
        bilan = feries.appliquer_feries_fixes(Semaine.objects.all())
        self.assertEqual([e['date'] for e in bilan['ecartes']], [SAMEDI.isoformat()])
        self.assertIn('suivi', bilan['ecartes'][0]['motif'])
        self.assertEqual([a['date'] for a in bilan['appliques']], ['2026-12-01'])

    def test_la_generation_applique_aux_seuls_jours_tout_juste_crees(self):
        from apps.parametres.views import SemaineViewSet

        # Un ancien 28/11 laissé en cours : la génération ne doit PAS y toucher.
        reponse = self._api(SemaineViewSet.as_view({'post': 'ajouter_batch'}), 'post', {
            'annee_universitaire': ANNEE, 'type_semestre': 'I',
            'date_debut': '2027-11-22', 'nombre_semaines': 1})
        self.assertEqual(reponse.status_code, 201, reponse.data)
        self.assertEqual(self._ligne(SAMEDI).type_semaine, 'cours')
        nouveau = self._ligne(date(2027, 11, 27))
        self.assertEqual(nouveau.type_semaine, 'cours', '27/11/2027 n\'est pas le 28')
        self.assertFalse(Semaine.objects.filter(date=date(2027, 11, 28)).exists(),
                         'le jeu d\'essai ne génère pas le dimanche')

    def test_la_generation_marque_un_ferie_fixe_parmi_les_jours_crees(self):
        from apps.parametres.views import SemaineViewSet

        reponse = self._api(SemaineViewSet.as_view({'post': 'ajouter_batch'}), 'post', {
            'annee_universitaire': ANNEE, 'type_semestre': 'I',
            'date_debut': '2028-11-27', 'nombre_semaines': 1})
        self.assertEqual(reponse.status_code, 201, reponse.data)
        self.assertEqual(self._ligne(date(2028, 11, 28)).type_semaine, 'ferie')
        self.assertEqual(self._ligne(SAMEDI).type_semaine, 'cours',
                         "l'ancien 28/11/2026 reste en cours")

    def test_le_29_fevrier_est_valide(self):
        JourFerieFixe(jour=29, mois=2, libelle='Bissextile').full_clean()
        with self.assertRaises(ValidationError):
            JourFerieFixe(jour=30, mois=2, libelle='Impossible').clean()


# ═════════════════════════════════════════════════════════════════════════════
# La migration de données
# ═════════════════════════════════════════════════════════════════════════════
class MigrationFeriesMauritanieTest(TestCase):

    def setUp(self):
        self.migration = importlib.import_module(
            'apps.parametres.migrations.0018_feries_fixes_mauritanie')

    def test_cree_les_4_sans_doublon_et_le_retour_ne_retire_qu_elles(self):
        JourFerieFixe.objects.all().delete()
        self.migration.creer(registre, None)
        self.assertEqual(
            set(JourFerieFixe.objects.values_list('jour', 'mois')),
            {(1, 1), (1, 5), (25, 5), (28, 11)})
        self.assertTrue(all(JourFerieFixe.objects.values_list('actif', flat=True)))

        JourFerieFixe.objects.filter(jour=1, mois=5).update(libelle='Libellé corrigé')
        self.migration.creer(registre, None)
        self.assertEqual(JourFerieFixe.objects.count(), 4, 'rejouer ne crée pas de doublon')
        self.assertEqual(JourFerieFixe.objects.get(jour=1, mois=5).libelle,
                         'Libellé corrigé', 'rejouer n\'écrase pas un libellé modifié')

        JourFerieFixe.objects.create(jour=15, mois=8, libelle='Ajouté à la main')
        self.migration.retirer(registre, None)
        self.assertEqual(list(JourFerieFixe.objects.values_list('jour', 'mois')), [(15, 8)])

    def test_aucune_fete_religieuse(self):
        libelles = ' '.join(l for _, _, l in self.migration.FERIES).lower()
        for fete in ('aïd', 'aid', 'hégire', 'mawlid', 'tabaski', 'ramadan'):
            self.assertNotIn(fete, libelles)


# ═════════════════════════════════════════════════════════════════════════════
# Les chemins qui recopient — ajoutés parce que le sabotage les aurait manqués
# ═════════════════════════════════════════════════════════════════════════════
class RecopiesTest(_Calendrier):

    def test_retablir_est_refuse_sur_une_semaine_entiere_hors_cours(self):
        """
        Isolé du refus de modification : sur un férié ISOLÉ, c'est lui qui tombe
        le premier, et le refus de rétablissement n'y était jamais éprouvé.
        """
        from apps.ipgei.views import SeanceReelleViewSet

        seance = self._seance(3, 'Lundi', annulee=True)
        SemaineIPGEI.objects.filter(pk=self.semaines[3].pk).update(
            type_semaine=SemaineIPGEI.TYPE_VACANCES)
        reponse = self._api(SeanceReelleViewSet.as_view({'patch': 'partial_update'}),
                            'patch', {'annulee': False}, pk=seance.pk)
        self.assertEqual(reponse.status_code, 400)
        self.assertIn('ne peut pas y être rétablie', ' '.join(reponse.data['errors']['jour']))

    def test_le_report_en_lot_saute_le_jour_ferie_sans_s_interrompre(self):
        from apps.ipgei.views import SeanceReelleViewSet

        source = self._seance(1, 'Samedi')
        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')
        reponse = self._api(SeanceReelleViewSet.as_view({'post': 'appliquer_lot'}), 'post',
                            {'nb_semaines': 3, 'annulee': False}, pk=source.pk)
        self.assertEqual(reponse.status_code, 200, reponse.data)
        self.assertEqual(reponse.data['jours_fermes_ecartes'], 1)
        self.assertFalse(SeanceReelle.objects.filter(date=SAMEDI).exists(),
                         'rien ne se pose sur le 28 novembre')
        self.assertTrue(SeanceReelle.objects.filter(date=date(2026, 12, 5)).exists(),
                        'la semaine 3 reçoit bien sa séance')

    def test_la_duplication_de_semaine_recopie_les_seances_du_ferie(self):
        self._seance(2, 'Samedi')                             # annulée par le férié
        self._seance(2, 'Vendredi', self.soir, annulee=True)  # annulée à la main
        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')

        bilan = services.dupliquer_semaine(self.classe, self.semaines[2], [self.semaines[3]])
        self.assertEqual(bilan['creees'], 1)
        copie = SeanceReelle.objects.get(semaine=self.semaines[3])
        self.assertEqual(copie.jour, self.jours['Samedi'])
        self.assertFalse(copie.annulee, 'le samedi de la semaine 3 est un jour de cours')

    def test_la_duplication_de_semaine_ne_pose_rien_un_jour_ferie(self):
        self._seance(1, 'Samedi')
        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')
        bilan = services.dupliquer_semaine(self.classe, self.semaines[1], [self.semaines[2]])
        self.assertEqual(bilan['creees'], 0)
        self.assertFalse(SeanceReelle.objects.filter(date=SAMEDI).exists())
        self.assertTrue(any('Indépendance' in d for d in bilan['details_conflits']))


class BornesDuSuiviTest(_Calendrier):
    """Piège (a), côté suivi : les bornes d'une semaine se lisaient sur ses seuls jours « cours »."""

    def test_un_samedi_ferie_reste_dans_sa_semaine_courante(self):
        from freezegun import freeze_time

        from apps.suivi.views import SuivieViewSet

        feries.marquer_jour_ferie(ANNEE, 'I', SAMEDI, 'Indépendance')
        with freeze_time('2026-11-28'):
            reponse = self._api(SuivieViewSet.as_view({'get': 'semaines_generees'}), 'get',
                                annee_universitaire=ANNEE, type_semestre='I')
        self.assertEqual(reponse.status_code, 200, reponse.data)
        self.assertEqual(reponse.data['current_week'], 2,
                         'le 28/11 appartient à la semaine 2, pas à la suivante')
