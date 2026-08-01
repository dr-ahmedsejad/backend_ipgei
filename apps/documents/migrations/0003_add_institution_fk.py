"""
Section 1bis institution_V1 — FK institution sur DocumentOfficiel et RegistreDiplome.

RegistreDiplome est immuable (delete()/save() après création interdits) — backfill via
le manager `_default_manager` qui contourne la garde dans les migrations.
"""
from django.db import migrations, models


def backfill_documents_institution(apps, schema_editor):
    Institution = apps.get_model('parametres', 'Institution')
    DocumentOfficiel = apps.get_model('documents', 'DocumentOfficiel')
    RegistreDiplome = apps.get_model('documents', 'RegistreDiplome')

    # Base neuve (rien à backfiller) : ne pas exiger d'institution principale.
    if not DocumentOfficiel.objects.exists() and not RegistreDiplome.objects.exists():
        return

    principales = list(Institution.objects.filter(est_principale=True))
    if len(principales) != 1:
        raise RuntimeError(f"{len(principales)} institutions principales — corriger.")
    principale = principales[0]

    # DocumentOfficiel : via etudiant.departement.institution si dispo
    nb_via_dept = 0
    for obj in DocumentOfficiel.objects.filter(
        institution__isnull=True,
        etudiant__departement__institution__isnull=False,
    ).select_related('etudiant__departement').iterator(chunk_size=500):
        obj.institution_id = obj.etudiant.departement.institution_id
        obj.save(update_fields=['institution'])
        nb_via_dept += 1
    nb_fallback = DocumentOfficiel.objects.filter(institution__isnull=True).update(institution=principale)
    print(f"  -> DocumentOfficiel: {nb_via_dept} via etudiant.departement, {nb_fallback} via principale")

    # RegistreDiplome : via filiere.institution (PROTECT FK)
    nb_via_filiere = 0
    for obj in RegistreDiplome.objects.filter(
        institution__isnull=True,
        filiere__institution__isnull=False,
    ).select_related('filiere').iterator(chunk_size=500):
        obj.institution_id = obj.filiere.institution_id
        # save() lève PermissionError sur RegistreDiplome — utiliser update direct
        RegistreDiplome.objects.filter(pk=obj.pk).update(institution=obj.institution_id)
        nb_via_filiere += 1
    nb_reg_fallback = RegistreDiplome.objects.filter(institution__isnull=True).update(institution=principale)
    print(f"  -> RegistreDiplome: {nb_via_filiere} via filiere.institution, {nb_reg_fallback} via principale")


def reverse_backfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('documents', '0002_numeroserieconfig'),
        ('parametres', '0006_semestre_simplification'),
    ]

    operations = [
        migrations.AddField(
            model_name='documentofficiel',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='documents_officiels', null=True, blank=True,
            ),
        ),
        migrations.AddField(
            model_name='registrediplome',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='registres_diplomes', null=True, blank=True,
            ),
        ),
        migrations.RunPython(backfill_documents_institution, reverse_backfill),
        migrations.AlterField(
            model_name='documentofficiel',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='documents_officiels',
            ),
        ),
        migrations.AlterField(
            model_name='registrediplome',
            name='institution',
            field=models.ForeignKey(
                to='parametres.institution',
                on_delete=models.deletion.PROTECT,
                related_name='registres_diplomes',
            ),
        ),
    ]
