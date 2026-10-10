# Index de consultation (10/10/2026, comme à l'ISS) : AJOUT d'index, aucune
# ligne touchée.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0001_initial'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='notification',
            index=models.Index(fields=['destinataire', 'lue'], name='notif_dest_lue_idx'),
        ),
        migrations.AddIndex(
            model_name='notification',
            index=models.Index(fields=['created_at'], name='notif_created_idx'),
        ),
    ]
