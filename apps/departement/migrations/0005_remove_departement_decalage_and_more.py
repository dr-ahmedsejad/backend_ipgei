# Migration manuelle (auto-generee corrigee) :
# - RENAME decalage -> decalage_impair (preserve les valeurs existantes)
# - ADD decalage_pair (default 0)
# - ALTER decalage_impair pour ajouter le help_text et garder default=0

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('departement', '0004_departement_is_container'),
    ]

    operations = [
        # 1. Renommer le champ existant decalage en decalage_impair.
        #    Preserve toutes les valeurs (ex: L1 G1/G2 avec decalage=1 -> decalage_impair=1).
        migrations.RenameField(
            model_name='departement',
            old_name='decalage',
            new_name='decalage_impair',
        ),
        # 2. Ajouter le help_text au champ renomme (optionnel, juste cosmetique pour admin Django).
        migrations.AlterField(
            model_name='departement',
            name='decalage_impair',
            field=models.IntegerField(
                default=0,
                help_text='Semaines sautées au début du semestre Impair (rentrée). '
                          'Ex : L1 avec formation militaire de 3 semaines = 3.',
            ),
        ),
        # 3. Ajouter le nouveau champ decalage_pair (initialise a 0 partout).
        migrations.AddField(
            model_name='departement',
            name='decalage_pair',
            field=models.IntegerField(
                default=0,
                help_text='Semaines sautées au début du semestre Pair (rare : stage, '
                          'examens reportés…). Mettre 0 si non applicable.',
            ),
        ),
    ]
