"""
Reprise des semaines générées AVANT l'introduction de `suivi_genere_le`.

Le champ a été ajouté par la migration 0009, mais rien n'a repris l'existant :
une semaine générée auparavant restait sans horodatage, donc affichée
« Prévisionnel — suivi non généré » alors que ses 90 lignes de suivi étaient
bien là. Le bandeau annonçait exactement le contraire de la réalité.

La date n'est pas inventée : elle est lue dans le journal d'audit, qui
enregistre chaque génération (`SuiviGeneration`) avec son horodatage, le numéro
de semaine et le nombre de lignes créées. Ce dernier sert à lever l'ambiguïté
quand un même numéro de semaine existe sur plusieurs années.

À défaut d'entrée exploitable, la semaine est laissée telle quelle : mieux vaut
un « prévisionnel » visiblement faux, que l'utilisateur peut corriger en
régénérant, qu'une date inventée qui masquerait une divergence réelle.
"""
import re

from django.db import migrations


def reprendre(apps, schema_editor):
    SemaineIPGEI = apps.get_model('ipgei', 'SemaineIPGEI')
    Suivie       = apps.get_model('suivi', 'Suivie')
    AuditLog     = apps.get_model('core', 'AuditLog')

    # Générations connues : (numéro de semaine, nb de lignes) -> horodatage.
    generations = []
    for entree in AuditLog.objects.filter(model_name='SuiviGeneration').order_by('timestamp'):
        message = str((entree.changes or {}).get('message', ''))
        trouve  = re.search(r'semaine (\d+)', message)
        if trouve:
            generations.append((
                int(trouve.group(1)),
                (entree.changes or {}).get('suivies_created'),
                entree.timestamp,
            ))

    reprises = 0
    for semaine in SemaineIPGEI.objects.filter(suivi_genere_le__isnull=True).select_related('semestre'):
        if semaine.numero is None:
            continue
        # Le type de semestre est dérivé du code : S1/S3 impairs, S2/S4 pairs.
        type_semestre = 'I' if semaine.semestre.code in ('S1', 'S3') else 'P'
        lignes = Suivie.objects.filter(
            annee_universitaire=semaine.semestre.annee_universitaire,
            type_semestre=type_semestre,
            numero_semaine=semaine.numero,
        ).count()
        if not lignes:
            continue                      # réellement prévisionnelle

        candidates = [g for g in generations if g[0] == semaine.numero]
        exactes    = [g for g in candidates if g[1] == lignes]
        retenue    = (exactes or candidates)
        if not retenue:
            continue

        semaine.suivi_genere_le = retenue[-1][2]     # la plus récente
        semaine.save(update_fields=['suivi_genere_le'])
        reprises += 1

    if reprises:
        print(f'  {reprises} semaine(s) datée(s) depuis le journal d\'audit.')


class Migration(migrations.Migration):

    dependencies = [
        ('ipgei', '0010_unicite_case_classe_entiere'),
        ('core', '0001_initial'),
        ('suivi', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(reprendre, migrations.RunPython.noop),
    ]
