"""
Le type de séance devient une référence au socle plutôt qu'une chaîne figée.

IPGEI tenait sa propre liste — `cours`, `td`, `tp`, `ds` — qu'il fallait
traduire à chaque échange avec le Suivi (`cours` → `CM`). Le référentiel
« Paramètres → Séances » en contient douze, dont les examens EF et ER qui
manquaient, et il est administrable sans toucher au code.

PostgreSQL ne sait pas convertir une colonne texte en clé étrangère : la
migration passe donc par une colonne temporaire, remplit, puis remplace. Les
séances existantes sont converties par la correspondance ci-dessous ; un type
absent du référentiel y est créé plutôt que de faire échouer la migration sur
une base dont on ne connaît pas l'état.
"""
from django.db import migrations, models
import django.db.models.deletion


CORRESPONDANCE = {
    'cours': 'CM',
    'td':    'TD',
    'tp':    'TP',
    'ds':    'DS',
}


def convertir(apps, schema_editor):
    Seance       = apps.get_model('parametres', 'Seance')
    SeanceType   = apps.get_model('ipgei', 'SeanceType')
    SeanceReelle = apps.get_model('ipgei', 'SeanceReelle')

    references = {}
    for code_ipgei, libelle in CORRESPONDANCE.items():
        reference, _ = Seance.objects.get_or_create(
            type_seance=libelle, defaults={'is_special': False},
        )
        references[code_ipgei] = reference

    # Repli sur CM : une valeur inattendue vaut mieux rattachée qu'orpheline,
    # la migration ne pouvant pas deviner l'intention.
    defaut = references['cours']

    # `order_by()` sans argument neutralise l'ordre par défaut : trier par
    # grille remonterait jusqu'à `Classe`, dont les champs hérités de
    # `Departement` n'existent pas dans l'état historique des migrations.
    for modele in (SeanceType, SeanceReelle):
        for seance in modele.objects.order_by():
            seance.type_seance_ref = references.get(seance.type_seance_ancien, defaut)
            seance.save(update_fields=['type_seance_ref'])


def revenir(apps, schema_editor):
    inverse = {v: k for k, v in CORRESPONDANCE.items()}
    for nom in ('SeanceType', 'SeanceReelle'):
        modele = apps.get_model('ipgei', nom)
        for seance in modele.objects.order_by().select_related('type_seance_ref'):
            seance.type_seance_ancien = inverse.get(
                seance.type_seance_ref.type_seance, 'cours')
            seance.save(update_fields=['type_seance_ancien'])


class Migration(migrations.Migration):

    dependencies = [
        ('ipgei', '0011_backfill_suivi_genere_le'),
        ('parametres', '0001_initial'),
    ]

    operations = [
        # 1. Mettre l'ancienne colonne de côté.
        migrations.RenameField('seancetype',   'type_seance', 'type_seance_ancien'),
        migrations.RenameField('seancereelle', 'type_seance', 'type_seance_ancien'),

        # 2. Créer la clé étrangère, temporairement facultative.
        migrations.AddField(
            model_name='seancetype', name='type_seance_ref',
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name='+', to='parametres.seance'),
        ),
        migrations.AddField(
            model_name='seancereelle', name='type_seance_ref',
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name='+', to='parametres.seance'),
        ),

        # 3. Convertir, puis retirer l'ancienne colonne et rendre la clé
        #    obligatoire — dans cet ordre, sinon la contrainte tomberait sur des
        #    lignes pas encore converties.
        migrations.RunPython(convertir, revenir),

        migrations.RemoveField('seancetype',   'type_seance_ancien'),
        migrations.RemoveField('seancereelle', 'type_seance_ancien'),

        migrations.RenameField('seancetype',   'type_seance_ref', 'type_seance'),
        migrations.RenameField('seancereelle', 'type_seance_ref', 'type_seance'),

        migrations.AlterField(
            model_name='seancetype', name='type_seance',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='+', to='parametres.seance'),
        ),
        migrations.AlterField(
            model_name='seancereelle', name='type_seance',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='+', to='parametres.seance'),
        ),
    ]
