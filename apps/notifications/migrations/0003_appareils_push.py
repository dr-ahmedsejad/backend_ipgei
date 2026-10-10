# Notifications push (app Groupe Polytechnique) : deux tables NOUVELLES,
# aucune ligne existante touchée.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0002_index_consultations'),
    ]

    operations = [
        migrations.CreateModel(
            name='AppareilPush',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('user_id', models.BigIntegerField(db_index=True)),
                ('jeton', models.CharField(max_length=512, unique=True)),
                ('plateforme', models.CharField(default='android', max_length=20)),
                ('langue', models.CharField(default='fr', max_length=5)),
                ('projet', models.CharField(blank=True, default='gp', max_length=20)),
                ('cree_le', models.DateTimeField(auto_now_add=True)),
                ('vu_le', models.DateTimeField(auto_now=True)),
            ],
            options={'db_table': 'notifications_appareil'},
        ),
        migrations.CreateModel(
            name='PushEnvoye',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('notification_id', models.BigIntegerField()),
                ('notification_le', models.DateTimeField()),
                ('nb_appareils', models.PositiveIntegerField(default=0)),
                ('traite_le', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'notifications_push_envoye',
                'indexes': [models.Index(fields=['notification_le'], name='push_envoye_le_idx')],
                'constraints': [models.UniqueConstraint(fields=('notification_id', 'notification_le'),
                                                        name='notifications_push_envoye_unique')],
            },
        ),
    ]
