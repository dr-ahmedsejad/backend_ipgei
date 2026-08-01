from django.db import migrations


def backfill_institution(apps, schema_editor):
    EM = apps.get_model('em', 'EM')
    Institution = apps.get_model('parametres', 'Institution')
    principale = Institution.objects.filter(est_principale=True).first()
    if not principale:
        principale = Institution.objects.first()
    if not principale:
        return
    EM.objects.filter(institution__isnull=True).update(institution=principale)


def reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('em', '0009_em_institution'),
    ]

    operations = [
        migrations.RunPython(backfill_institution, reverse),
    ]
