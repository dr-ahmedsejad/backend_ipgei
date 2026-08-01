"""Audit de coherence prof.type vs prof_type_history.

Detecte :
1. Profs sans aucune entree dans prof_type_history (invisibles en mode historique)
2. Profs dont le statut "actuel" en prof_type_history (date_fin IS NULL ou >= today)
   ne correspond pas a prof.type
3. Profs avec plusieurs periodes ouvertes (chevauchement)
4. Profs avec dates incoherentes (date_fin < date_debut)

Usage :
    python manage.py audit_prof_type_history
    python manage.py audit_prof_type_history --fix      # tente de corriger (cree entree manquante)
    python manage.py audit_prof_type_history --json     # sortie JSON pour cron
"""
import json as _json
from django.core.management.base import BaseCommand
from django.db.models import Count, Q
from django.utils import timezone

from apps.prof.models import Prof, ProfTypeHistory


class Command(BaseCommand):
    help = 'Audit de coherence entre prof.type et prof_type_history'

    def add_arguments(self, parser):
        parser.add_argument('--fix', action='store_true', help='Cree les entrees manquantes pour les profs sans history')
        parser.add_argument('--json', action='store_true', help='Sortie JSON (pour cron / monitoring)')

    def handle(self, *args, **opts):
        today = timezone.now().date()
        report = {
            'date':                  str(today),
            'profs_total':           Prof.objects.count(),
            'profs_sans_history':    [],
            'profs_type_desync':     [],
            'profs_periodes_ouvertes_multiples': [],
            'periodes_dates_incoherentes': [],
            'fixes_applied':         0,
        }

        # 1. Profs sans aucune entree dans prof_type_history
        sans_history = Prof.objects.exclude(
            id__in=ProfTypeHistory.objects.values_list('prof_id', flat=True)
        ).values_list('id', 'nom', 'type')
        report['profs_sans_history'] = [
            {'id': p[0], 'nom': p[1], 'type_actuel': p[2]} for p in sans_history
        ]

        # 2. Desync prof.type vs entree active
        for p in Prof.objects.all():
            current = ProfTypeHistory.objects.filter(prof_id=p.id).filter(
                Q(date_fin__isnull=True) | Q(date_fin__gte=today)
            ).filter(date_debut__lte=today).order_by('-date_debut').first()
            if current and current.type != p.type:
                report['profs_type_desync'].append({
                    'id':              p.id,
                    'nom':             p.nom,
                    'prof_type':       p.type,
                    'history_type':    current.type,
                    'history_periode': f'{current.date_debut} -> {current.date_fin or "en cours"}',
                })

        # 3. Plusieurs periodes ouvertes pour un meme prof
        ouvertes = (
            ProfTypeHistory.objects.filter(date_fin__isnull=True)
            .values('prof_id').annotate(n=Count('id')).filter(n__gt=1)
        )
        for o in ouvertes:
            entries = list(
                ProfTypeHistory.objects.filter(prof_id=o['prof_id'], date_fin__isnull=True)
                .values('id', 'type', 'date_debut')
            )
            report['profs_periodes_ouvertes_multiples'].append({
                'prof_id': o['prof_id'], 'n_ouvertes': o['n'], 'entries': entries,
            })

        # 4. Dates incoherentes (date_fin < date_debut) — comparison via F()
        from django.db.models import F as _F
        bad_dates = ProfTypeHistory.objects.filter(date_fin__lt=_F('date_debut'))
        for e in bad_dates:
            report['periodes_dates_incoherentes'].append({
                'id': e.id, 'prof_id': e.prof_id, 'type': e.type,
                'date_debut': str(e.date_debut), 'date_fin': str(e.date_fin),
            })

        # --fix : cree entrees manquantes
        if opts.get('fix') and report['profs_sans_history']:
            for p in report['profs_sans_history']:
                ProfTypeHistory.objects.create(
                    prof_id=p['id'], type=p['type_actuel'],
                    date_debut=today, date_fin=None,
                    motif='Backfill auto via audit_prof_type_history --fix',
                    cree_par='audit-fix',
                )
                report['fixes_applied'] += 1

        # Output
        if opts.get('json'):
            self.stdout.write(_json.dumps(report, indent=2, ensure_ascii=False))
            return

        self.stdout.write(self.style.HTTP_INFO(f"=== Audit prof_type_history — {today} ==="))
        self.stdout.write(f"Total profs : {report['profs_total']}")

        n_sans = len(report['profs_sans_history'])
        if n_sans:
            self.stdout.write(self.style.WARNING(f"\n[!] {n_sans} prof(s) sans entree history :"))
            for p in report['profs_sans_history']:
                self.stdout.write(f"  - #{p['id']} {p['nom']} (type actuel: {p['type_actuel']})")
        else:
            self.stdout.write(self.style.SUCCESS("[OK] Tous les profs ont au moins une entree history"))

        n_desync = len(report['profs_type_desync'])
        if n_desync:
            self.stdout.write(self.style.ERROR(f"\n[!] {n_desync} prof(s) en desync prof.type vs history :"))
            for p in report['profs_type_desync']:
                self.stdout.write(f"  - #{p['id']} {p['nom']} : prof.type={p['prof_type']} mais history dit {p['history_type']} ({p['history_periode']})")
        else:
            self.stdout.write(self.style.SUCCESS("[OK] Aucun desync prof.type vs history"))

        n_mult = len(report['profs_periodes_ouvertes_multiples'])
        if n_mult:
            self.stdout.write(self.style.ERROR(f"\n[!] {n_mult} prof(s) avec plusieurs periodes ouvertes :"))
            for p in report['profs_periodes_ouvertes_multiples']:
                self.stdout.write(f"  - prof #{p['prof_id']} : {p['n_ouvertes']} periodes ouvertes")
        else:
            self.stdout.write(self.style.SUCCESS("[OK] Aucun chevauchement (1 seule periode ouverte par prof)"))

        n_bad = len(report['periodes_dates_incoherentes'])
        if n_bad:
            self.stdout.write(self.style.ERROR(f"\n[!] {n_bad} periode(s) avec dates incoherentes (date_fin < date_debut) :"))
            for e in report['periodes_dates_incoherentes']:
                self.stdout.write(f"  - #{e['id']} prof={e['prof_id']} : {e['date_debut']} -> {e['date_fin']}")
        else:
            self.stdout.write(self.style.SUCCESS("[OK] Toutes les dates sont coherentes"))

        if report['fixes_applied']:
            self.stdout.write(self.style.SUCCESS(f"\n>>> {report['fixes_applied']} entree(s) creee(s) avec --fix"))
