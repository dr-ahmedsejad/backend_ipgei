"""
Heure du pointage (`SuiviePointage.pointe_le`) : distingue un vrai « Non fait »
d'une séance pas encore pointée (voir apps/suivi/statut_pointage.py).

Données existantes (aucune heure n'était gardée) :
  * « Fait » / « Reporté » : pointées, à la fin de leur journée ;
  * « Non fait » : pointée si sa grille (même année, même semaine, même
    département) contient au moins une séance « Fait » ou « Reporté » — la
    semaine a été traitée, ce « Non fait » est donc réel ; sinon elle reste
    « en attente » (pointe_le vide).

Retour arrière : la colonne est supprimée.
"""
from collections import defaultdict
from datetime import datetime, time

from django.db import migrations, models
from django.utils import timezone


def remplir(apps, schema_editor):
    SP = apps.get_model('suivi', 'SuiviePointage')
    Lien = apps.get_model('suivi', 'SuiviePointageDepartement')
    if Lien._meta.db_table not in schema_editor.connection.introspection.table_names():
        return  # base neuve sans table de liaison : rien d'ancien à reprendre

    depts = defaultdict(set)
    for sp_id, d_id in Lien.objects.values_list('suiviepointage_id', 'departement_id'):
        depts[sp_id].add(d_id)

    lignes = list(SP.objects.filter(pointe_le__isnull=True)
                  .values_list('pk', 'annee_universitaire', 'numero_semaine', 'commentaire', 'date_suivie'))

    # Grilles pointées : (année, semaine, département) avec un « Fait » / « Reporté ».
    # Leur fin = la dernière date de la grille.
    fin_grille = {}
    for pk, annee, sem, com, date in lignes:
        if com in ('Fait', 'Reporté') and date:
            for d in depts[pk]:
                cle = (annee, sem, d)
                fin_grille[cle] = max(fin_grille.get(cle, date), date)

    def fin_journee(d):
        return timezone.make_aware(datetime.combine(d, time(23, 59, 59)))

    a_maj = []
    for pk, annee, sem, com, date in lignes:
        quand = None
        if com in ('Fait', 'Reporté'):
            quand = fin_journee(date) if date else timezone.now()
        else:
            fins = [fin_grille[(annee, sem, d)] for d in depts[pk] if (annee, sem, d) in fin_grille]
            if fins:
                quand = fin_journee(max(fins))
        if quand:
            a_maj.append(SP(pk=pk, pointe_le=quand))
    SP.objects.bulk_update(a_maj, ['pointe_le'], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('suivi', '0016_index_consultations'),
    ]

    operations = [
        migrations.AddField(
            model_name='suiviepointage',
            name='pointe_le',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(remplir, migrations.RunPython.noop),
    ]
