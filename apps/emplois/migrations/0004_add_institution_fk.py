"""
Section 1bis institution_V1 — Ajout FK institution sur Emplois et EmploisArchive.

3 étapes intégrées (MySQL InnoDB) :
  A) AddField nullable
  B) RunPython backfill (via departement.institution, fallback institution principale)
  C) AlterField NOT NULL

Si la table est volumineuse (>500k lignes), envisager pt-online-schema-change
pour la dernière étape.
"""
from django.db import migrations, models


def backfill_emplois_institution(apps, schema_editor):
    Institution = apps.get_model('parametres', 'Institution')
    Emplois = apps.get_model('emplois', 'Emplois')
    EmploisArchive = apps.get_model('emplois', 'EmploisArchive')

    # Base neuve (rien à backfiller) : ne pas exiger d'institution principale.
    if not Emplois.objects.exists() and not EmploisArchive.objects.exists():
        return

    principales = list(Institution.objects.filter(est_principale=True))
    if not principales:
        raise RuntimeError("Aucune institution principale — backfill impossible.")
    if len(principales) > 1:
        raise RuntimeError(
            f"{len(principales)} institutions principales — désambiguïser avant migration."
        )
    principale = principales[0]

    # Stratégie : utiliser departement.institution si dispo, sinon institution principale.
    for Model in (Emplois, EmploisArchive):
        # Backfill via departement.institution
        nb_via_dept = Model.objects.filter(
            institution__isnull=True,
            departement__institution__isnull=False,
        ).count()
        # Update en batch — séquence en deux passes
        for obj in Model.objects.filter(
            institution__isnull=True,
            departement__institution__isnull=False,
        ).select_related('departement').iterator(chunk_size=500):
            obj.institution_id = obj.departement.institution_id
            obj.save(update_fields=['institution'])

        # Fallback : institution principale pour ceux sans departement.institution
        nb_fallback = Model.objects.filter(institution__isnull=True).update(institution=principale)
        if nb_via_dept or nb_fallback:
            print(
                f"  -> {Model.__name__}: {nb_via_dept} via departement.institution, "
                f"{nb_fallback} via institution principale"
            )


def reverse_backfill(apps, schema_editor):
    """No-op : restaurer le dump mysqldump pour rollback complet."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('emplois', '0003_emplois_indexes'),
        ('parametres', '0006_semestre_simplification'),
    ]

    operations = [
        # A) AddField nullable
        migrations.AddField(
            model_name='emplois',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='emplois',
                null=True, blank=True,
            ),
        ),
        migrations.AddField(
            model_name='emploisarchive',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='emplois_archive',
                null=True, blank=True,
            ),
        ),
        # B) Backfill
        migrations.RunPython(backfill_emplois_institution, reverse_backfill),
        # C) NOT NULL
        migrations.AlterField(
            model_name='emplois',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='emplois',
            ),
        ),
        migrations.AlterField(
            model_name='emploisarchive',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='emplois_archive',
            ),
        ),
    ]
