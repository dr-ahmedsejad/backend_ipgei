"""
Migration : ajout du champ FK `em` (→ em.EM) sur InscriptionElement.
Utilisé par _creer_inscriptions_pedagogiques pour lier directement
les cours de planification (EM) aux inscriptions automatiques.
Le champ est nullable : aucun impact sur les lignes existantes.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inscriptions', '0004_element_fk_to_modules'),
        ('em', '0006_coefficient_integer'),
    ]

    operations = [
        migrations.AddField(
            model_name='inscriptionelement',
            name='em',
            field=models.ForeignKey(
                blank=True,
                help_text='Élément de module (planification) lié à cette inscription.',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='inscriptions_elements',
                to='em.em',
            ),
        ),
    ]
