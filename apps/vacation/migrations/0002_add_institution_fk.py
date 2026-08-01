"""
Section 1bis institution_V1 — FK institution sur Surveillance et Vacation.
"""
from django.db import migrations, models


def backfill_vacation_institution(apps, schema_editor):
    Institution = apps.get_model('parametres', 'Institution')
    Surveillance = apps.get_model('vacation', 'Surveillance')
    Vacation = apps.get_model('vacation', 'Vacation')

    # Base neuve (rien à backfiller) : ne pas exiger d'institution principale.
    if not Surveillance.objects.exists() and not Vacation.objects.exists():
        return

    principales = list(Institution.objects.filter(est_principale=True))
    if len(principales) != 1:
        raise RuntimeError(f"{len(principales)} institutions principales — corriger.")
    principale = principales[0]

    # Surveillance : via departement.institution (FK CASCADE → toujours rempli)
    nb_via_dept = 0
    for obj in Surveillance.objects.filter(
        institution__isnull=True,
        departement__institution__isnull=False,
    ).select_related('departement').iterator(chunk_size=500):
        obj.institution_id = obj.departement.institution_id
        obj.save(update_fields=['institution'])
        nb_via_dept += 1
    nb_fallback = Surveillance.objects.filter(institution__isnull=True).update(institution=principale)
    print(f"  -> Surveillance: {nb_via_dept} via departement, {nb_fallback} via principale")

    # Vacation : ManyToMany departements → fallback direct sur principale
    nb = Vacation.objects.filter(institution__isnull=True).update(institution=principale)
    print(f"  -> Vacation: {nb} via principale")


def reverse_backfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('vacation', '0001_initial'),
        ('parametres', '0006_semestre_simplification'),
    ]

    operations = [
        migrations.AddField(
            model_name='surveillance',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='surveillances', null=True, blank=True,
            ),
        ),
        migrations.AddField(
            model_name='vacation',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='vacations', null=True, blank=True,
            ),
        ),
        migrations.RunPython(backfill_vacation_institution, reverse_backfill),
        migrations.AlterField(
            model_name='surveillance',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='surveillances',
            ),
        ),
        migrations.AlterField(
            model_name='vacation',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='vacations',
            ),
        ),
    ]
