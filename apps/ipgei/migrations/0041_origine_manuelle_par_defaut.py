"""
`SeanceReelle.origine` par défaut : « grille » → « manuelle », et l'existant
réétiqueté.

Le défaut valait `grille`, si bien que toute séance saisie à la main en
héritait. Or c'est sur cette étiquette que repose la promesse de la
duplication — « les séances ajoutées à la main ne sont jamais écrasées » —,
qui était donc fausse : au relevé, onze séances manuelles portaient
« grille » et auraient été détruites au premier écrasement.

`seance_type` tranche sans ambiguïté : nul, la séance n'a jamais été
dupliquée d'un patron, donc elle n'en vient pas. C'est le seul critère
disponible, et il est sûr — `dupliquer_grille` le renseigne toujours.

Les permutations ne sont pas touchées : elles portent déjà leur propre
origine, et `seance_type` nul ne suffit pas à les distinguer d'une saisie.
"""
from django.db import migrations, models


def etiqueter_manuelles(apps, schema_editor):
    """Les séances jamais issues d'un patron redeviennent « manuelles »."""
    SeanceReelle = apps.get_model('ipgei', 'SeanceReelle')
    SeanceReelle.objects.filter(
        origine='grille', seance_type__isnull=True,
    ).update(origine='manuelle')


def retablir_grille(apps, schema_editor):
    """
    Retour en arrière : on rend « grille » à ce qu'on vient d'étiqueter.

    Imparfait, et c'est assumé — une séance manuelle d'avant la migration est
    désormais indistinguable d'une séance manuelle créée depuis. Le retour
    ramène donc l'ambiguïté d'origine, ce qui est le propre d'un retour en
    arrière sur une correction de données.
    """
    SeanceReelle = apps.get_model('ipgei', 'SeanceReelle')
    SeanceReelle.objects.filter(
        origine='manuelle', seance_type__isnull=True,
    ).update(origine='grille')


class Migration(migrations.Migration):

    dependencies = [
        ('ipgei', '0040_auditeur_libre'),
    ]

    operations = [
        migrations.AlterField(
            model_name='seancereelle',
            name='origine',
            field=models.CharField(
                choices=[('grille', 'Dupliquée de la grille type'),
                         ('dupliquee', "Recopiée d'une autre semaine"),
                         ('manuelle', 'Ajoutée manuellement'),
                         ("permutation", "Issue d'une permutation")],
                default='manuelle', max_length=12),
        ),
        migrations.RunPython(etiqueter_manuelles, retablir_grille),
    ]
