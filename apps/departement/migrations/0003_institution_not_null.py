"""
Section 1bis institution_V1 — Passer Departement.institution en NOT NULL + PROTECT.

Le backfill préalable (fix_orphelins_institution) a déjà rattaché tous les
Departement orphelins à l'institution principale. Cette migration fait juste
le ALTER FIELD.
"""
from django.db import migrations, models


def verify_no_null_institution(apps, schema_editor):
    Departement = apps.get_model('departement', 'Departement')
    # Base neuve (aucun departement) : rien a verifier.
    if not Departement.objects.exists():
        return
    nb_null = Departement.objects.filter(institution__isnull=True).count()
    if nb_null:
        raise RuntimeError(
            f"{nb_null} Departement(s) avec institution=NULL — "
            "exécuter d'abord `fix_orphelins_institution --apply`."
        )


def reverse_verify(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('departement', '0002_departement_filiere_departement_groupe_and_more'),
        ('parametres', '0006_semestre_simplification'),
    ]

    operations = [
        migrations.RunPython(verify_no_null_institution, reverse_verify),
        migrations.AlterField(
            model_name='departement',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='departements',
            ),
        ),
    ]
