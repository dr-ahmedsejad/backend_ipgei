"""
Dédoublonne les DocumentOfficiel : au plus UN par
(étudiant, type_document, année, semestre).

RÈGLE DE SÛRETÉ — on ne casse jamais un QR en circulation :
  • Un document DÉLIVRÉ (premiere_generation renseignée) n'est JAMAIS supprimé
    (son QR a pu être imprimé/remis). Les groupes ayant plusieurs délivrés sont
    seulement SIGNALÉS pour revue manuelle.
  • On ne supprime que les doublons NON délivrés (premiere_generation = NULL).
  • Le « gardé » = le document délivré le plus récent s'il en existe, sinon le
    plus récent (id max) — cohérent avec la logique de réutilisation
    (order_by('-id').first()).

Usage :
    python manage.py dedupe_documents_officiels --dry-run
    python manage.py dedupe_documents_officiels --apply
    python manage.py dedupe_documents_officiels --type releve_semestre --apply
"""
from collections import defaultdict

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Dédoublonne les DocumentOfficiel (garde 1 par étudiant/type/année/semestre)."

    def add_arguments(self, parser):
        parser.add_argument('--type', default=None,
                            help="Limiter à un type_document (ex: releve_semestre).")
        parser.add_argument('--dry-run', action='store_true', default=False)
        parser.add_argument('--apply', action='store_true', default=False)
        parser.add_argument('--verbose', action='store_true', default=False,
                            help="Détailler chaque suppression.")

    def handle(self, *args, **opts):
        from apps.documents.models import DocumentOfficiel

        if not opts['apply'] and not opts['dry_run']:
            opts['dry_run'] = True
        mode = 'DRY-RUN' if opts['dry_run'] else 'APPLY'

        qs = DocumentOfficiel.objects.all()
        if opts['type']:
            qs = qs.filter(type_document=opts['type'])

        groups = defaultdict(list)
        for d in qs.order_by('id'):
            key = (d.etudiant_id, d.type_document, d.annee_universitaire, d.semestre_id)
            groups[key].append(d)

        n_del = 0
        n_groups_dup = 0
        protected = 0           # doublons délivrés conservés
        review_groups = []      # groupes avec >1 délivré → revue manuelle

        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== DEDUPE DOCUMENTS OFFICIELS [{mode}] ==="))

        for key, docs in groups.items():
            if len(docs) < 2:
                continue
            n_groups_dup += 1
            delivered = [d for d in docs if d.premiere_generation is not None]
            undelivered = [d for d in docs if d.premiere_generation is None]

            keeper = (max(delivered, key=lambda d: d.id) if delivered
                      else max(docs, key=lambda d: d.id))

            # Doublons délivrés NON gardés → jamais supprimés (QR en circulation).
            other_delivered = [d for d in delivered if d.id != keeper.id]
            if other_delivered:
                protected += len(other_delivered)
                review_groups.append((key, len(docs), len(delivered)))

            to_delete = [d for d in undelivered if d.id != keeper.id]
            if to_delete and opts['verbose']:
                etu = docs[0].etudiant
                self.stdout.write(
                    f"  {etu.matricule} / {key[1]} / {key[2]} / sem={key[3]} : "
                    f"garde {keeper.numero_serie} (délivré={keeper.premiere_generation is not None}), "
                    f"supprime {len(to_delete)}"
                )
            for d in to_delete:
                if opts['verbose']:
                    self.stdout.write(f"      - {d.numero_serie} (créé {d.date_generation:%Y-%m-%d}, non délivré)")
                if opts['apply']:
                    if d.fichier_pdf:
                        try:
                            d.fichier_pdf.delete(save=False)
                        except Exception as exc:
                            self.stdout.write(self.style.WARNING(f"        (fichier: {exc})"))
                    d.delete()
                n_del += 1

        self.stdout.write("")
        self.stdout.write(f"  Groupes avec doublons          : {n_groups_dup}")
        self.stdout.write(f"  Doublons NON délivrés à purger : {n_del}")
        if protected:
            self.stdout.write(self.style.WARNING(
                f"  Doublons DÉLIVRÉS conservés     : {protected} "
                f"(dans {len(review_groups)} groupe(s) — QR en circulation, non supprimés)"))

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING("\n[DRY-RUN] Rien supprimé. Relancer avec --apply."))
        else:
            self.stdout.write(self.style.SUCCESS(f"\n{n_del} doublon(s) non délivré(s) supprimé(s)."))
