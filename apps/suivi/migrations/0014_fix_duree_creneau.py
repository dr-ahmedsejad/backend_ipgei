"""Fiabilisation de duree_creneau (erreur de saisie : 2,0 h sur des créneaux de 1,5 h).

Le nouveau modèle de calcul (paie / stat vacations) se base sur le duree_creneau STOCKÉ
par ligne. Or des pointages ont un duree_creneau erroné (ex. 2,0 h saisi par défaut sur
le créneau 08h00-09h30 qui dure réellement 1,5 h) — ce qui sur-paie ces séances.

Cette migration aligne duree_creneau sur la durée réelle du créneau (`Creneau.duree`)
partout où ils diffèrent (et où un créneau est rattaché). Constaté sur le dump prod du
12/06/2026 : 57 lignes, toutes du pattern 2,0 → 1,5 (créneaux 08h00-09h30 / 09h45-11h15
/ 11h30-13h00). Effet : Novembre 101 000 → 96 000 (= valeur correcte).

Idempotente. Non réversible proprement (durées erronées non conservées) : reverse = noop.
"""
from django.db import migrations

BATCH = 500


def forwards(apps, schema_editor):
    SuiviePointage = apps.get_model('suivi', 'SuiviePointage')
    Creneau = apps.get_model('parametres', 'Creneau')

    duree_map = {c.id: c.duree for c in Creneau.objects.all()}
    buf = []
    fixed = 0

    def flush():
        if buf:
            SuiviePointage.objects.bulk_update(buf, ['duree_creneau'])
            buf.clear()

    qs = SuiviePointage.objects.exclude(creneau_fk_id__isnull=True).only(
        'id', 'duree_creneau', 'creneau_fk_id')
    for s in qs.iterator():
        nominal = duree_map.get(s.creneau_fk_id)
        if nominal is None:
            continue
        if s.duree_creneau is None or abs(float(s.duree_creneau) - float(nominal)) > 0.001:
            s.duree_creneau = nominal
            buf.append(s)
            fixed += 1
            if len(buf) >= BATCH:
                flush()
    flush()
    print("  [duree_creneau] lignes recalées sur la durée nominale du créneau : %s" % fixed)


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('suivi', '0013_alter_suivie_prof_alter_suiviepointage_prof'),
        ('parametres', '0012_alter_semestre_niveau_semestre'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
