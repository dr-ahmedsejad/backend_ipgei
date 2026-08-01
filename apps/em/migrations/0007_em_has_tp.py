from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('em', '0006_coefficient_integer'),
    ]

    operations = [
        migrations.AddField(
            model_name='em',
            name='has_tp',
            field=models.BooleanField(
                default=False,
                help_text="L'élément comporte une note de TP (pondération /5).",
            ),
        ),
    ]
