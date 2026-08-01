"""
Audit volumétrique pré-migration — Section 0 du plan institution_V1.

Ne modifie rien. Produit un rapport exhaustif :
- Comptes par table (tous modèles liés à Year ou Institution)
- Vérification de l'unicité de l'institution principale
- Lignes sans FK institution (orphelines)
- Filières et départements sans institution

Usage :
    python manage.py audit_institution_annee
    python manage.py audit_institution_annee --json
    python manage.py audit_institution_annee --annee 2025-2026
"""
import json
from collections import OrderedDict

from django.core.management.base import BaseCommand
from django.db.models import Count, Q


class Command(BaseCommand):
    help = "Audit pré-migration : volumes, cohérence institution, FK orphelines."

    def add_arguments(self, parser):
        parser.add_argument('--annee', type=str, default=None,
                            help="Année universitaire à cibler pour les comptes détaillés (ex: '2025-2026').")
        parser.add_argument('--json', action='store_true',
                            help="Sortie JSON machine-readable au lieu de tableau lisible.")

    def handle(self, *args, **opts):
        from apps.parametres.models import Institution, Year, Semestre
        from apps.scolarite.models import Filiere, DepartementAcademique
        from apps.departement.models import Departement
        from apps.absence.models import Etudiant
        from apps.inscriptions.models import (
            Preinscription, InscriptionAdministrative, InscriptionPedagogique,
            InscriptionElement, Derogation, Progression,
        )
        from apps.evaluations.models import (
            SessionEvaluation, PVDeliberation, LigneDeliberation, Note,
            ResultatElement, ResultatSemestre, ResultatModule,
            RachatNote, ObligationRattrapage, AnonymatSession,
            MembreJury, ParametreJury, JustificatifAnneeBlanche,
        )
        from apps.emplois.models import Emplois, EmploisArchive
        from apps.suivi.models import Suivie, SuiviePointage, ChargeInstitution
        from apps.vacation.models import Surveillance, Vacation
        from apps.documents.models import DocumentOfficiel, RegistreDiplome
        from apps.em.models import EM
        from apps.modules.models import Module

        report = OrderedDict()

        # ── 1. Institution : unicité principale ───────────────────────────────
        nb_principales = Institution.objects.filter(est_principale=True).count()
        institutions = list(Institution.objects.values('id', 'acronyme', 'nom_fr', 'nom', 'est_principale', 'code_etablissement'))
        report['institutions'] = {
            'count_total':       Institution.objects.count(),
            'count_principales': nb_principales,
            'unique_principale': nb_principales == 1,
            'liste':             institutions,
        }

        # ── 2. Filière/Departement sans institution (orphelins) ──────────────
        report['orphelins_institution'] = {
            'filiere_sans_institution':                Filiere.objects.filter(institution__isnull=True).count(),
            'departement_sans_institution':            Departement.objects.filter(institution__isnull=True).count(),
            'departement_academique_sans_institution': DepartementAcademique.objects.filter(institution__isnull=True).count(),
            'ids_filieres_orphelines':                 list(Filiere.objects.filter(institution__isnull=True).values_list('id', 'code', 'intitule_fr')),
            'ids_departements_orphelins':              list(Departement.objects.filter(institution__isnull=True).values_list('id', 'nom', 'annee_universitaire')[:50]),
        }

        # ── 3. Years présentes ────────────────────────────────────────────────
        report['years'] = list(Year.objects.values('id', 'annee', 'est_active', 'est_cloturee').order_by('annee'))

        # ── 4. Semestres : détection doublons et instances annualisées ───────
        doublons_semestre = list(
            Semestre.objects.values('code_semestre', 'niveau_semestre_id', 'type_semestre')
            .annotate(c=Count('id')).filter(c__gt=1).order_by('-c')
        )
        sem_report = {
            'count_total':                  Semestre.objects.count(),
            'doublons_par_code_niveau_type': doublons_semestre,
        }
        # Champs filiere/annee_univ supprimés en migration 0006 — tolérance pré/post migration
        try:
            sem_report['avec_filiere']    = Semestre.objects.filter(filiere__isnull=False).count()
            sem_report['avec_annee_univ'] = Semestre.objects.filter(annee_univ__isnull=False).count()
            sem_report['templates_pures'] = Semestre.objects.filter(filiere__isnull=True, annee_univ__isnull=True).count()
        except Exception:
            sem_report['avec_filiere']    = 'N/A (post-migration 0006)'
            sem_report['avec_annee_univ'] = 'N/A (post-migration 0006)'
            sem_report['templates_pures'] = 'N/A (post-migration 0006)'
        report['semestres'] = sem_report

        # ── 5. Comptes Year-scopés (Groupe 1 & 2 du plan) ─────────────────────
        annee = None
        if opts['annee']:
            try:
                annee = Year.objects.get(annee=opts['annee'])
            except Year.DoesNotExist:
                self.stderr.write(self.style.WARNING(f"Année '{opts['annee']}' introuvable — audit global."))

        def _count(model, **filters):
            try:
                return model.objects.filter(**filters).count()
            except Exception as exc:
                return f'err: {exc}'

        annee_filter_fk = {'annee_univ': annee} if annee else {}
        annee_filter_str = {'annee_universitaire': annee.annee} if annee else {}
        annee_filter_vac = {'annee_univ': annee.annee} if annee else {}

        report['comptes_volumetriques'] = {
            # Groupe 2 — FK Year
            'Preinscription':            _count(Preinscription, **annee_filter_fk),
            'InscriptionAdministrative': _count(InscriptionAdministrative, **annee_filter_fk),
            'InscriptionPedagogique':    _count(InscriptionPedagogique, **({'inscription_admin__annee_univ': annee} if annee else {})),
            'InscriptionElement':        _count(InscriptionElement, **({'inscription_ped__inscription_admin__annee_univ': annee} if annee else {})),
            'Derogation':                _count(Derogation, **annee_filter_fk),
            'Progression_source':        _count(Progression, **({'annee_source': annee} if annee else {})),
            'Progression_cible':         _count(Progression, **({'annee_cible': annee} if annee else {})),
            'SessionEvaluation':         _count(SessionEvaluation, **annee_filter_fk),
            'PVDeliberation':            _count(PVDeliberation, **annee_filter_fk),
            'LigneDeliberation':         _count(LigneDeliberation, **({'pv__annee_univ': annee} if annee else {})),
            'Note':                      _count(Note, **({'session__annee_univ': annee} if annee else {})),
            'ResultatElement':           _count(ResultatElement, **({'session__annee_univ': annee} if annee else {})),
            'ResultatSemestre':          _count(ResultatSemestre, **({'session__annee_univ': annee} if annee else {})),
            'ResultatModule':            _count(ResultatModule, **({'session__annee_univ': annee} if annee else {})),
            'RachatNote':                _count(RachatNote, **({'pv__annee_univ': annee} if annee else {})),
            'ObligationRattrapage':      _count(ObligationRattrapage, **({'ligne__pv__annee_univ': annee} if annee else {})),
            'AnonymatSession':           _count(AnonymatSession, **({'session__annee_univ': annee} if annee else {})),
            'MembreJury':                _count(MembreJury, **({'pv__annee_univ': annee} if annee else {})),
            'ParametreJury':             _count(ParametreJury, **({'pv__annee_univ': annee} if annee else {})),
            'JustificatifAnneeBlanche':  _count(JustificatifAnneeBlanche, **({'ligne_deliberation__pv__annee_univ': annee} if annee else {})),

            # Groupe 1 — CharField annee
            'Emplois':                   _count(Emplois, **annee_filter_str),
            'EmploisArchive':            _count(EmploisArchive, **annee_filter_str),
            'Suivie':                    _count(Suivie, **annee_filter_str),
            'SuiviePointage':            _count(SuiviePointage, **annee_filter_str),
            'Surveillance':              _count(Surveillance, **annee_filter_vac),
            'Vacation':                  _count(Vacation, **annee_filter_vac),
            'DocumentOfficiel':          _count(DocumentOfficiel, **annee_filter_str),
            'RegistreDiplome':           _count(RegistreDiplome, **annee_filter_str),
            'ChargeInstitution':         _count(ChargeInstitution, **annee_filter_str),
            'Departement':               _count(Departement, **annee_filter_str),

            # Entités stables (réutilisées)
            'Etudiant_total':            Etudiant.objects.count(),
            'Filiere_total':             Filiere.objects.count(),
            'Module_total':              Module.objects.count(),
            'EM_total':                  EM.objects.count(),
        }

        # ── 6. Bloquants identifiés par le plan ───────────────────────────────
        bloquants = []
        if nb_principales != 1:
            bloquants.append(f"Institution principale non-unique : {nb_principales} trouvées (attendu: 1).")

        if annee and RegistreDiplome.objects.filter(annee_universitaire=annee.annee).exists():
            nb_reg = RegistreDiplome.objects.filter(annee_universitaire=annee.annee).count()
            bloquants.append(f"RegistreDiplome {annee.annee} présent ({nb_reg}) — modèle immuable, purge impossible.")

        if Filiere.objects.filter(institution__isnull=True).exists():
            bloquants.append(
                f"{Filiere.objects.filter(institution__isnull=True).count()} Filière(s) sans institution — "
                "corriger avant Section 1bis."
            )
        if Departement.objects.filter(institution__isnull=True).exists():
            bloquants.append(
                f"{Departement.objects.filter(institution__isnull=True).count()} Departement(s) sans institution — "
                "corriger avant Section 1bis."
            )

        report['bloquants'] = bloquants

        # ── 7. Sortie ────────────────────────────────────────────────────────
        if opts['json']:
            self.stdout.write(json.dumps(report, indent=2, default=str, ensure_ascii=False))
        else:
            self._print_report(report, annee)

    def _print_report(self, report, annee):
        # Affichage ASCII pur — compatible Windows cp1252 sans caractères spéciaux
        W = self.style

        def _safe(s):
            """Remplace caractères non-ASCII pour console Windows."""
            return s.encode('ascii', errors='replace').decode('ascii')

        out = self.stdout.write

        out(W.MIGRATE_HEADING("\n=========== AUDIT INSTITUTION / ANNEE ===========\n"))

        # Institution
        out(W.HTTP_INFO("-- Institutions --"))
        inst = report['institutions']
        out(f"  Total institutions        : {inst['count_total']}")
        status = W.SUCCESS("OK") if inst['unique_principale'] else W.ERROR("BLOQUANT")
        out(f"  Avec est_principale=True  : {inst['count_principales']}  [{status}]")
        for i in inst['liste']:
            marker = "* " if i['est_principale'] else "  "
            label = _safe(f"{i['acronyme']} - {i['nom_fr'] or i['nom']} (code: {i['code_etablissement']})")
            out(f"    {marker}#{i['id']} {label}")

        # Orphelins
        out(W.HTTP_INFO("\n-- Orphelins institution --"))
        orph = report['orphelins_institution']
        for key, val in orph.items():
            if key.startswith('ids_'):
                continue
            status = W.SUCCESS("OK") if val == 0 else W.ERROR("!!")
            out(f"  {key:50s} : {val}  [{status}]")
        if orph['ids_filieres_orphelines']:
            out("  Filieres orphelines (id, code, intitule):")
            for f in orph['ids_filieres_orphelines']:
                out(_safe(f"    - {f}"))

        # Years
        out(W.HTTP_INFO("\n-- Years --"))
        for y in report['years']:
            marker = "* " if y['est_active'] else "  "
            c = " [cloturee]" if y['est_cloturee'] else ""
            out(f"  {marker}#{y['id']} {y['annee']}{c}")

        # Semestres
        out(W.HTTP_INFO("\n-- Semestres --"))
        sem = report['semestres']
        out(f"  Total                       : {sem['count_total']}")
        out(f"  Avec filiere                : {sem.get('avec_filiere', 'N/A')}")
        out(f"  Avec annee_univ             : {sem.get('avec_annee_univ', 'N/A')}")
        out(f"  Templates pures (null/null) : {sem.get('templates_pures', 'N/A')}")
        if sem['doublons_par_code_niveau_type']:
            out(W.WARNING("  !! Doublons detectes -- Section 1 doit consolider :"))
            for d in sem['doublons_par_code_niveau_type'][:10]:
                out(f"    - code={d['code_semestre']}, niveau_id={d['niveau_semestre_id']}, type={d['type_semestre']}: {d['c']} enregistrements")

        # Volumetrie
        scope = ('annee ' + annee.annee) if annee else 'toutes annees'
        out(W.HTTP_INFO(f"\n-- Comptes volumetriques ({scope}) --"))
        for key, val in report['comptes_volumetriques'].items():
            out(f"  {key:30s} : {val}")

        # Bloquants
        out(W.HTTP_INFO("\n-- Bloquants detectes --"))
        if not report['bloquants']:
            out(W.SUCCESS("  Aucun bloquant -- pret pour Section 1."))
        else:
            for b in report['bloquants']:
                out(W.ERROR(f"  !! {_safe(b)}"))

        out("")
