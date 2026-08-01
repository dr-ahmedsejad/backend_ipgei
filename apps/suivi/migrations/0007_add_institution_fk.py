"""
Section 1bis institution_V1 — FK institution sur Suivie et SuiviePointage.

ChargeInstitution a déjà une FK institution (CASCADE), inchangée.
"""
from django.db import migrations, models


def backfill_suivi_institution(apps, schema_editor):
    Institution = apps.get_model('parametres', 'Institution')
    Suivie = apps.get_model('suivi', 'Suivie')
    SuiviePointage = apps.get_model('suivi', 'SuiviePointage')

    # Base neuve (rien à backfiller) : ne pas exiger d'institution principale.
    if not Suivie.objects.exists() and not SuiviePointage.objects.exists():
        return

    principales = list(Institution.objects.filter(est_principale=True))
    if len(principales) != 1:
        raise RuntimeError(f"{len(principales)} institutions principales — corriger.")
    principale = principales[0]

    # Suivie : via departement.institution si dispo
    nb_via_dept = 0
    for obj in Suivie.objects.filter(
        institution__isnull=True,
        departement__institution__isnull=False,
    ).select_related('departement').iterator(chunk_size=500):
        obj.institution_id = obj.departement.institution_id
        obj.save(update_fields=['institution'])
        nb_via_dept += 1
    nb_fallback = Suivie.objects.filter(institution__isnull=True).update(institution=principale)
    print(f"  -> Suivie: {nb_via_dept} via departement, {nb_fallback} via principale")

    # SuiviePointage : pas de FK departement (CharField id_departement) → fallback direct
    nb_p = SuiviePointage.objects.filter(institution__isnull=True).update(institution=principale)
    print(f"  -> SuiviePointage: {nb_p} via principale")


def reverse_backfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('suivi', '0006_reclamation_fields'),
        ('parametres', '0006_semestre_simplification'),
    ]

    operations = [
        migrations.AddField(
            model_name='suivie',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='suivies', null=True, blank=True,
            ),
        ),
        migrations.AddField(
            model_name='suiviepointage',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='suivies_pointage', null=True, blank=True,
            ),
        ),
        migrations.RunPython(backfill_suivi_institution, reverse_backfill),
        migrations.AlterField(
            model_name='suivie',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='suivies',
            ),
        ),
        migrations.AlterField(
            model_name='suiviepointage',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='suivies_pointage',
            ),
        ),
    ]
