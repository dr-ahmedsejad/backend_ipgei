"""
Ventile le volume horaire d'une matiere en CM / TD / TP.

Le calcul de vacation applique un tarif par TYPE d'heure : un volume global ne
permettait pas de le nourrir, et le miroir vers `em.EM` devait tout reporter sur
`CM` faute de mieux. Chaque matiere porte desormais ses trois volumes.

`volume_horaire` disparait de la base et devient une propriete calculee
(somme des trois) : conserver un total stocke a cote de son detail garantirait
une divergence tot ou tard.

L'ordre des operations compte : on AJOUTE les colonnes, on REPORTE l'existant
sur `volume_cm`, puis seulement on supprime l'ancienne colonne — sinon les
volumes deja saisis seraient perdus.
"""
from django.db import migrations, models


def reporter_volumes(apps, schema_editor):
    """Reporte l'ancien volume global sur les heures de cours magistral."""
    Matiere = apps.get_model('ipgei', 'Matiere')
    Matiere.objects.update(volume_cm=models.F('volume_horaire'))


def restaurer_volumes(apps, schema_editor):
    Matiere = apps.get_model('ipgei', 'Matiere')
    Matiere.objects.update(volume_horaire=models.F('volume_cm'))


class Migration(migrations.Migration):

    dependencies = [
        ('ipgei', '0003_classe_departement_miroir_matiere_em_miroir'),
    ]

    operations = [
        migrations.AddField(
            model_name='matiere',
            name='volume_cm',
            field=models.PositiveSmallIntegerField(
                default=0, help_text='Heures de cours magistral sur le semestre.'),
        ),
        migrations.AddField(
            model_name='matiere',
            name='volume_td',
            field=models.PositiveSmallIntegerField(
                default=0, help_text='Heures de travaux dirigés sur le semestre.'),
        ),
        migrations.AddField(
            model_name='matiere',
            name='volume_tp',
            field=models.PositiveSmallIntegerField(
                default=0, help_text='Heures de travaux pratiques sur le semestre.'),
        ),
        migrations.RunPython(reporter_volumes, restaurer_volumes),
        migrations.RemoveField(
            model_name='matiere',
            name='volume_horaire',
        ),
    ]
