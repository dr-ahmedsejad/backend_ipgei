"""
Migration : InscriptionElement.element passe de em.EM à modules.ElementModule.
Les étapes 1-4 sont déjà appliquées (DDL auto-committed MySQL).
On utilise --fake pour marquer cette migration sans la ré-exécuter.
Le champ reste nullable (null=True) — une ligne orpheline existe avec element_id=NULL.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inscriptions', '0003_add_est_dette_to_inscriptionpedagogique'),
        ('modules', '0001_initial'),
        ('em', '0002_em_coefficient_em_credits_em_est_element_module_and_more'),
    ]

    operations = [
        # 1. Supprimer l'unique_together qui référence l'ancien champ 'element'
        migrations.AlterUniqueTogether(
            name='inscriptionelement',
            unique_together=set(),
        ),
        # 2. Supprimer l'ancien champ FK vers em.EM
        migrations.RemoveField(
            model_name='inscriptionelement',
            name='element',
        ),
        # 3. Ajouter le nouveau champ FK vers modules.ElementModule (nullable)
        migrations.AddField(
            model_name='inscriptionelement',
            name='element',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='inscriptions_elements',
                to='modules.elementmodule',
                help_text='Élément de module LMD.',
                null=True,
                blank=True,
            ),
        ),
        # 4. Rétablir l'unique_together avec le nouveau champ
        migrations.AlterUniqueTogether(
            name='inscriptionelement',
            unique_together={('inscription_ped', 'element')},
        ),
        # NOTE : on ne fait PAS le AlterField NULL→NOT NULL car la table contient
        # une ligne avec element_id=NULL (MySQL STRICT_TRANS_TABLES le refuserait).
        # Utiliser --fake pour marquer cette migration comme appliquée.
    ]
