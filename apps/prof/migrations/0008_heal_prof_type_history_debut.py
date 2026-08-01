"""Guérison one-shot : rétrodater le 1er statut payé-à-l'heure d'un prof pour
couvrir son activité réelle antérieure (cas "prof créé / séances marquées en retard").

Symptôme corrigé : un prof créé en juin avec une vacation datée de mai voyait son
historique démarrer en juin → absent des états de mai. On recale le date_debut de son
PREMIER enregistrement (s'il est payé-à-l'heure) sur sa première activité si celle-ci
est antérieure.

Sûr :
- ne touche QUE le 1er enregistrement (pas de conflit avec une période ultérieure) ;
- ne recule jamais au-delà de la 1re activité réelle ;
- ignore les profs dont le 1er statut n'est pas payé-à-l'heure (permanent/contractuel),
  pour ne pas réécrire un historique de bascule.

Le filet runtime (`payes_a_lheure_ids_for_month`) rend déjà le calcul correct ; cette
migration aligne en plus les DONNÉES. Non réversible proprement : reverse = noop.
"""
from django.db import migrations
from django.db.models import Min

PAID = ('vacataire', 'personnel_admin', 'personnel_militaire')


def forwards(apps, schema_editor):
    ProfTypeHistory = apps.get_model('prof', 'ProfTypeHistory')
    Vacation = apps.get_model('vacation', 'Vacation')
    SuiviePointage = apps.get_model('suivi', 'SuiviePointage')

    # Première activité réelle par prof (vacation ou pointage "Fait")
    act = {}
    for pid, d in (Vacation.objects.filter(date__isnull=False)
                   .values('prof_id').annotate(m=Min('date'))
                   .values_list('prof_id', 'm')):
        if d is not None:
            act[pid] = d
    for pid, d in (SuiviePointage.objects.filter(commentaire='Fait', date_suivie__isnull=False)
                   .values('prof_id').annotate(m=Min('date_suivie'))
                   .values_list('prof_id', 'm')):
        if d is not None and (pid not in act or d < act[pid]):
            act[pid] = d

    # Base neuve : aucune activité → rien à guérir. Évite en plus de requêter
    # prof_type_history (table unmanaged, absente d'une base fraîchement migrée).
    if not act:
        return

    healed = 0
    prof_ids = set(ProfTypeHistory.objects.values_list('prof_id', flat=True).distinct())
    for pid in prof_ids:
        earliest_act = act.get(pid)
        if earliest_act is None:
            continue
        first = (ProfTypeHistory.objects.filter(prof_id=pid)
                 .order_by('date_debut').first())
        if first and first.type in PAID and first.date_debut > earliest_act:
            first.date_debut = earliest_act
            first.save(update_fields=['date_debut'])
            healed += 1
    print("  [prof_type_history] 1ers statuts rétrodatés sur la 1re activité : %s" % healed)


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('prof', '0007_prof_actif_alter_prof_banque'),
        ('vacation', '0004_alter_surveillance_departement'),
        ('suivi', '0013_alter_suivie_prof_alter_suiviepointage_prof'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
