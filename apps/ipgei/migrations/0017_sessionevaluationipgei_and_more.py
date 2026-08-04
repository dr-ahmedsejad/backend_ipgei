"""
Fenêtres de saisie des notes : une session normale et une session de rattrapage
par semestre, là où `Semestre.est_cloture` verrouillait les deux d'un bloc.

Le peuplement reconduit l'état actuel — saisie normale ouverte sur les semestres
ouverts, rattrapage en attente d'ouverture — pour qu'aucune campagne en cours ne
se ferme du fait de cette migration.
"""
from decimal import Decimal
from django.conf import settings
import django.core.validators
from django.db import migrations, models
import django.db.models.deletion
from django.utils import timezone


def peupler_sessions(apps, schema_editor):
    Session   = apps.get_model('ipgei', 'SessionEvaluationIPGEI')
    Semestre  = apps.get_model('ipgei', 'SemestreIPGEI')
    Parametres = apps.get_model('ipgei', 'ParametresIPGEI')

    maintenant = timezone.now()
    for semestre in Semestre.objects.all():
        parametres = Parametres.objects.filter(institution_id=semestre.institution_id).first()
        plafond = parametres.plafond_rattrapage if parametres else None

        Session.objects.get_or_create(
            semestre=semestre, type_session='normale',
            defaults={
                'est_ouverte': not semestre.est_cloture,
                'est_close':   semestre.est_cloture,
                'ouverte_le':  None if semestre.est_cloture else maintenant,
            },
        )
        # Le rattrapage attend une décision explicite de la scolarité, sauf sur
        # un semestre déjà clôturé — sa campagne est terminée par définition.
        Session.objects.get_or_create(
            semestre=semestre, type_session='rattrapage',
            defaults={
                'est_ouverte': False,
                'est_close':   semestre.est_cloture,
                'plafond_rattrapage': plafond,
            },
        )


def depeupler_sessions(apps, schema_editor):
    apps.get_model('ipgei', 'SessionEvaluationIPGEI').objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('ipgei', '0016_frais_inscription'),
    ]

    operations = [
        migrations.CreateModel(
            name='SessionEvaluationIPGEI',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('type_session', models.CharField(choices=[('normale', 'Session normale'), ('rattrapage', 'Session de rattrapage')], max_length=12)),
                ('est_ouverte', models.BooleanField(default=False)),
                ('est_close', models.BooleanField(default=False)),
                ('date_debut_saisie', models.DateField(blank=True, help_text='Indicative — affichée aux enseignants.', null=True)),
                ('date_fin_saisie', models.DateField(blank=True, help_text='Indicative — la clôture seule fait foi.', null=True)),
                ('plafond_rattrapage', models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True, validators=[django.core.validators.MinValueValidator(Decimal('0')), django.core.validators.MaxValueValidator(Decimal('20'))])),
                ('ouverte_le', models.DateTimeField(blank=True, null=True)),
                ('cloturee_le', models.DateTimeField(blank=True, null=True)),
                ('cloturee_par', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='sessions_ipgei_cloturees', to=settings.AUTH_USER_MODEL)),
                ('semestre', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sessions', to='ipgei.semestreipgei')),
            ],
            options={
                'verbose_name': 'session de saisie IPGEI',
                'verbose_name_plural': 'sessions de saisie IPGEI',
                'db_table': 'ipgei_session_evaluation',
                'ordering': ['semestre', 'type_session'],
            },
        ),
        migrations.AddConstraint(
            model_name='sessionevaluationipgei',
            constraint=models.UniqueConstraint(fields=('semestre', 'type_session'), name='uniq_ipgei_session_evaluation'),
        ),
        migrations.RunPython(peupler_sessions, depeupler_sessions),
    ]
