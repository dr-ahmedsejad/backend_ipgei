"""
Section 1bis institution_V1 — FK institution sur :
- Preinscription
- InscriptionAdministrative
- Derogation
- Progression

Stratégie de backfill par modèle :
- Preinscription : via filiere.institution si dispo (filiere SET_NULL → peut être NULL),
  fallback institution principale
- InscriptionAdministrative : via filiere.institution (FK PROTECT toujours rempli)
- Derogation : via etudiant.departement.institution
- Progression : via filiere_source.institution (FK PROTECT)
"""
from django.db import migrations, models


def backfill_inscriptions_institution(apps, schema_editor):
    Institution = apps.get_model('parametres', 'Institution')
    Preinscription = apps.get_model('inscriptions', 'Preinscription')
    InscriptionAdministrative = apps.get_model('inscriptions', 'InscriptionAdministrative')
    Derogation = apps.get_model('inscriptions', 'Derogation')
    Progression = apps.get_model('inscriptions', 'Progression')

    # Base neuve (rien à backfiller) : ne pas exiger d'institution principale.
    if not (Preinscription.objects.exists() or InscriptionAdministrative.objects.exists()
            or Derogation.objects.exists() or Progression.objects.exists()):
        return

    principales = list(Institution.objects.filter(est_principale=True))
    if len(principales) != 1:
        raise RuntimeError(f"{len(principales)} institutions principales — corriger.")
    principale = principales[0]

    # Preinscription
    nb_via_f = 0
    for obj in Preinscription.objects.filter(
        institution__isnull=True, filiere__institution__isnull=False,
    ).select_related('filiere').iterator(chunk_size=200):
        obj.institution_id = obj.filiere.institution_id
        obj.save(update_fields=['institution'])
        nb_via_f += 1
    nb_p_fb = Preinscription.objects.filter(institution__isnull=True).update(institution=principale)
    print(f"  -> Preinscription: {nb_via_f} via filiere, {nb_p_fb} via principale")

    # InscriptionAdministrative
    nb_via_f = 0
    for obj in InscriptionAdministrative.objects.filter(
        institution__isnull=True, filiere__institution__isnull=False,
    ).select_related('filiere').iterator(chunk_size=200):
        obj.institution_id = obj.filiere.institution_id
        obj.save(update_fields=['institution'])
        nb_via_f += 1
    nb_ia_fb = InscriptionAdministrative.objects.filter(institution__isnull=True).update(institution=principale)
    print(f"  -> InscriptionAdministrative: {nb_via_f} via filiere, {nb_ia_fb} via principale")

    # Derogation
    nb_via_d = 0
    for obj in Derogation.objects.filter(
        institution__isnull=True, etudiant__departement__institution__isnull=False,
    ).select_related('etudiant__departement').iterator(chunk_size=200):
        obj.institution_id = obj.etudiant.departement.institution_id
        obj.save(update_fields=['institution'])
        nb_via_d += 1
    nb_d_fb = Derogation.objects.filter(institution__isnull=True).update(institution=principale)
    print(f"  -> Derogation: {nb_via_d} via etudiant.departement, {nb_d_fb} via principale")

    # Progression
    nb_via_fs = 0
    for obj in Progression.objects.filter(
        institution__isnull=True, filiere_source__institution__isnull=False,
    ).select_related('filiere_source').iterator(chunk_size=200):
        obj.institution_id = obj.filiere_source.institution_id
        obj.save(update_fields=['institution'])
        nb_via_fs += 1
    nb_pr_fb = Progression.objects.filter(institution__isnull=True).update(institution=principale)
    print(f"  -> Progression: {nb_via_fs} via filiere_source, {nb_pr_fb} via principale")


def reverse_backfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('inscriptions', '0010_short_derogation_labels'),
        ('parametres', '0006_semestre_simplification'),
    ]

    operations = [
        migrations.AddField(
            model_name='preinscription',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution', on_delete=models.deletion.PROTECT,
                related_name='preinscriptions', null=True, blank=True,
            ),
        ),
        migrations.AddField(
            model_name='inscriptionadministrative',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution', on_delete=models.deletion.PROTECT,
                related_name='inscriptions_admin', null=True, blank=True,
            ),
        ),
        migrations.AddField(
            model_name='derogation',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution', on_delete=models.deletion.PROTECT,
                related_name='derogations', null=True, blank=True,
            ),
        ),
        migrations.AddField(
            model_name='progression',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution', on_delete=models.deletion.PROTECT,
                related_name='progressions', null=True, blank=True,
            ),
        ),
        migrations.RunPython(backfill_inscriptions_institution, reverse_backfill),
        migrations.AlterField(
            model_name='preinscription',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution', on_delete=models.deletion.PROTECT,
                related_name='preinscriptions',
            ),
        ),
        migrations.AlterField(
            model_name='inscriptionadministrative',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution', on_delete=models.deletion.PROTECT,
                related_name='inscriptions_admin',
            ),
        ),
        migrations.AlterField(
            model_name='derogation',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution', on_delete=models.deletion.PROTECT,
                related_name='derogations',
            ),
        ),
        migrations.AlterField(
            model_name='progression',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution', on_delete=models.deletion.PROTECT,
                related_name='progressions',
            ),
        ),
    ]
