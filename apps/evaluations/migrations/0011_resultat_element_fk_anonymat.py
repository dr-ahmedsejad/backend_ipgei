"""
Migration 0011 — Deux changements :
1. ResultatElement.inscription_element : OneToOneField → ForeignKey
   + unique_together (inscription_element, session)
   Permet de conserver une ligne par session (normale + rattrapage) — Art. 18.

2. Nouveau modèle AnonymatSession pour l'anonymat par (étudiant × session).
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('evaluations', '0010_date_deliberation_nullable'),
        ('inscriptions', '__first__'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 1 — Changer OneToOneField → ForeignKey sur ResultatElement
        migrations.AlterField(
            model_name='resultatelement',
            name='inscription_element',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='resultats',
                to='inscriptions.inscriptionelement',
            ),
        ),
        migrations.AlterUniqueTogether(
            name='resultatelement',
            unique_together={('inscription_element', 'session')},
        ),

        # 2 — Nouveau modèle AnonymatSession
        migrations.CreateModel(
            name='AnonymatSession',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False)),
                ('numero_anonymat', models.PositiveIntegerField()),
                ('genere_le', models.DateTimeField(auto_now_add=True)),
                ('genere_par', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='anonymats_generes',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('inscription_admin', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='anonymats',
                    to='inscriptions.inscriptionadministrative',
                )),
                ('session', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='anonymats',
                    to='evaluations.sessionevaluation',
                )),
            ],
            options={
                'db_table': 'evaluations_anonymat_session',
            },
        ),
        migrations.AlterUniqueTogether(
            name='anonymatsession',
            unique_together={
                ('session', 'inscription_admin'),
                ('session', 'numero_anonymat'),
            },
        ),
    ]
