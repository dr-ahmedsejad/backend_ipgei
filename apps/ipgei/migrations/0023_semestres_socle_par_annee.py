"""
Les semestres du socle sont nommés par ANNÉE d'étude, plus par niveau.

« S3 — MP » était exact tant que MP était le seul niveau de deuxième année.
Depuis que les niveaux s'administrent, S3 est suivi par MP comme par MPI : le
semestre relève d'un rang, pas d'un niveau. Seul l'intitulé change ici —
`niveau_semestre` reste la jointure technique dont le socle se sert pour
retrouver les départements.
"""
from django.db import migrations


ANNEE_PAR_CODE = {
    'S1': '1re année', 'S2': '1re année',
    'S3': '2e année',  'S4': '2e année',
}


def nommer_par_annee(apps, schema_editor):
    Semestre = apps.get_model('parametres', 'Semestre')
    for semestre in Semestre.objects.filter(code_semestre__in=ANNEE_PAR_CODE):
        attendu = f'{semestre.code_semestre} — {ANNEE_PAR_CODE[semestre.code_semestre]}'
        if semestre.semestre != attendu:
            semestre.semestre = attendu
            semestre.save(update_fields=['semestre'])


def nommer_par_niveau(apps, schema_editor):
    """Retour à l'intitulé d'origine, tiré du niveau rattaché."""
    Semestre = apps.get_model('parametres', 'Semestre')
    for semestre in Semestre.objects.filter(
        code_semestre__in=ANNEE_PAR_CODE,
    ).select_related('niveau_semestre'):
        semestre.semestre = f'{semestre.code_semestre} — {semestre.niveau_semestre.niveau}'
        semestre.save(update_fields=['semestre'])


class Migration(migrations.Migration):

    dependencies = [
        ('ipgei', '0022_alter_deliberation_niveau'),
        ('parametres', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(nommer_par_annee, nommer_par_niveau),
    ]
