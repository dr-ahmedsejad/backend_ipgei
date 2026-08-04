"""
Référentiel des niveaux : MPSI et MP cessent d'être des constantes du code.

Quatre règles y étaient adossées — semestres suivis, droit au redoublement,
décisions du jury, rang d'année commandant le tarif. Les porter en données est
ce qui permet d'ajouter un niveau, MPI par exemple, sans toucher au code.

Le peuplement recrée à l'identique le cursus existant, et rattache chaque
matière au niveau que son semestre désignait jusqu'ici : rien ne change de
comportement du seul fait de cette migration.
"""

import django.core.validators
from django.db import migrations, models
import django.db.models.deletion



def peupler_niveaux(apps, schema_editor):
    Niveau      = apps.get_model('ipgei', 'NiveauIPGEI')
    Matiere     = apps.get_model('ipgei', 'Matiere')
    Institution = apps.get_model('parametres', 'Institution')

    # Le cursus d'origine, tel que le code le décrivait en dur.
    ORIGINE = [
        {'code': 'MPSI', 'libelle': 'MPSI — 1re année', 'rang': 1,
         'redoublement_autorise': False, 'ordre': 1,
         'decisions_possibles': ['admis', 'reoriente']},
        {'code': 'MP', 'libelle': 'MP — 2e année', 'rang': 2,
         'redoublement_autorise': True, 'ordre': 2,
         'decisions_possibles': ['autorise_cnim', 'redoublant', 'exclu']},
    ]
    SEMESTRES = {'S1': 'MPSI', 'S2': 'MPSI', 'S3': 'MP', 'S4': 'MP'}

    for institution in Institution.objects.all():
        fiches = {}
        for donnees in ORIGINE:
            fiche, _ = Niveau.objects.get_or_create(
                institution=institution, code=donnees['code'], defaults=donnees,
            )
            fiches[donnees['code']] = fiche

        # Chaque matière rejoint le niveau que son semestre désignait.
        for matiere in Matiere.objects.filter(institution=institution, niveau_ref__isnull=True):
            code = SEMESTRES.get(matiere.code_semestre)
            if code and code in fiches:
                matiere.niveau_ref = fiches[code]
                matiere.save(update_fields=['niveau_ref'])


def depeupler_niveaux(apps, schema_editor):
    apps.get_model('ipgei', 'Matiere').objects.update(niveau_ref=None)
    apps.get_model('ipgei', 'NiveauIPGEI').objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('parametres', '0015_institution_diplome_sequence_debut'),
        ('ipgei', '0020_lignedeliberation_nb_redoublements_avant_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='NiveauIPGEI',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(help_text='Code court affiché partout : MPSI, MP, MPI…', max_length=10)),
                ('libelle', models.CharField(blank=True, default='', help_text='Intitulé complet. Vide, le code et le rang en tiennent lieu.', max_length=100)),
                ('rang', models.PositiveSmallIntegerField(default=1, help_text='1 = première année, 2 = deuxième année. Commande les semestres et le tarif.', validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(2)])),
                ('redoublement_autorise', models.BooleanField(default=False, help_text='Le jury peut-il prononcer un redoublement à ce niveau ? Interdit en première année, sans exception.')),
                ('decisions_possibles', models.JSONField(blank=True, default=list, help_text='Codes des décisions que le jury peut prononcer. Vide = celles du rang.')),
                ('actif', models.BooleanField(default=True, help_text='Un niveau désactivé disparaît des écrans sans rien effacer : inscriptions et délibérations passées restent lisibles.')),
                ('ordre', models.PositiveSmallIntegerField(default=0, help_text="Ordre d'affichage.")),
            ],
            options={
                'verbose_name': 'niveau IPGEI',
                'verbose_name_plural': 'niveaux IPGEI',
                'db_table': 'ipgei_niveau',
                'ordering': ['rang', 'ordre', 'code'],
            },
        ),
        migrations.RemoveConstraint(
            model_name='matiere',
            name='uniq_ipgei_matiere',
        ),
        # Les champs d'abord, les contraintes ensuite : l'auto-génération place
        # la contrainte sur `niveau_ref` avant de créer la colonne.
        migrations.AddField(
            model_name='niveauipgei',
            name='institution',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='niveaux_ipgei', to='parametres.institution'),
        ),
        migrations.AddField(
            model_name='matiere',
            name='niveau_ref',
            field=models.ForeignKey(blank=True, help_text='Niveau dont cette matière fait partie de la maquette.', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='matieres', to='ipgei.niveauipgei'),
        ),
        migrations.AddConstraint(
            model_name='matiere',
            constraint=models.UniqueConstraint(fields=('institution', 'code', 'code_semestre', 'niveau_ref'), name='uniq_ipgei_matiere_niveau'),
        ),
        migrations.AddConstraint(
            model_name='niveauipgei',
            constraint=models.UniqueConstraint(fields=('institution', 'code'), name='uniq_ipgei_niveau_code'),
        ),
        migrations.RunPython(peupler_niveaux, depeupler_niveaux),
    ]
