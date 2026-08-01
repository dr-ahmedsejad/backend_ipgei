"""
Section 5 institution_V1 — Helper d'import en masse de notes historiques.

Boucle sur un dossier organisé par session/EM :

    <dossier_racine>/
        SN-I-2023-2024/
            INFO101.xlsx       # notes pour EM code_em='INFO101'
            MATH102.xlsx
            ...
        SN-P-2023-2024/
            ...

Chaque fichier Excel doit avoir colonnes : matricule, note_cc, note_tp, note_exam.

Pour chaque session ouverte :
1. Ouvre la session (est_close=False)
2. Boucle sur les fichiers Excel par EM
3. Réutilise NoteViewSet.importer logiquement (mais en process direct, pas HTTP)
4. Ferme la session
5. Calcule éléments → modules → semestres

Usage :
    python manage.py importer_notes_historiques --dossier "C:/imports/2023-2024" --annee 2023-2024
    python manage.py importer_notes_historiques --dossier "C:/imports/2023-2024" --annee 2023-2024 --skip-calcul
    python manage.py importer_notes_historiques --dossier "C:/imports/2023-2024" --annee 2023-2024 --dry-run
"""
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Import en masse de notes historiques par session/EM (helper)."

    def add_arguments(self, parser):
        parser.add_argument('--dossier', type=str, required=True,
                            help="Dossier racine contenant les sous-dossiers <session_code>/<em_code>.xlsx")
        parser.add_argument('--annee', type=str, required=True,
                            help="Année universitaire ciblée (ex: '2023-2024')")
        parser.add_argument('--institution', type=int, default=None)
        parser.add_argument('--skip-calcul', action='store_true', default=False,
                            help="Saute le calcul (éléments/modules/semestres) après chaque session.")
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help="Simule sans modifier la base.")

    @transaction.atomic
    def handle(self, *args, **opts):
        from apps.parametres.models import Year, Institution
        from apps.evaluations.models import SessionEvaluation
        from apps.em.models import EM

        dossier_racine = Path(opts['dossier'])
        if not dossier_racine.is_dir():
            raise CommandError(f"Dossier introuvable : {dossier_racine}")

        try:
            annee = Year.objects.get(annee=opts['annee'])
        except Year.DoesNotExist:
            raise CommandError(f"Year '{opts['annee']}' introuvable.")

        if opts['institution']:
            try:
                inst = Institution.objects.get(pk=opts['institution'])
            except Institution.DoesNotExist:
                raise CommandError(f"Institution #{opts['institution']} introuvable.")
        else:
            principales = list(Institution.objects.filter(est_principale=True))
            if len(principales) != 1:
                raise CommandError(f"{len(principales)} institutions principales — utiliser --institution.")
            inst = principales[0]

        # Sessions de cette année pour cette institution
        sessions = {
            s.code: s for s in SessionEvaluation.objects.filter(annee_univ=annee, institution=inst)
        }
        if not sessions:
            raise CommandError(
                f"Aucune SessionEvaluation pour {annee.annee} institution #{inst.id}. "
                "Lancer d'abord `inserer_annee_historique`."
            )

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== Import notes historiques {annee.annee} institution #{inst.id} ==="
        ))

        # EM index par code_em
        em_par_code = {em.code_em: em for em in EM.objects.all()}

        total_imported = 0
        total_errors = 0

        for session_dir in sorted(dossier_racine.iterdir()):
            if not session_dir.is_dir():
                continue

            session_code = session_dir.name
            session = sessions.get(session_code)
            if not session:
                self.stdout.write(self.style.WARNING(
                    f"  [SKIP] Aucune session avec code='{session_code}'"
                ))
                continue

            self.stdout.write(f"\n  Session : {session_code} (id={session.id})")

            # Ouvrir la session
            if not opts['dry_run']:
                session.est_close = False
                session.est_ouverte = True
                session.save(update_fields=['est_close', 'est_ouverte'])

            session_imported = 0
            for xlsx in sorted(session_dir.glob('*.xlsx')):
                em_code = xlsx.stem
                em = em_par_code.get(em_code)
                if not em:
                    self.stdout.write(self.style.WARNING(
                        f"    [SKIP] EM code='{em_code}' introuvable ({xlsx.name})"
                    ))
                    continue

                if opts['dry_run']:
                    self.stdout.write(f"    [DRY] {xlsx.name} -> EM #{em.id}")
                    continue

                nb = self._import_one_excel(xlsx, session=session, em=em)
                session_imported += nb
                self.stdout.write(f"    {xlsx.name} : {nb} notes importees")

            total_imported += session_imported

            # Refermer + calculer
            if not opts['dry_run']:
                if not opts['skip_calcul']:
                    self.stdout.write(f"    Calcul elements/modules/semestres...")
                    try:
                        from apps.evaluations.services.calcul_notes import NoteCalculService
                        svc = NoteCalculService(session)
                        svc.calculer_tous_elements_session()
                        svc.calculer_tous_modules_session()
                        svc.calculer_tous_semestres_session()
                        self.stdout.write(self.style.SUCCESS(f"    OK calcul session"))
                    except Exception as exc:
                        self.stdout.write(self.style.ERROR(f"    ERR calcul : {exc}"))
                        total_errors += 1
                session.est_close = True
                session.est_ouverte = False
                session.save(update_fields=['est_close', 'est_ouverte'])

        self.stdout.write(self.style.SUCCESS(
            f"\nTOTAL : {total_imported} notes importees, {total_errors} erreurs."
        ))

        if opts['dry_run']:
            transaction.set_rollback(True)

    def _import_one_excel(self, xlsx_path, session, em):
        """Réimplémente la logique de NoteViewSet.importer en process direct."""
        import openpyxl
        from decimal import Decimal, InvalidOperation
        from apps.inscriptions.models import InscriptionElement
        from apps.evaluations.models import Note

        wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
        ws = wb.active

        first_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if not first_row:
            return 0
        headers = {str(c).strip().lower(): i for i, c in enumerate(first_row) if c is not None}
        if 'matricule' not in headers:
            return 0

        ie_index = {
            ie.inscription_ped.inscription_admin.etudiant.matricule: ie
            for ie in InscriptionElement.objects.filter(em=em).select_related(
                'inscription_ped__inscription_admin__etudiant'
            )
        }

        col_map = {'CC': 'note_cc', 'TP': 'note_tp', 'EXAM': 'note_exam'}
        m_idx = headers['matricule']
        nb = 0

        for row in ws.iter_rows(min_row=2, values_only=True):
            if m_idx >= len(row) or not row[m_idx]:
                continue
            raw = row[m_idx]
            # Excel stocke souvent les matricules comme float : 22640 -> 22640.0
            if isinstance(raw, float) and raw.is_integer():
                matricule = str(int(raw))
            else:
                matricule = str(raw).strip()
                if matricule.endswith('.0') and matricule[:-2].isdigit():
                    matricule = matricule[:-2]
            ie = ie_index.get(matricule)
            if not ie:
                continue

            for type_n, col in col_map.items():
                idx = headers.get(col)
                if idx is None or idx >= len(row) or row[idx] is None:
                    continue
                try:
                    val = Decimal(str(row[idx]).replace(',', '.'))
                except InvalidOperation:
                    continue
                Note.objects.update_or_create(
                    inscription_element=ie, session=session, type_note=type_n,
                    defaults={'valeur': val},
                )
                nb += 1
        return nb
