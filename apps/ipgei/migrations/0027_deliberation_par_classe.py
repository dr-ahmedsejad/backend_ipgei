from django.db import migrations, models
import django.db.models.deletion


def rattacher_aux_classes(apps, schema_editor):
    """
    Rattache les délibérations existantes à leur classe, quand elle ne fait
    aucun doute.

    Un jury tenu avant la règle portait tout un niveau. Si toutes ses lignes
    viennent d'une même classe — le cas courant, un niveau n'ayant souvent
    qu'une classe ouverte — le rattachement est certain et l'on peut le poser.
    Sinon on laisse vide : deviner reviendrait à réécrire après coup ce qu'un
    procès-verbal déjà signé affirme.
    """
    Deliberation = apps.get_model('ipgei', 'Deliberation')

    for deliberation in Deliberation.objects.filter(classe__isnull=True):
        classes = set(
            deliberation.lignes.values_list('inscription__classe_id', flat=True)
        )
        classes.discard(None)
        if len(classes) == 1:
            deliberation.classe_id = classes.pop()
            deliberation.save(update_fields=['classe'])


def detacher(apps, schema_editor):
    """Retour en arrière : le champ disparaît, rien à défaire ligne à ligne."""


class Migration(migrations.Migration):

    dependencies = [
        ('ipgei', '0026_signataire'),
    ]

    operations = [
        migrations.AddField(
            model_name='deliberation',
            name='classe',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='deliberations', to='ipgei.classe',
            ),
        ),
        migrations.RunPython(rattacher_aux_classes, detacher),
    ]
