from django.db import migrations, models
import django.db.models.deletion


#: L'année universitaire est portée en TEXTE par IPGEI (et par le socle sur
#: `departement`), alors que le registre `annee` la porte en table. Rien ne
#: reliait les deux : une chaîne quelconque entrait sans contrôle, et c'est
#: ainsi qu'une année a existé côté IPGEI sans exister au registre — d'où un
#: tarif introuvable et dix-sept inscriptions ouvertes à zéro.
#:
#: Django ne sait pas déclarer une clé étrangère vers une colonne non primaire.
#: Elle est donc posée en SQL, hors de l'ORM : `makemigrations` ne la gérera
#: pas, et une violation remontera en `IntegrityError`. Le contrôle applicatif
#: (`socle.annee_socle`) reste ce qui donne le message ; ceci est le filet.
CONTRAINTES = [
    ('departement',       'fk_departement_annee_univ'),
    ('ipgei_inscription', 'fk_ipgei_inscription_annee_univ'),
    ('ipgei_semestre',    'fk_ipgei_semestre_annee_univ'),
]

POSER = '\n'.join(
    f'ALTER TABLE {table} ADD CONSTRAINT {nom} '
    f'FOREIGN KEY (annee_universitaire) REFERENCES annee(annee) '
    f'ON UPDATE CASCADE DEFERRABLE INITIALLY DEFERRED;'
    for table, nom in CONTRAINTES
)

RETIRER = '\n'.join(
    f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {nom};'
    for table, nom in CONTRAINTES
)


class Migration(migrations.Migration):

    dependencies = [
        ('ipgei', '0027_deliberation_par_classe'),
    ]

    operations = [
        migrations.AddField(
            model_name='inscriptionipgei',
            name='issue_de',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='inscriptions_issues', to='ipgei.lignedeliberation',
            ),
        ),
        # `ON UPDATE CASCADE` : renommer une année au registre suit alors sur
        # les trois tables, au lieu de laisser des lignes orphelines. C'était
        # jusqu'ici impossible à faire proprement.
        migrations.RunSQL(POSER, RETIRER),
    ]
